#!/usr/bin/env python3
"""校验钉钉 AI 表格（报销台账）与本地发票库的一致性。

业务规则：发票管理系统是**全量发票池**，但同步到钉钉表 hERWDMS 的**只能是报销数据**。
一行 = 一张已归属某条钉钉「日常报销」审批单的发票。因此校验四件事：

  A. 表内每一行都必须有审批单号        —— 否则是非报销数据混入
  B. 表 ↔ 本地库 一一对应              —— 键、审批单号、归属人、价税合计都要一致
  C. 表内每个审批单在钉钉侧必须是通过   —— 钉钉把「审批未通过」也返回 COMPLETED，
                                          只有 processInstanceResult=agree 才算通过
  D. 本地库里有审批单号的单据是否都已同步（凭证的键是「审批单号-序号」）
  E. 钉钉侧审批结果查不动（登录态过期等）—— 与 A~D 的「数据不一致」分开报，
                                          避免把「没跑完」误判成「数据错」
  F. 同一审批单下出现「发票 + 凭证」同金额配对 —— 疑似同一笔报销入表两次
  G. 审批单在钉钉侧稳定不可读（business error）—— 老单子会被回收，重试无用。
                                          归到这里只做提示，不算不一致、不影响退出码，
                                          否则每天都会刷同一条，巡检就没人看了

退出码：0 = 通过或只剩 G 类提示；1 = 存在数据不一致；
        2 = 数据一致但有 E 类核对失败（本次巡检不算跑完）。
        可直接挂到定时任务里按退出码汇报。

用法：
    python3 verify_dingtalk_sync_consistency.py [--skip-approval-check] [--json 输出.json]
"""
import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from decimal import Decimal

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'


def find_docker():
    """launchd 的 PATH 里没有 /usr/local/bin，定时任务跑时必须自己把 docker 找出来。"""
    for candidate in (os.environ.get('DOCKER'), shutil.which('docker'), '/usr/local/bin/docker',
                      '/Applications/Docker.app/Contents/Resources/bin/docker'):
        if candidate and os.path.exists(candidate):
            return candidate
    return 'docker'


DOCKER = find_docker()
BASE_ID = 'LeBq413JA24E9bjoHzYPeEZg8DOnGvpb'
TABLE_ID = 'hERWDMS'
PROCESS_CODE = 'PROC-FF9942ED-92C0-46BA-9923-3861BBB66AAE'
DB_CONTAINER = 'invoice_db'

FIELD_KEY = 'CLWOBCJ'      # sync_key = 发票号码
FIELD_APPROVAL = 'kZE7D5A'  # 审批单号
FIELD_OWNER = 'B8nE78p'     # 归属人
FIELD_TOTAL = 'zv0Jvma'     # 价税合计


def run_json(cmd, timeout=90, attempts=3, want_key=None):
    """跑 dws 并解析 JSON。

    dws 会间歇性返回错误体（没有预期键）或者干脆空输出——连查 40 多个审批单时
    实测碰到过，表现成 `KeyError: 'data'` 这种让人误以为是数据问题的报错。
    所以统一在这里重试，并把真实返回体截进异常信息，方便一眼看出是「接口抖了」
    还是「数据真出问题」。
    """
    last = ''
    for attempt in range(attempts):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        body = proc.stdout or proc.stderr
        try:
            out = json.loads(body)
        except json.JSONDecodeError:
            last = f'命令未返回 JSON (rc={proc.returncode}): {body[:300]}'
        else:
            if want_key is None or want_key in out:
                return out
            last = f'返回体缺少 {want_key!r}：{json.dumps(out, ensure_ascii=False)[:300]}'
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def fetch_table():
    out = run_json([DWS, 'aitable', 'record', 'query', '--base-id', BASE_ID, '--table-id', TABLE_ID,
                    '--field-ids', ','.join([FIELD_KEY, FIELD_APPROVAL, FIELD_OWNER, FIELD_TOTAL]),
                    '--all', '--limit', '100', '--format', 'json'], timeout=180, want_key='data')
    rows = {}
    for rec in out['data']['records']:
        cells = rec['cells']
        key = str(cells.get(FIELD_KEY) or '').strip()
        rows[key] = {
            'recordId': rec['recordId'],
            'approval': str(cells.get(FIELD_APPROVAL) or '').strip(),
            'owner': str(cells.get(FIELD_OWNER) or '').strip(),
            'total': str(cells.get(FIELD_TOTAL) or '').strip(),
        }
    return rows


