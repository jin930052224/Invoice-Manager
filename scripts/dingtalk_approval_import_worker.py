#!/usr/bin/env python3
import io
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import build_order_block  # noqa: E402
import ledger_guard  # noqa: E402  # only for optional read-only diagnostics

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ENV = PROJECT_ROOT / 'backend' / '.env'
ROOT = Path('/Volumes/D 盘/Codex/发票管理系统/dingtalk-import-queue')
STATE = ROOT / 'imported.json'
WORKER_LOCK = ROOT / '.worker.lock'
APPROVALS = ROOT / 'approvals.json'
#: 《报销统计表》同步队列——由 dingtalk_sync_worker.py 消费
SYNC_QUEUE = Path('/Volumes/D 盘/Codex/发票管理系统/dingtalk-sync-queue')

#: 处理完的 job 文件必须搬走。**这不是打扫卫生，是功能性的一步**：
#: `main()` 只在「ROOT 里没有 job-*.json」时才造 hourly 任务，
#: job 赖着不走 = hourly 永久停摆 + 每次触发都重复处理同一个 job。
#: 2026-09-23 实测：14:22 一个补导入 job 留下后，hourly 从此再不运行。
#: （9/20 那次更严重：一个格式非法的 job 常驻 + worker 崩溃，死循环了一整天。）
PROCESSED = ROOT / 'processed'
CANCELLED = ROOT / 'cancelled'

#: 每日对账回填的状态文件（记「今天跑过没有」）
RECONCILE_STATE = ROOT / 'reconcile-state.json'
HISTORICAL_RECONCILE_STATE = ROOT / 'historical-reconcile-state.json'
#: 每日对账回填的窗口。hourly 只扫近 2 小时，只要 worker 停摆超过 2 小时
#: （崩溃 / 没登录 / 机器重启）那段窗口的单就永久漏掉——所以需要一个大窗口兜底。
#: 90 天足够覆盖「坏了几天才被发现」；成本可控，因为 run_job 本身幂等。
RECONCILE_DAYS = 90
HISTORICAL_RECONCILE_DAYS = 365
HISTORICAL_RECONCILE_INTERVAL_DAYS = 7
APP_UNIFIED_ID = '6f54f334-fd10-456b-b6a8-2c7f726ec7f6'

#: 「自动补台账」总开关。**默认关闭**。
#: 补票（往票池灌）只增不改，可以直接开；补台账是**往账上加钱**，
#: 是不可逆性最强的一步，所以要求显式开启：把这个文件建出来即开。
#: 关着的时候台账 job 不入队（票照常导入），巡检/对账报告照常报缺。
LEDGER_AUTOWRITE_FLAG = ROOT / 'allow-ledger-autowrite'

TZ = timezone(timedelta(hours=8))
PROCESS = 'PROC-FF9942ED-92C0-46BA-9923-3861BBB66AAE'
API = 'http://127.0.0.1:18841/api'
SUPPORTED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}
ARCHIVE_TYPES = {'zip', 'rar'}
# 压缩包防护：单包最多解出多少张、解出来总共多大，避免一个异常包把 worker 拖死
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
# 上传接口有 @limiter.limit("10/minute")。上次 365 天回补时 downloaded=146 但只 success=39，
# 剩下 129 张全是被 429 打回来的——所以这里必须主动限速 + 遇 429 退避重试，
# 不能把限流当成「文件有问题」直接记 failed。
UPLOAD_MIN_INTERVAL = 7.0
MAX_RATE_LIMIT_RETRIES = 5
PROCESSING_POLL_INTERVAL = 2.0
PROCESSING_WAIT_TIMEOUT = 300
PENDING_RECORD_STATUSES = {'已上传', '解析中'}

# 审批附件下载优先使用企业内部应用 App Token。用户登录态接口对少量历史 fileId
# 会固定返回 business_error，而钉钉客户端仍然能下载；官方服务端接口才是无人值守
# 导入应依赖的稳定通道。应用尚未开通 Workflow.Instance.Write 时会自动回退现有
# dws 用户态，保证当前链路不因权限尚未申请而整体停摆。
APP_ATTACHMENT_DOWNLOAD_PATH = '/v1.0/workflow/processInstances/spaces/files/urls/download'
APP_ATTACHMENT_SCOPE = 'Workflow.Instance.Write'
_APP_CREDENTIALS_CACHE = None
_STORAGE_OPERATOR_UNION_ID = None
_APP_DOWNLOAD_DISABLED_REASON = None


class AppAttachmentDownloadUnavailable(RuntimeError):
    """企业内部应用附件下载通道当前不可用，可安全回退用户态。"""


class UploadThrottle:
    """把上传节奏压到限流阈值以下；单个 worker 进程内共享。"""

    def __init__(self, min_interval=UPLOAD_MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def upload_with_retry(token_cache, throttle, data, file_name, detail, owner, claim_amount):
    """上传单个文件；被限流就退避重试，其他错误照常抛出。"""
    last_error = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        throttle.wait()
        try:
            return upload_one(login_token(token_cache), data, file_name, detail, owner, claim_amount)
        except requests.HTTPError as exc:
            response = getattr(exc, 'response', None)
            last_error = exc
            if response is not None and response.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception as exc:
            last_error = exc
            raise
    if last_error:
        raise last_error


def _extension(name):
    return name.rsplit('.', 1)[-1].lower() if '.' in str(name) else ''


def _is_noise(name):
    """macOS 打包会塞 __MACOSX 和 .DS_Store，这些不是发票。"""
    parts = str(name).replace('\\', '/').split('/')
    return any(p == '__MACOSX' or p.startswith('._') or p == '.DS_Store' for p in parts)


def _decode_zip_name(info):
    """zip 里没打 UTF-8 标记时 zipfile 按 cp437 解，中文名会变成乱码，
    再按 GBK 转回来才能拿到「高铁票1.pdf」这种名字。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode('cp437').decode('gbk')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def extract_archive(data, file_type):
    """把压缩包拆成 [(内部文件名, 字节)]，只保留 jpg/jpeg/png/pdf 且过滤掉系统垃圾文件。

    zip 用标准库直接解；rar 交给 macOS 自带的 bsdtar（libarchive 原生支持 RAR/RAR5），
    这样不用额外装 unar/7z。返回 (entries, total_seen)，total_seen 是包里一共多少个条目，
    便于把「包里确实没发票」和「解包失败」区分开。
    """
    kind = (file_type or '').lower()
    if kind == 'zip':
        return _extract_zip(data)
    if kind == 'rar':
        return _extract_with_bsdtar(data)
    return [], 0


def _extract_zip(data):
    entries = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            for info in infos[:MAX_ARCHIVE_ENTRIES]:
                name = _decode_zip_name(info)
                if _is_noise(name) or _extension(name) not in SUPPORTED_EXTENSIONS:
                    continue
                if info.file_size > MAX_ARCHIVE_BYTES:
                    continue
                entries.append((name, archive.read(info)))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        # 加密包 / 损坏包：记日志跳过，不要把整轮扫描带崩
        print(f'压缩包解包失败(按 zip 处理): {exc}')
        return [], 1
    return entries, len(entries)


def _extract_with_bsdtar(data):
    bsdtar = '/usr/bin/bsdtar'
    if not Path(bsdtar).exists():
        print('没有 bsdtar，rar 包无法解包')
        return [], 0
    entries = []
    with tempfile.TemporaryDirectory() as work:
        archive_path = Path(work) / 'archive.rar'
        archive_path.write_bytes(data)
        out_dir = Path(work) / 'out'
        out_dir.mkdir()
        proc = subprocess.run([bsdtar, '-xf', str(archive_path), '-C', str(out_dir)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print(f'rar 解包失败: {proc.stderr.strip()[:200]}')
            return [], 1
        seen = total = 0
        for path in sorted(out_dir.rglob('*')):
            if not path.is_file():
                continue
            total += 1
            relative = path.relative_to(out_dir)
            if _is_noise(str(relative)) or _extension(path.name) not in SUPPORTED_EXTENSIONS:
                continue
            if seen >= MAX_ARCHIVE_ENTRIES:
                break
            relative_bytes = path.read_bytes()
            if len(relative_bytes) > MAX_ARCHIVE_BYTES:
                continue
            seen += 1
            entries.append((str(relative), relative_bytes))
    return entries, total


def save(job, status):
    """status-<job id>.json 落盘。

    ⚠️ 为什么这些 status 文件可以留在 ROOT（而 job-*.json 必须搬走）：
    launchd 的 `WatchPaths` 只在**目录条目增删**时触发，覆盖已存在的文件不改目录 mtime。
    status 文件名是稳定的（`hourly-<yyyymmddHH>`、`reconcile-<yyyymmdd>`），
    同一个任务重复跑就是覆盖，不会再触发一轮——**反馈环在第二次迭代就自熄**。
    真正把 hourly 卡死的是 job 文件赖着不走（见 archive_job），不是 status 文件。
    2026-09-23 推演过「把 status 挪去子目录」的方案，结论是不解决任何实际问题、
    只会让产物路径变复杂，故不做。
    """
    status['attachment_download_channel'] = download_channel_status()
    status['updated_at'] = datetime.now(TZ).isoformat()
    (ROOT / f"status-{job['id']}.json").write_text(json.dumps(status, ensure_ascii=False))


def mark_imported(imported, key):
    """每次成功处理后立即记录，避免进程中断后再次上传同一附件。"""
    imported.add(key)
    write_if_changed(STATE, json.dumps(sorted(imported), ensure_ascii=False))

def blank_status(job):
    return {'id': job['id'], 'status': 'scanning', 'total': 0, 'downloaded': 0, 'recognized': 0, 'invoices': 0, 'non_invoices': 0, 'skipped': 0, 'failed': 0, 'rejected_approvals': 0, 'windows_total': 0, 'windows_completed': 0, 'scanned_approvals': 0, 'failure_samples': [],
            # 《报销统计表》同步入队计数（2026-09-22 起）
            'orders_queued': 0, 'orders_already_queued': 0, 'orders_skipped_no_rows': 0,
            'orders_deferred_processing': 0,
            # 安全闸门计数（2026-09-23 起，见 ledger_guard.py / ledger_write_gate）
            # already_in_ledger = 台账已有该单，没事可做（**不是**问题，别当告警看）
            # ledger_off        = 自动补台账总开关没开（默认）
            # duplicate         = 疑似重复报销，拦下等人确认
            # no_index          = 台账读不到，核对不了所以不写钱（fail closed）
            # out_of_scope      = 早于台账范围起点（2026-06-15），本来就不收（不是问题）
            'orders_already_in_ledger': 0,
            'orders_blocked_ledger_off': 0, 'orders_blocked_duplicate': 0,
            'orders_blocked_no_index': 0, 'orders_blocked_out_of_scope': 0,
            'blocked_samples': []}


MAX_FAILURE_SAMPLES = 20


def record_failure(status, stage, name, exc):
    """把失败原因记进 status。

    只有计数器没有原因时，「failed: 5」等于没有信息——分不清是钉钉下载挂了、
    限流、还是文件本身有问题，每次都得回头翻日志。留住前 N 条样本够定位了。
    """
    status['failed'] += 1
    if len(status['failure_samples']) < MAX_FAILURE_SAMPLES:
        status['failure_samples'].append({
            'stage': stage,
            'fileName': name,
            'error': f'{type(exc).__name__}: {exc}'[:300],
        })


def download_channel_status():
    """给巡检留下当前附件下载通道，避免再次把权限缺口误判成文件损坏。"""
    if _APP_DOWNLOAD_DISABLED_REASON:
        return {'mode': 'user-fallback', 'reason': _APP_DOWNLOAD_DISABLED_REASON}
    return {'mode': 'app-preferred', 'scope': APP_ATTACHMENT_SCOPE}


def list_approvals(start, end):
    approvals, cursor = [], None
    while True:
        command = [DWS, 'oa', 'approval', 'list-by-admin', '--process-code', PROCESS,
                   '--start', start.isoformat(timespec='seconds'),
                   '--end', end.isoformat(timespec='seconds'), '--limit', '20', '--format', 'json']
        # 首次请求必须省略 --cursor；传 0/空串在部分历史日期会返回错误或空页。
        # 只有服务端明确 hasMore 时，第二页起才附带 nextCursor。
        if cursor is not None:
            command.extend(['--cursor', cursor])
        result = json.loads(subprocess.check_output(command, text=True, timeout=30))['result']
        approvals.extend(result.get('list', []))
        if not result.get('hasMore'):
            return approvals
        cursor = str(result['nextCursor'])


def approval_submitted_at(approval):
    """Use DingTalk's millisecond createTime; never infer a precise time from the number."""
    try:
        timestamp = int(approval.get('createTime'))
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp / 1000, TZ).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError, OSError):
        return None