def fetch_local():
    sql = ("SELECT id, coalesce(invoice_number,''), coalesce(approval_instance_id,''), "
           "coalesce(owner,''), coalesce(total_with_tax::text,''), status, document_type "
           "FROM invoices ORDER BY id")
    proc = subprocess.run([DOCKER, 'exec', DB_CONTAINER, 'psql', '-U', 'postgres', '-d',
                           'invoice_db', '-t', '-A', '-F', '\x1f', '-c', sql],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f'查询本地库失败: {proc.stderr[:300]}')
    invoices = []
    for line in proc.stdout.splitlines():
        parts = line.split('\x1f')
        if len(parts) < 7 or not parts[0].strip().isdigit():
            continue
        invoices.append({
            'id': int(parts[0]), 'number': parts[1].strip(), 'approval': parts[2].strip(),
            'owner': parts[3].strip(), 'total': parts[4].strip(), 'status': parts[5].strip(),
            'document_type': parts[6].strip(),
        })
    return invoices


def is_business_error(message) -> bool:
    """判断核对失败是不是「钉钉侧稳定不可读」而不是暂时性故障。

    钉钉在这两种情况下都回 `business error: success=false`：
      - 审批单太老、附件被回收（download-url 取不到）
      - 详情/列表接口对该审批单就是不给数据
    这类重试也没用，属于稳定状态；而登录态过期、网络抖动、非 JSON 返回
    才是「这次没查成」，需要报出来重试。
    """
    text = str(message or '')
    return 'business error' in text or 'success=false' in text


def approval_result_cache():
    """按 businessId 查钉钉审批结果；同一天的审批单合并成一次列表查询。

    两个容易踩的点（2026-09-21 巡检时真实踩到）：

    1. `list-by-admin` 的 `--limit` 上限是 20，且**默认只取一页**。某天审批单
       超过 20 条时，目标可能落在第 2 页——不分页就会误报「钉钉侧未查到」，
       看上去像数据问题，其实是查得不全。这里按 nextCursor 翻完为止。
    2. 连查 40 多个审批单时，中间偶尔会有一次调用返回错误体（没有 `result` 键）。
       直接 `body['result']` 会抛 `KeyError('result')`，被外层包成一句毫无信息量的
       `{'error': "'result'"}`。所以这里既重试、也把真实返回体截进错误信息。
    """
    cache = {}

    def call_json(command, attempts=3):
        last = ''
        for attempt in range(attempts):
            proc = subprocess.run(command, capture_output=True, text=True, timeout=90)
            body = proc.stdout or proc.stderr
            try:
                return json.loads(body), ''
            except json.JSONDecodeError:
                last = f'非 JSON 返回 (rc={proc.returncode}): {body[:200]}'
            except Exception as exc:                      # noqa: BLE001
                last = str(exc)
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
        return None, last

    def lookup(business_id):
        if business_id in cache:
            return cache[business_id]
        day = f'{business_id[0:4]}-{business_id[4:6]}-{business_id[6:8]}'
        result = None
        error = ''
        cursor = '0'
        try:
            while True:
                listed, err = call_json(
                    [DWS, 'oa', 'approval', 'list-by-admin', '--process-code', PROCESS_CODE,
                     '--start', f'{day}T00:00:00', '--end', f'{day}T23:59:59',
                     '--cursor', str(cursor), '--limit', '20', '--format', 'json'])
                if listed is None:
                    error = err
                    break
                page = listed.get('result')
                if page is None:
                    error = f'列表接口未返回 result：{json.dumps(listed, ensure_ascii=False)[:200]}'
                    break
                for item in page.get('list', []):
                    if item.get('businessId') == business_id:
                        detail, err = call_json(
                            [DWS, 'oa', 'approval', 'detail', '--instance-id',
                             item['processInstanceId'], '--format', 'json'])
                        if detail is None:
                            error = err
                            break
                        body = detail.get('result')
                        if body is None:
                            error = (f'详情接口未返回 result：'
                                     f'{json.dumps(detail, ensure_ascii=False)[:200]}')
                            break
                        result = {
                            'status': body.get('status'),
                            'result': body.get('processInstanceResult'),
                            'title': body.get('processInstanceTitle'),
                        }
                        break
                if result is not None or error:
                    break
                if not page.get('hasMore'):
                    break
                cursor = page.get('nextCursor')
                if not cursor:
                    break
        except Exception as exc:                          # noqa: BLE001
            error = f'{type(exc).__name__}: {exc}'
        if result is None:
            result = {'error': error or '钉钉侧未查到该审批单'}
        cache[business_id] = result
        return result

    return lookup