def windows_for(now, lookback_days):
    start = now - timedelta(days=lookback_days)
    while start < now:
        end = min(start + timedelta(days=30), now)
        yield start, end
        start = end

def approval_windows(approval_number):
    """审批单号前 8 位是 yyyymmdd。格式非法时返回 None，交由调用方标记任务，
    而不是抛出异常拖垮整个 worker。前 8 位解析出的年份也要落在合理区间，
    否则像发票号码 26317000003387517253 会被误解析成 2631 年。"""
    prefix = str(approval_number).strip()[:8]
    try:
        day = datetime.strptime(prefix, '%Y%m%d').replace(tzinfo=TZ)
    except ValueError:
        return None
    if not 2000 <= day.year <= 2100:
        return None
    return [(day, day + timedelta(days=1))]

def is_approved(detail):
    """钉钉审批列表把「审批未通过」也返回 status=COMPLETED，只有
    detail.processInstanceResult 能区分：agree=通过，refuse=驳回。
    RUNNING 的中间节点也可能返回 agree，不能据此提前同步台账。"""
    return (str(detail.get('status') or '').upper() == 'COMPLETED'
            and str(detail.get('processInstanceResult') or '').lower() == 'agree')


def should_import_attachments(detail):
    """提交后即识别附件供财务查重；撤销和驳回的审批不再导入。"""
    return str(detail.get('status') or '').upper() == 'RUNNING' or is_approved(detail)


def should_enqueue_order(detail, records_ready):
    """未通过审批，即使附件已识别完成，也不能写入报销统计表。"""
    return records_ready and is_approved(detail)

def attachments_from_detail(detail):
    """Read both ordinary attachment controls and attachments in expense rows."""
    attachments = []
    for field in detail.get('formValueVOS', []):
        if field.get('componentType') == 'DDAttachment' and field.get('value'):
            attachments.extend(json.loads(field['value']))
        if field.get('componentType') == 'TableField' and field.get('value'):
            for row in json.loads(field['value']):
                claim_amount = next((cell.get('value') for cell in row.get('rowValue', []) if cell.get('label') in {'报销金额（元）', '金额（元）'}), None)
                for cell in row.get('rowValue', []):
                    value = cell.get('value')
                    if isinstance(value, list) and (str(cell.get('key', '')).startswith('DDAttachment_') or cell.get('label') in {'发票', '附件'}):
                        for attachment in value:
                            attachment = dict(attachment)
                            attachment['_approval_claim_amount'] = claim_amount
                            attachments.append(attachment)
    return attachments

def enqueue_order_job(detail, list_item, status=None):
    """把一张审批单排进《报销统计表》同步队列。

    2026-09-22 起同步目标从 AI 表格 hERWDMS 换成钉钉在线表格《报销统计表》。
    这里顺手入队是**零额外开销**的——`detail` 本来就已经拉下来了。

    ⚠️ 同步口径：后台源记录镜像。
      1. 必须在 `is_approved()` 判定通过之后调用；
      2. 允许同一审批单反复入队，worker 按后台 Invoice.id 增量幂等；
      3. 只要审批详情可映射就入队，票号、金额、单据类型不参与过滤。
    """
    business_id = str(detail.get('businessId') or '').strip()
    if not business_id:
        return None
    job_path = SYNC_QUEUE / f'order-{business_id}.json'
    if job_path.exists():
        if status is not None:
            status['orders_already_queued'] = status.get('orders_already_queued', 0) + 1
        return None

    block = build_order_block(detail, list_item)

    payload = {
        'kind': 'reimbursement_order',
        'business_id': business_id,
        'queued_at': datetime.now(TZ).isoformat(),
        'block': block,
    }
    try:
        SYNC_QUEUE.mkdir(parents=True, exist_ok=True)
        temporary = SYNC_QUEUE / f'.{job_path.name}.tmp'
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(job_path)   # 原子落盘，避免 worker 读到半个文件
    except Exception as exc:
        print(f'入队报销统计表同步失败 {business_id}: {exc}')
        return None
    if status is not None:
        status['orders_queued'] = status.get('orders_queued', 0) + 1
    return job_path


def login_token(cache):
    """登录 token 在单个任务内复用，避免压缩包里几十张图就登几十次。"""
    if 'token' not in cache:
        cache['token'] = requests.post(f'{API}/auth/login',
                                       json={'username': 'admin', 'password': 'qwe123'},
                                       timeout=30).json()['access_token']
    return cache['token']


def upload_one(token, data, file_name, detail, owner, claim_amount):
    form = {'processing_mode': 'llm_only',
            'approval_instance_id': detail['businessId'],
            'owner': owner}
    if claim_amount is not None:
        form['approval_claim_amount'] = str(claim_amount)
    response = requests.post(f'{API}/invoices/upload',
                             headers={'Authorization': f'Bearer {token}'},
                             data=form,
                             files=[('files', (file_name, data, 'application/octet-stream'))],
                             timeout=120)
    response.raise_for_status()
    created = response.json()[0]
    if created.get('status') != 'success':
        raise RuntimeError(created.get('message', '上传失败'))
    return created


def fetch_approval_records(token, business_id):
    """读取一张审批单在后台的全部源记录，供识别完成后再投递台账同步。"""
    records = []
    page = 1
    while True:
        response = requests.get(
            f'{API}/invoices/sync-records',
            headers={'Authorization': f'Bearer {token}'},
            params={'approval_number': business_id, 'page': page, 'page_size': 100},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        batch = body.get('items') or []
        records.extend(batch)
        if not batch or len(records) >= int(body.get('total') or 0):
            return records
        page += 1


def wait_for_approval_records_ready(token_cache, business_id,
                                    timeout=PROCESSING_WAIT_TIMEOUT,
                                    interval=PROCESSING_POLL_INTERVAL):
    """等待该审批单后台源记录离开「已上传/解析中」。

    同步层按 Invoice.id 只追加、不更新；半成品一旦写入就不会自动覆盖，所以必须
    等识别进入终态后再生成 order job。返回 (ready, records)。无后台记录也视为
    ready——无票审批仍需要把审批详情写入全量台账。
    """
    deadline = time.monotonic() + max(0, timeout)
    records = []
    while True:
        records = fetch_approval_records(login_token(token_cache), business_id)
        pending = [record for record in records
                   if (str(record.get('status') or '') in PENDING_RECORD_STATUSES
                       and str(record.get('document_type') or '') != 'voucher')]
        if not pending:
            return True, records
        if time.monotonic() >= deadline:
            return False, records
        time.sleep(interval)


def _env_file_values(path):
    """读取简单 KEY=VALUE 配置；不修改环境、不输出任何密钥。"""
    values = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _app_credentials():
    """取钉钉企业内部应用凭据；只在内存中使用，绝不落日志。"""
    global _APP_CREDENTIALS_CACHE
    if _APP_CREDENTIALS_CACHE is not None:
        return _APP_CREDENTIALS_CACHE
    env = _env_file_values(BACKEND_ENV)
    client_id = os.environ.get('DINGTALK_APP_KEY') or env.get('DINGTALK_APP_KEY')
    client_secret = os.environ.get('DINGTALK_APP_SECRET') or env.get('DINGTALK_APP_SECRET')
    _APP_CREDENTIALS_CACHE = (client_id, client_secret) if client_id and client_secret else ()
    return _APP_CREDENTIALS_CACHE


def _download_uri_from_payload(payload):
    """兼容 dws 产品命令和 raw API 的不同响应 envelope。"""
    body = payload.get('data') or payload.get('result') or payload
    uri = (body.get('downloadUri') or body.get('download_uri')) if isinstance(body, dict) else None
    if not uri:
        raise RuntimeError(f'附件下载接口未返回 downloadUri: {json.dumps(payload, ensure_ascii=False)[:300]}')
    return uri


def _download_attachment_app(process_instance_id, file_id):
    """走官方企业内部应用服务端 API 获取临时链接，适合 launchd 无人值守。"""
    global _APP_DOWNLOAD_DISABLED_REASON
    if _APP_DOWNLOAD_DISABLED_REASON:
        raise AppAttachmentDownloadUnavailable(_APP_DOWNLOAD_DISABLED_REASON)
    credentials = _app_credentials()
    if not credentials:
        _APP_DOWNLOAD_DISABLED_REASON = 'missing DINGTALK_APP_KEY/DINGTALK_APP_SECRET'
        raise AppAttachmentDownloadUnavailable(_APP_DOWNLOAD_DISABLED_REASON)
    client_id, client_secret = credentials
    command = [
        DWS, 'api', 'POST', APP_ATTACHMENT_DOWNLOAD_PATH,
        '--client-id', client_id, '--client-secret', client_secret,
        '--data', json.dumps({
            'processInstanceId': process_instance_id,
            'fileId': str(file_id),
        }, ensure_ascii=False),
        '--format', 'json',
    ]
    proc = subprocess.run(command, text=True, capture_output=True, timeout=30)
    raw = (proc.stdout or proc.stderr or '').strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'应用态附件接口非 JSON 输出: {raw[:300]}') from exc
    if proc.returncode:
        message = json.dumps(payload, ensure_ascii=False)
        if (APP_ATTACHMENT_SCOPE in message or 'AccessTokenPermissionDenied' in message
                or 'requiredScopes' in message or '尚未开通所需的权限' in message):
            _APP_DOWNLOAD_DISABLED_REASON = f'app scope missing: {APP_ATTACHMENT_SCOPE}'
            raise AppAttachmentDownloadUnavailable(_APP_DOWNLOAD_DISABLED_REASON)
        raise RuntimeError(f'应用态附件接口失败(rc={proc.returncode}): {message[:500]}')
    return _download_uri_from_payload(payload)