def is_standalone_voucher(amount, invoice_amounts, sibling_amounts=()):
    """凭证是「独立支出」还是「某张单据的配套单据」。

    这是 backend/app/services/dingtalk_sync_service.is_standalone_voucher 的镜像——
    两边必须同规则，否则巡检会把「按规则本该跳过的凭证」误报成没同步（D 类）。
    改动其中一处时务必同步改另一处。

    sibling_amounts = 同审批单下**更早取得序号的独立凭证**金额。必须一起比对：
    同一个审批单下两张凭证也可能同额（付款截图 + 账单截图都是 82.40），
    那种情况同样只该进一行。
    """
    if amount in (None, ''):
        return False
    try:
        target = Decimal(str(amount))
    except Exception:
        return False
    for candidate in itertools.chain(invoice_amounts, sibling_amounts):
        if candidate in (None, ''):
            continue
        try:
            if Decimal(str(candidate)) == target:
                return False
        except Exception:
            continue
    return True


def _amount_in(amount, candidates) -> bool:
    """金额是否命中候选集合——只用来给跳过原因起个准确的说明。"""
    if amount in (None, ''):
        return False
    try:
        target = Decimal(str(amount))
    except Exception:
        return False
    for candidate in candidates:
        if candidate in (None, ''):
            continue
        try:
            if Decimal(str(candidate)) == target:
                return True
        except Exception:
            continue
    return False