def _download_attachment_user(process_instance_id, file_id):
    """现有 dws 用户登录态通道，保留作应用权限开通前的兼容回退。"""
    link = json.loads(subprocess.check_output(
        [DWS, 'oa', 'approval', 'attachment', 'download-url',
         '--instance-id', process_instance_id, '--file-id', str(file_id), '--format', 'json'],
        text=True, timeout=30))
    return _download_uri_from_payload(link)


def _storage_api(path, body):
    """用应用身份读取审批附件所在的钉盘文件；不记录签名下载链接。"""
    credentials = _app_credentials()
    if not credentials:
        raise RuntimeError('缺少钉钉应用凭证，无法读取审批附件所在的钉盘文件')
    client_id, client_secret = credentials
    union_id = _storage_operator_union_id()
    proc = subprocess.run(
        [DWS, 'api', 'POST', path, '--client-id', client_id,
         '--client-secret', client_secret, '--params', json.dumps({'unionId': union_id}),
         '--data', json.dumps(body), '--format', 'json'],
        text=True, capture_output=True, timeout=30,
    )
    try:
        payload = json.loads((proc.stdout or proc.stderr).strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError('钉盘文件接口没有返回有效 JSON') from exc
    if proc.returncode:
        message = json.dumps(payload, ensure_ascii=False)
        if 'Storage.File.Read' in message:
            raise RuntimeError('钉钉应用缺少 Storage.File.Read（企业存储文件读）权限')
        raise RuntimeError(f'钉盘文件接口失败: {message[:300]}')
    return payload.get('data') or payload.get('result') or payload


def _storage_operator_union_id():
    """从当前钉钉组织身份取得稳定 unionId，避免依赖离职发起人身份。"""
    global _STORAGE_OPERATOR_UNION_ID
    if _STORAGE_OPERATOR_UNION_ID:
        return _STORAGE_OPERATOR_UNION_ID
    profile = json.loads(subprocess.check_output(
        [DWS, 'profile', 'list', '--format', 'json'], text=True, timeout=30))
    current = [item for item in profile.get('profiles') or [] if item.get('isOrgCurrent')]
    if len(current) != 1 or not current[0].get('userId'):
        raise RuntimeError('无法唯一确定当前钉钉组织的操作人')
    credentials = _app_credentials()
    if not credentials:
        raise RuntimeError('缺少钉钉应用凭证')
    proc = subprocess.run(
        [DWS, 'api', 'POST', 'https://oapi.dingtalk.com/topapi/v2/user/get',
         '--client-id', credentials[0], '--client-secret', credentials[1],
         '--data', json.dumps({'userid': current[0]['userId']}), '--format', 'json'],
        text=True, capture_output=True, timeout=30,
    )
    try:
        payload = json.loads((proc.stdout or proc.stderr).strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError('钉钉操作人查询没有返回有效 JSON') from exc
    if proc.returncode:
        message = json.dumps(payload, ensure_ascii=False)
        if 'qyapi_get_member' in message:
            raise RuntimeError('钉钉应用缺少 qyapi_get_member（成员信息读）权限')
        raise RuntimeError(f'钉钉操作人查询失败: {message[:300]}')
    result = payload.get('result') or (payload.get('data') or {}).get('result') or {}
    union_id = result.get('unionid') or result.get('unionId')
    if not union_id:
        raise RuntimeError('钉钉操作人查询未返回 unionId')
    _STORAGE_OPERATOR_UNION_ID = str(union_id)
    return _STORAGE_OPERATOR_UNION_ID


def _download_attachment_storage(attachment):
    """审批下载因发起人离职而失败时，按审批返回的 spaceId/fileId 下载原文件。"""
    if not attachment:
        raise RuntimeError('审批详情没有附件元数据')
    space_id, file_id = str(attachment.get('spaceId') or ''), str(attachment.get('fileId') or '')
    if not space_id.isdigit() or not file_id.isdigit():
        raise RuntimeError('审批附件缺少有效的 spaceId/fileId')
    base = f'/v1.0/storage/spaces/{space_id}/dentries/{file_id}'
    metadata = _storage_api(f'{base}/query', {'option': {'withThumbnail': False}})
    dentry = metadata.get('dentry') or {}
    if (str(dentry.get('id')) != file_id or str(dentry.get('spaceId')) != space_id
            or dentry.get('name') != attachment.get('fileName')):
        raise RuntimeError('钉盘文件信息与审批附件不一致，已停止导入')
    info = _storage_api(f'{base}/downloadInfos/query', {'option': {'preferIntranet': False}})
    signature = info.get('headerSignatureInfo') or {}
    urls = signature.get('resourceUrls') or []
    if not urls:
        raise RuntimeError('钉盘下载接口没有返回文件地址')
    response = requests.get(urls[0], headers=signature.get('headers') or {}, timeout=60)
    response.raise_for_status()
    expected_size = int(attachment.get('fileSize') or 0)
    if expected_size and len(response.content) != expected_size:
        raise RuntimeError('钉盘下载文件大小与审批附件不一致')
    return response.content


def download_attachment(process_instance_id, file_id, attachment=None):
    """应用下载优先，失败后依次尝试用户态与应用钉盘直读。"""
    try:
        uri = _download_attachment_app(process_instance_id, file_id)
        response = requests.get(uri, timeout=60)
        response.raise_for_status()
        return response.content
    except (AppAttachmentDownloadUnavailable, RuntimeError, requests.RequestException) as app_error:
        try:
            uri = _download_attachment_user(process_instance_id, file_id)
            response = requests.get(uri, timeout=60)
            response.raise_for_status()
            return response.content
        except (RuntimeError, subprocess.CalledProcessError, requests.RequestException) as user_error:
            try:
                return _download_attachment_storage(attachment)
            except Exception as storage_error:
                raise RuntimeError(
                    f'审批附件下载均失败：应用态 {type(app_error).__name__}；'
                    f'用户态 {type(user_error).__name__}；钉盘 {storage_error}'
                ) from storage_error


def run_job(job, imported, approval_records):
    now = datetime.now(TZ)
    lookback_days = job.get('lookback_days')
    approval_number = job.get('approval_number')
    throttle = UploadThrottle(job.get('upload_min_interval', UPLOAD_MIN_INTERVAL))
    if approval_number:
        windows = approval_windows(approval_number)
        if windows is None:
            status = blank_status(job)
            status['status'] = 'rejected'
            status['reason'] = f"审批单号格式非法，已跳过: {approval_number}"
            save(job, status)
            print(f"任务 {job['id']} 已拒绝: 审批单号格式非法 {approval_number}")
            return
    else:
        # Use an overlap so an approval completing around an hourly boundary
        # is seen by at least two runs. Imported attachment keys make this
        # idempotent.
        windows = list(windows_for(now, lookback_days)) if lookback_days else [(now - timedelta(hours=2), now)]
    status = blank_status(job)
    status['windows_total'] = len(windows)
    save(job, status)
    approvals = []
    for start, end in windows:
        try:
            approvals.extend(list_approvals(start, end))
        except Exception:
            status['failed'] += 1
        status['windows_completed'] += 1
        status['scanned_approvals'] = len({item['processInstanceId'] for item in approvals})
        save(job, status)

    if approval_number:
        approvals = [item for item in approvals if item.get('businessId') == approval_number]

    status['status'] = 'running'
    save(job, status)
    for approval in {item['processInstanceId']: item for item in approvals}.values():
        if approval.get('businessId'):
            approval_records[approval['businessId']] = {
                'approval_number': approval['businessId'],
                'owner': approval.get('title', '').split('提交的')[0],
                'status': approval.get('status'),
                'result': approval_records.get(approval['businessId'], {}).get('result'),
                'submitted_at': approval_submitted_at(approval)
                                or approval_records.get(approval['businessId'], {}).get('submitted_at'),
            }
            # 注意：这里只更新内存里的 dict，落盘挪到 run_job 末尾一次写完。
            # 对账回填一次要过上百张单，逐单写一个越来越大的 JSON 纯属浪费 IO。
        if approval.get('status') not in {'RUNNING', 'COMPLETED'}:
            continue
        try:
            detail = json.loads(subprocess.check_output([DWS, 'oa', 'approval', 'detail', '--instance-id', approval['processInstanceId'], '--format', 'json'], text=True, timeout=30))['result']
        except Exception:
            status['failed'] += 1
            save(job, status)
            continue
        if approval.get('businessId') in approval_records:
            approval_records[approval['businessId']]['status'] = detail.get('status') or approval.get('status')
            approval_records[approval['businessId']]['result'] = detail.get('processInstanceResult')
        # 审批中立即识别附件；只有已完成且通过的审批可投递表格同步。
        # 已完成但拒绝、已撤销的审批都不继续导入。
        if not should_import_attachments(detail):
            status['rejected_approvals'] += 1
            save(job, status)
            print(f"跳过未通过或已撤销的审批单 {approval.get('businessId')} (status={detail.get('status')}, result={detail.get('processInstanceResult')})")
            continue
        owner = approval.get('title', '').split('提交的')[0]
        # 先导入附件、等待后台识别进入终态，再投递台账同步。同步层按 Invoice.id
        # 只追加不更新，若在「已上传/解析中」时抢先写表，空票号和旧状态会永久留下。
        token_cache = {}
        uploaded_ids = set()
        for attachment in attachments_from_detail(detail):
                key = f"{approval['processInstanceId']}:{attachment['fileId']}"
                file_type = str(attachment.get('fileType', '')).lower()
                claim_amount = attachment.get('_approval_claim_amount')
                status['total'] += 1
                if key in imported:
                    status['skipped'] += 1
                    save(job, status)
                    continue
                if file_type in ARCHIVE_TYPES:
                    # 压缩包里往往就是正经发票（高铁票、瑞幸发票…），解包后逐张导入，
                    # 否则这些单据会被整个漏掉、台账对不上。
                    try:
                        archive_bytes = download_attachment(approval['processInstanceId'], attachment['fileId'], attachment)
                        status['downloaded'] += 1
                    except Exception as exc:
                        record_failure(status, 'download-archive', attachment.get('fileName'), exc)
                        save(job, status)
                        continue
                    entries, seen = extract_archive(archive_bytes, file_type)
                    if not entries:
                        status['non_invoices'] += 1
                        mark_imported(imported, key)
                        print(f"压缩包内无可导入发票: {attachment.get('fileName')}（条目 {seen}）")
                        save(job, status)
                        continue
                    for inner_name, inner_bytes in entries:
                        inner_key = f'{key}:{inner_name}'
                        if inner_key in imported:
                            status['skipped'] += 1
                            save(job, status)
                            continue
                        try:
                            created = upload_with_retry(token_cache, throttle, inner_bytes,
                                                        Path(inner_name).name, detail, owner, claim_amount)
                            if created.get('id') is not None:
                                uploaded_ids.add(int(created['id']))
                            mark_imported(imported, inner_key)
                            status['recognized'] += 1
                        except Exception as exc:
                            record_failure(status, 'upload-archive-entry', inner_name, exc)
                        save(job, status)
                    # 包本身也标记已处理，避免每分钟重复下载同一个压缩包
                    mark_imported(imported, key)
                    continue
                if file_type not in SUPPORTED_EXTENSIONS:
                    status['non_invoices'] += 1
                    mark_imported(imported, key)
                    save(job, status)
                    continue
                try:
                    data = download_attachment(approval['processInstanceId'], attachment['fileId'], attachment)
                    status['downloaded'] += 1
                    created = upload_with_retry(token_cache, throttle, data, attachment['fileName'],
                                                detail, owner, claim_amount)
                    if created.get('id') is not None:
                        uploaded_ids.add(int(created['id']))
                    mark_imported(imported, key)
                    status['recognized'] += 1
                except Exception as exc:
                    record_failure(status, 'download-or-upload', attachment.get('fileName'), exc)
                save(job, status)

        ready, records = wait_for_approval_records_ready(
            token_cache, str(detail.get('businessId') or '').strip())
        # 上传返回时识别尚未完成；按后台终态分类，不能把凭证计作发票。
        for record in records:
            if not str(record.get('id') or '').isdigit() or int(record['id']) not in uploaded_ids:
                continue
            if record.get('document_type') == 'voucher':
                status['non_invoices'] += 1
            elif record.get('document_type') == 'invoice':
                status['invoices'] += 1
        if should_enqueue_order(detail, ready):
            enqueue_order_job(detail, approval, status)
        elif not ready:
            status['orders_deferred_processing'] += 1
            pending_ids = [record.get('id') for record in records
                           if (str(record.get('status') or '') in PENDING_RECORD_STATUSES
                               and str(record.get('document_type') or '') != 'voucher')]
            if len(status['blocked_samples']) < MAX_FAILURE_SAMPLES:
                status['blocked_samples'].append({
                    'kind': 'source-records-processing',
                    'businessId': detail.get('businessId'),
                    'recordIds': pending_ids,
                    'reason': '后台识别未在等待窗口内完成，本轮不写台账',
                })
        save(job, status)
    # A scanned job can still contain attachments that failed to download or
    # upload. Keep that outcome visible instead of reporting a false success.
    status['status'] = 'completed_with_errors' if status['failed'] else 'completed'
    save(job, status)
    # 审批单索引最后一次落盘（内容没变就不写，省一次 mtime 抖动）
    write_if_changed(APPROVALS, json.dumps(approval_records, ensure_ascii=False))

def write_if_changed(path: Path, text: str) -> bool:
    """内容没变就不写。省 IO，也少一次 mtime 抖动（launchd 正盯在这个目录上）。"""
    try:
        if path.exists() and path.read_text(encoding='utf-8') == text:
            return False
    except Exception:  # noqa: BLE001
        pass
    path.write_text(text, encoding='utf-8')
    return True


#: 闸门 action → status 计数器字段。
#: **加新闸门时必须在这里加一行**，否则计数会落到 `orders_blocked_unknown` 上
#: （不 KeyError，但会没人看得懂）。allow-list 而不是动态拼 `orders_blocked_{kind}`，
#: 是因为拼字符串会把 kind 的拼写错误变成 KeyError，把整轮扫描打断。
COUNTER_FOR_KIND = {
    'already_present': 'orders_already_in_ledger',
    'ledger_off': 'orders_blocked_ledger_off',
    'duplicate': 'orders_blocked_duplicate',
    'no_index': 'orders_blocked_no_index',
    # 早于台账范围起点（2026-06-15）的单。见 ledger_guard.SCOPE_START。
    # 手动全量回扫（lookback_days=365）会捞出大批 2025 老单，就靠这条拦住。
    'out_of_scope': 'orders_blocked_out_of_scope',
}


def ledger_write_gate(detail) -> dict:
    """Return read-only audit hints; never block source-record mirroring.

    Kept for diagnostic scripts that display possible duplicate reimbursements.
    Production synchronization no longer calls this function.
    """
    verdict = ledger_guard.check_order(detail)
    return {
        'action': 'observe',
        'kind': verdict.get('kind') or '',
        'reason': verdict.get('reason') or '',
        'collisions': verdict.get('collisions') or [],
    }


def daily_reconcile_job():
    """今天还没做大窗口对账就返回一个 job，否则 None。

    ⚠️ 为什么必须有这个：hourly 只扫「近 2 小时」。只要 worker 停摆超过 2 小时
    （崩溃 / 未登录 / 机器重启），那段时间提交的单就**永久漏掉**——
    2026-09-23 的 `202609211009000095307` 就是这么漏的：9/20 一个格式非法的
    job 让 worker 一直崩溃，窗口正好被错过，之后再也追不回来。

    成本可控：`run_job` 幂等——附件按 imported.json 跳过、台账 job 按 enqueue
    守卫跳过，已处理过的单基本只是拉一次详情。窗口大小看 RECONCILE_DAYS。
    """
    today = f'{datetime.now(TZ):%Y%m%d}'
    try:
        last = json.loads(RECONCILE_STATE.read_text(encoding='utf-8')).get('last_run_date')
    except Exception:  # noqa: BLE001
        last = None
    if last == today:
        return None
    # 先落盘再干活：万一这轮跑得久、跨了天，也不会当晚重复触发
    write_if_changed(RECONCILE_STATE, json.dumps(
        {'last_run_date': today,
         'started_at': datetime.now(TZ).isoformat(),
         'lookback_days': RECONCILE_DAYS,
         '_说明': '每日对账回填：大窗口扫一遍已通过的单，补上 hourly 窗口漏掉的。'
                  '删掉本文件可强制重跑。'},
        ensure_ascii=False, indent=1))
    return {'id': f'reconcile-{today}',
            'requested_at': datetime.now(TZ).isoformat(),
            'lookback_days': RECONCILE_DAYS}


def historical_reconcile_job():
    """存储权限生效后每周回扫一年，补齐 90 天窗口外的历史附件。"""
    now = datetime.now(TZ)
    try:
        last = datetime.fromisoformat(json.loads(
            HISTORICAL_RECONCILE_STATE.read_text(encoding='utf-8'))['started_at'])
        if now - last < timedelta(days=HISTORICAL_RECONCILE_INTERVAL_DAYS):
            return None
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        for scope in ('Storage.File.Read', 'qyapi_get_member'):
            proc = subprocess.run(
                [DWS, 'dev', 'app', 'permission', 'list', '--unified-app-id', APP_UNIFIED_ID,
                 '--scope-value', scope, '--format', 'json'],
                text=True, capture_output=True, timeout=30,
            )
            payload = json.loads(proc.stdout) if proc.returncode == 0 else {}
            items = (payload.get('data') or {}).get('items') or []
            if not any(item.get('scopeValue') == scope and item.get('authed')
                       for item in items):
                return None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    write_if_changed(HISTORICAL_RECONCILE_STATE, json.dumps({
        'started_at': now.isoformat(), 'lookback_days': HISTORICAL_RECONCILE_DAYS,
    }, ensure_ascii=False))
    return {'id': f'historical-{now:%Y%m%d}',
            'requested_at': now.isoformat(),
            'lookback_days': HISTORICAL_RECONCILE_DAYS}


def job_status(job) -> str:
    path = ROOT / f"status-{job['id']}.json"
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('status') or 'completed'
    except Exception:  # noqa: BLE001
        return 'completed'


def archive_job(path, job):
    """把处理过的 job 文件搬出队列（rejected → cancelled/，其余 → processed/）。

    ⚠️ 这一步是**功能性**的，不是打扫卫生：`main()` 只在「ROOT 里没有
    job-*.json」时才造 hourly / 每日对账任务。job 赖着不走，这两个就永远不跑。

    2026-09-23 实测就是这么把 hourly 停掉的：14:22 一个补导入 job 留下后，
    hourly 从 14:23 起再没运行过。9/20 那次更严重——一个格式非法的 job 常驻，
    让 worker 每隔几秒撞一次同一个异常，死了一整天。
    """
    if path is None or not path.exists():
        return
    target = CANCELLED if job_status(job) == 'rejected' else PROCESSED
    target.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(target / path.name)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 搬不动任务文件 {path.name}: {exc}"
              f" —— 它会一直挡住 hourly 任务，请人工挪到 {target.name}/")


def _main_unlocked():
    ROOT.mkdir(parents=True, exist_ok=True)
    match_requests = list(ROOT.glob('match-*.json'))
    jobs = []
    for path in sorted(ROOT.glob('job-*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except Exception as exc:
            print(f"跳过无法解析的任务文件 {path.name}: {exc}")
            continue
        job['_job_path'] = path      # 记住来源：处理完必须搬走（见 archive_job）
        jobs.append(job)

    current_hour = datetime.now(TZ).strftime('%Y%m%d%H')
    # A pending match must not suppress the scheduled hourly approval scan.
    # When a match file wakes WatchPaths within an hour already scanned, only
    # handle the match; the next hour still gets its regular scan.
    hourly_due = not (ROOT / f'status-hourly-{current_hour}.json').exists()
    if not jobs and (not match_requests or hourly_due):
        # 队列空闲：常规的「近 2 小时」窗口
        jobs = [{'id': f"hourly-{datetime.now(TZ):%Y%m%d%H}", 'requested_at': datetime.now(TZ).isoformat()}]
        # 外加每天一次的大窗口对账回填（补 hourly 窗口漏掉的）
        reconcile = daily_reconcile_job()
        if reconcile:
            jobs.append(reconcile)
            print(f"📋 追加每日对账回填任务：窗口 {RECONCILE_DAYS} 天")
        historical = historical_reconcile_job()
        if historical:
            jobs.append(historical)
            print(f"📋 追加每周历史回扫任务：窗口 {HISTORICAL_RECONCILE_DAYS} 天")

    imported = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    approval_records = json.loads(APPROVALS.read_text()) if APPROVALS.exists() else {}

    # 单个任务失败只标记该任务，不能中断整轮扫描——否则后面所有任务和
    # 末尾的 imported.json 落盘都会被跳过。
    for job in jobs:
        path = job.pop('_job_path', None)
        try:
            run_job(job, imported, approval_records)
        except Exception:
            traceback.print_exc()
            try:
                status = blank_status(job)
                status['status'] = 'failed'
                status['reason'] = 'worker 运行异常，已跳过该任务'
                status['failed'] = 1
                save(job, status)
            except Exception:
                pass
        archive_job(path, job)

    if match_requests:
        from dingtalk_invoice_match import reconcile_requests
        try:
            reconcile_requests(match_requests, login_token({}))
        except Exception as exc:
            print(f'钉钉发票元数据匹配失败，本轮保留待查任务：{exc}')

    write_if_changed(STATE, json.dumps(sorted(imported), ensure_ascii=False))


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    with WORKER_LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('审批附件导入程序已在运行，本次触发跳过')
            return
        try:
            _main_unlocked()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == '__main__':
    main()