def local_sync_keys(invoices):
    """给本地每张发票算出它在钉钉表里**应该**用的唯一键；None = 按规则本就不该进表。

    - 发票：发票号码；
    - 凭证（行程单 / 付款截图等，没有票号）：「审批单号-序号」，
      序号 = 同一审批单、同一 document_type 下按 id 升序、**且是独立支出**的位次；
    - 配套单据（同审批单下已有同额发票，或已被更早的独立凭证占用同额）或
      金额为空的凭证：不进表。
      ⚠️ 「同额」要比对两条来源：发票金额 + 更早取得序号的凭证金额。少了后者，
      同审批单下两张同额凭证（付款截图 + 账单截图）会双双入表，F 类检查就会报警。
    """
    amounts_by_approval = defaultdict(list)
    for inv in invoices:
        if inv['document_type'] == 'invoice':
            amounts_by_approval[inv['approval']].append(inv['total'])

    claimed = defaultdict(list)   # (审批单, document_type) -> 已占号的独立凭证金额
    counters = defaultdict(int)
    skipped = defaultdict(list)
    keys = {}
    for inv in invoices:
        if inv['number']:
            keys[inv['id']] = inv['number']
            continue
        if not inv['approval']:
            keys[inv['id']] = None
            continue
        bucket = (inv['approval'], inv['document_type'])
        if not is_standalone_voucher(inv['total'], amounts_by_approval.get(inv['approval'], []),
                                    claimed[bucket]):
            keys[inv['id']] = None
            if inv['total'] in (None, ''):
                reason = '金额为空'
            elif _amount_in(inv['total'], amounts_by_approval.get(inv['approval'], [])):
                reason = '同审批单下已有同额发票（配套单据）'
            else:
                reason = '同审批单下已有同额独立凭证（重复支出）'
            skipped[reason].append(inv['id'])
            continue
        counters[bucket] += 1
        claimed[bucket].append(inv['total'])
        keys[inv['id']] = f"{inv['approval']}-{counters[bucket]}"
    return keys, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-approval-check', action='store_true',
                        help='跳过钉钉侧审批结果核对（离线快速跑）')
    parser.add_argument('--json', dest='json_out', help='把结果写成 JSON')
    args = parser.parse_args()

    table = fetch_table()
    invoices = fetch_local()
    problems = {'A_表内缺审批单号': [], 'B_表与库不一致': [], 'C_审批单非通过': [],
                'D_本地未同步': [], 'E_审批核对失败': [], 'F_同审批单同额重复': [],
                'G_审批钉钉侧不可读': []}

    # A. 表内每行都要有审批单号
    for key, row in table.items():
        if not row['approval']:
            problems['A_表内缺审批单号'].append({'发票号码': key, 'recordId': row['recordId']})

    local_by_number = {i['number']: i for i in invoices if i['number']}
    local_reimbursement = [i for i in invoices if i['approval']]
    sync_keys, skipped_vouchers = local_sync_keys(invoices)
    local_by_key = {sync_keys[i['id']]: i for i in invoices if sync_keys.get(i['id'])}

    # B. 表 ↔ 库 双向一一对应
    for key, row in table.items():
        local = local_by_key.get(key)
        if local is None:
            problems['B_表与库不一致'].append({'同步键': key, '问题': '表里有、本地库没有'})
            continue
        label = local['number'] or f"凭证(id={local['id']})"
        if local['approval'] != row['approval']:
            problems['B_表与库不一致'].append({
                '同步键': key, '单据': label, '问题': '审批单号不一致',
                '表': row['approval'], '库': local['approval'], '发票id': local['id']})
        if row['owner'] and local['owner'] and row['owner'] != local['owner']:
            problems['B_表与库不一致'].append({
                '同步键': key, '单据': label, '问题': '归属人不一致',
                '表': row['owner'], '库': local['owner'], '发票id': local['id']})
        try:
            if row['total'] and local['total'] and abs(float(row['total']) - float(local['total'])) > 0.005:
                problems['B_表与库不一致'].append({
                    '同步键': key, '单据': label, '问题': '价税合计不一致',
                    '表': row['total'], '库': local['total'], '发票id': local['id']})
        except ValueError:
            pass

    # D. 本地有审批单号的单据是否都在表里（按规则该进表的那些）
    for local in local_reimbursement:
        expected = sync_keys.get(local['id'])
        if not expected:
            # 配套单据 / 金额为空的凭证：按同步规则本就不该进表，跳过不算问题
            continue
        if expected not in table:
            problems['D_本地未同步'].append({
                '发票id': local['id'], '同步键': expected,
                '类型': local['document_type'], '审批单号': local['approval'],
                '问题': '本地有报销归属但钉钉表里没有'})

    # F. 同一审批单下「发票 + 凭证」金额撞车 —— 疑似同一笔报销记了两行
    #
    # 正常情况下配套单据（行程单/付款截图）会因为「同审批单下已有同额发票」被拦下。
    # 但如果发票因为解析失败暂时没有金额、配套单据先解析出了金额，配套单据就会被误判成
    # 独立支出入表；等发票重解析成功后再入一次 → 同一笔出现两行。
    # 这个检查专门抓这种「两张票号格式不同的行、金额却一样」的配对。
    buckets = defaultdict(list)
    for key, row in table.items():
        if not row['approval'] or not row['total']:
            continue
        try:
            amount = f"{Decimal(row['total']):.2f}"
        except Exception:
            continue
        buckets[(row['approval'], amount)].append(key)
    for (approval, amount), keys in sorted(buckets.items()):
        if len(keys) < 2:
            continue
        synthetic = [k for k in keys if '-' in k]
        if synthetic:
            problems['F_同审批单同额重复'].append({
                '审批单号': approval, '金额': amount, '同步键': sorted(keys),
                '问题': '同一审批单下这些行的价税合计相同且含凭证行，疑似同一笔报销入表两次'})

    # C. 表内出现的审批单在钉钉侧必须是通过
    approvals = sorted({row['approval'] for row in table.values() if row['approval']})
    lookup_failed = 0
    if not args.skip_approval_check:
        lookup = approval_result_cache()
        for business_id in approvals:
            info = lookup(business_id)
            if info.get('error'):
                # 查不动 ≠ 数据错，分两种：
                #  - business error（success=false）：钉钉侧就是不给这个审批单的详情，
                #    重试也白搭（实测和「附件取不到」是同一个信号，老单子会被回收）。
                #    这属于**稳定不可读**，不该每天当成异常刷屏，单独归 G 类。
                #  - 其它（登录态过期 / 网络 / 非 JSON）：可能是暂时的，算「巡检没跑完」，
                #    归 E 类并用退出码 2 区别于「真不一致」。
                if is_business_error(info['error']):
                    problems['G_审批钉钉侧不可读'].append(
                        {'审批单号': business_id, '原因': info['error'][:200]})
                else:
                    lookup_failed += 1
                    problems['E_审批核对失败'].append(
                        {'审批单号': business_id, '原因': info['error']})
            elif info.get('status') != 'COMPLETED' or str(info.get('result') or '').lower() != 'agree':
                problems['C_审批单非通过'].append({
                    '审批单号': business_id, '钉钉状态': info.get('status'),
                    '审批结果': info.get('result'), '问题': '不是「审批通过」，不该出现在台账里'})

    data_problems = sum(len(v) for k, v in problems.items()
                        if k not in ('E_审批核对失败', 'G_审批钉钉侧不可读'))
    total_problems = data_problems + len(problems['E_审批核对失败'])
    print(f'钉钉表 {len(table)} 行   本地库 {len(invoices)} 张（其中带审批单号 {len(local_reimbursement)} 张）')
    print(f'表内涉及审批单 {len(approvals)} 个')
    for reason, ids in sorted(skipped_vouchers.items()):
        print(f'按规则跳过凭证 {len(ids)} 张（{reason}）：{ids if len(ids) <= 20 else str(ids[:20]) + " …"}')
    print()
    for name in ['A_表内缺审批单号', 'B_表与库不一致', 'C_审批单非通过', 'D_本地未同步',
                 'E_审批核对失败', 'F_同审批单同额重复', 'G_审批钉钉侧不可读']:
        items = problems[name]
        mark = '✅' if not items else '⚠️'
        print(f'{mark} {name}: {len(items)}')
        for item in items[:20]:
            print('     ', json.dumps(item, ensure_ascii=False))
        if len(items) > 20:
            print(f'      ... 另有 {len(items) - 20} 条')
    print()
    # 审批核对只要有查不动的，本次巡检就算「没跑完」——退出码 2 区别于「真不一致(1)」。
    # 内核已经重试 3 次，还能失败就不是抖动，值得报出来而不是当成功。
    incomplete = lookup_failed > 0
    if data_problems == 0 and incomplete:
        verdict = (f'⚠️ 本地与表内数据一致，但钉钉侧有 {lookup_failed}/{len(approvals)} 个审批单核对失败，'
                   f'本次巡检不算跑完（详见 E 类）')
    elif total_problems == 0:
        verdict = '✅ 一致，钉钉表里全部是报销数据'
    else:
        verdict = f'⚠️ 发现 {data_problems} 处数据不一致' + (
            f'，另有 {lookup_failed} 个审批单核对失败' if lookup_failed else '')
    print('结论：' + verdict)
    if args.json_out:
        json.dump({'table_rows': len(table), 'local_invoices': len(invoices),
                   'approvals': approvals, 'data_problems': data_problems,
                   'lookup_failed': lookup_failed, 'incomplete': incomplete,
                   'problems': problems},
                  open(args.json_out, 'w'), ensure_ascii=False, indent=1)
        print(f'明细已写入 {args.json_out}')
    if data_problems:
        return 1
    return 2 if incomplete else 0


if __name__ == '__main__':
    sys.exit(main())
