#!/usr/bin/env python3
"""只读比对：钉钉审批单 → 18 列映射 vs 《报销统计表》表内现状。

用途：
  同步改造前先做一次「全量映射自检」。这一步**不写表**，
  但能验出 90% 的映射 bug——因为表里已有的 51 单是人工填的，
  口径可信（B/D/J/K/M 五列 51/51 与审批单一致）。

对比时已知会「合理不一致」的项，单独归类，不算 bug：
  · A 序号        —— 人工流水号，本来就推不出来
  · C 发票号      —— 从本地票池补的，不是审批单字段
  · **行数多出来** —— 表是「一行一张发票」（2026-09-23 起）：一个明细挂 N 张票
                     就拆成 N 行，所以**表内行数天然 ≥ 明细行数**。
                     判据改成三层：表内行数 < 明细行数 → 报错；
                     行数够但「金额非空」切出来的组数 != 明细行数 → 报错；
                     组数对、行数更多 → 只提示。见 `collapse_detail_groups()`。
  · E–I 明细      —— 14 张「是否有发票=否」的单，表里压根没登记明细行
  · N / R 时间    —— 钉钉表格存日期序列号有 ±1 秒浮点漂移

⚠️ 票号（C 列）**只数有值的行数，不做序列比对**：同一明细内多张票的先后
   人工自己也排得不一致（同额票尤其），拿序列比会天天假报。

用法：
  python3 verify_reimbursement_sheet_mapping.py                 # 全量比对
  python3 verify_reimbursement_sheet_mapping.py --json out.json # 同时落盘
  python3 verify_reimbursement_sheet_mapping.py --only 202609181416000253467
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import (  # noqa: E402
    HEADER, HeaderMismatch, build_order_block, collapse_detail_groups,
    normalize_header_name, resolve_column_map,
)
from dingtalk_sync_worker import (  # noqa: E402
    SOURCE_FILE_HEADER, SOURCE_RECORD_HEADER, SOURCE_STATUS_HEADER,
    SOURCE_TYPE_HEADER,
)

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
NODE = '4lgGw3P8vPbXxd01hZpympmRV5daZ90D'
SHEET_ID = 'kgqie6hm'
PROCESS = 'PROC-FF9942ED-92C0-46BA-9923-3861BBB66AAE'
API = 'http://127.0.0.1:18841/api'
SHANGHAI = timezone(timedelta(hours=8))
_TOKEN_CACHE: dict = {}

RETRIES = 3
RETRY_SLEEP = 2

#: 这些错误是 dws / 钉钉侧抽风，重试有意义；其他（参数错、权限错）重试无用
TRANSIENT_HINTS = ('HSFTimeOutException', 'internalError', 'timeout',
                   'Timeout', 'ServiceUnavailable', 'network')

#: 列下标 → 名称，用于报告
COLUMN_NAMES = {i: name for i, name in enumerate(HEADER)}

#: 逐行比对的列（明细级）
PER_ROW_COMPARE = [4, 5, 6, 7, 8]
#: 单级比对（只对第一行有意义）
ORDER_LEVEL_COMPARE = [1, 3, 9, 10, 11, 12, 13, 14, 15, 16, 17]
#: 不做失败判定的列
INFORMATIONAL = [0, 2]
#: ±1 秒漂移可以接受的列
SECOND_TOLERANT = [13, 17]
#: 金额列（语义下标）—— 按**数值**比，不按字符串比
AMOUNT_COLUMNS = {5, 9}
#: 文本列 —— 比之前**折叠空白**
TEXT_COLUMNS = {4, 6, 7, 8}
#: 「是否有发票」的等价写法：表里三种写法并存（照搬原值写入，别统一）。
#: 这里只做**语义等价**判定，不改表。
FLAG_EQUIVALENT = {'有': '是', '是': '是', '无': '否', '否': '否', '': ''}


def _amount_value(text: str):
    """金额文本 → Decimal。取不到返回 None（那样就只能按字符串比）。"""
    cleaned = (text or '').replace(',', '').replace('¥', '').replace('￥', '').strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def same_value(col: int, sheet_value: str, mapped: str) -> str:
    """判断一格「表内值 vs 映射值」是否算一致。返回 'same' / 'equivalent' / 'different'。

    'same'       字符串完全相同
    'equivalent' 写法不同但**语义相同**（千分位、多余空格、有/是）→ 只记提示，不算问题
    'different'  真的不一致 → 报问题

    ⚠️ 为什么要做语义比较，而不是直接 `==`：
      表里的人工填写会带上**无语义的格式**，直接比会天天假报。实测 2026-09-23：
        · 金额  表 `5,000.00` / `3,446.00` vs 映射 `5000` / `3446`（千分位 + 两位小数）
        · 文本  表 `ai 包月接入 CPA  共享…` vs 映射 `… CPA 共享…`（多一个空格）
        · 有票  表 `有` vs 映射 `是`（三种写法并存，见 FLAG_EQUIVALENT）
      这样归一化**不会**把真差异调哑：金额变成数值比，其实比字符串比更严。
    """
    sheet_value = (sheet_value or '').strip()
    mapped = (mapped or '').strip()
    if sheet_value == mapped:
        return 'same'
    equivalent = False
    if col in AMOUNT_COLUMNS:
        left, right = _amount_value(sheet_value), _amount_value(mapped)
        equivalent = left is not None and right is not None and left == right
    elif col in TEXT_COLUMNS:
        equivalent = (' '.join(sheet_value.split()) == ' '.join(mapped.split()))
    elif col == 1:
        equivalent = (FLAG_EQUIVALENT.get(sheet_value, sheet_value)
                      == FLAG_EQUIVALENT.get(mapped, mapped))
    return 'equivalent' if equivalent else 'different'


def call_json(args: list[str]):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            out = subprocess.check_output([DWS, *args], text=True, timeout=60)
        except subprocess.CalledProcessError as exc:
            last = f'退出码 {exc.returncode}: {(exc.output or "")[:200]}'
        except Exception as exc:  # noqa: BLE001
            last = f'{type(exc).__name__}: {exc}'
        else:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                last = f'非 JSON: {out[:200]}'
            else:
                if data.get('error'):
                    blob = json.dumps(data['error'], ensure_ascii=False)
                    # dws 会间歇性抽风（HSFTimeOutException / internalError），
                    # 这类重试有意义；参数错、权限错就立刻返回，别白等。
                    if any(hint in blob for hint in TRANSIENT_HINTS):
                        last = blob[:300]
                    else:
                        return None, blob[:300]
                else:
                    return data, None
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)
    return None, last


def read_sheet() -> tuple[list[list[str]], list[list[str]], dict[str, int]]:
    """读全表，并同时返回语义行、原始行、技术列位置。"""
    data, err = call_json(['sheet', 'range', 'read', '--node', NODE,
                           '--sheet-id', SHEET_ID, '--range', 'A1:Z10000', '--format', 'json'])
    if data is None:
        raise RuntimeError(f'读表失败: {err}')
    raw_rows = []
    for r in data['cells']:
        line = []
        for c in r:
            v = c.get('value') if isinstance(c, dict) else c
            line.append('' if v is None else str(v))
        raw_rows.append(line)
    if not raw_rows:
        raise RuntimeError('读表返回空')
    col_map = resolve_column_map(raw_rows[0])
    header_lookup = {
        normalize_header_name(value): index
        for index, value in enumerate(raw_rows[0])
        if normalize_header_name(value)
    }
    technical_headers = (
        SOURCE_RECORD_HEADER, SOURCE_TYPE_HEADER, SOURCE_FILE_HEADER, SOURCE_STATUS_HEADER,
    )
    technical_cols = {
        name: header_lookup.get(normalize_header_name(name)) for name in technical_headers
    }
    missing = [name for name, col in technical_cols.items() if col is None]
    if missing:
        raise HeaderMismatch(f'缺少技术列 {"、".join(missing)}')
    rows = []
    for line in raw_rows:
        semantic = [''] * len(HEADER)
        for index, col in col_map.items():
            if col < len(line):
                semantic[index] = line[col]
        rows.append(semantic)
    return rows, raw_rows, technical_cols


def group_sheet_orders(rows: list[list[str]], raw_rows: list[list[str]],
                       technical_cols: dict[str, int]) -> list[dict]:
    """按唯一审批号聚合，旧人工块与后台镜像行分开保存。"""
    orders: dict[str, dict] = {}
    current_business_id = ''
    for index, (line, raw_line) in enumerate(zip(rows[1:], raw_rows[1:]), start=2):
        business_id = line[12].strip()
        if business_id:
            current_business_id = business_id
        if not current_business_id or not any(str(x).strip() for x in raw_line):
            continue
        order = orders.setdefault(current_business_id, {
            'business_id': current_business_id,
            'row_start': index,
            'all_lines': [],
            'legacy_blocks': [],
            'mirror_rows': [],
        })
        order['all_lines'].append((index, line))
        source_value = str(raw_line[technical_cols[SOURCE_RECORD_HEADER]] or '').strip()
        source_type = str(raw_line[technical_cols[SOURCE_TYPE_HEADER]] or '').strip()
        source_file = str(raw_line[technical_cols[SOURCE_FILE_HEADER]] or '').strip()
        source_status = str(raw_line[technical_cols[SOURCE_STATUS_HEADER]] or '').strip()
        if source_value or source_type:
            order['mirror_rows'].append({
                'row': index,
                'semantic': line,
                'source_id': int(source_value) if source_value.isdigit() else None,
                'document_type': source_type,
                'file_name': source_file,
                'status': source_status,
            })
            continue
        if business_id or not order['legacy_blocks']:
            order['legacy_blocks'].append({'row_start': index, 'lines': []})
        order['legacy_blocks'][-1]['lines'].append((index, line))
    return list(orders.values())


def _token() -> str:
    if 'token' not in _TOKEN_CACHE:
        request = urllib.request.Request(
            f'{API}/auth/login',
            data=json.dumps({'username': 'admin', 'password': 'qwe123'}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            _TOKEN_CACHE['token'] = json.loads(response.read())['access_token']
    return _TOKEN_CACHE['token']


def pool_records(business_id: str) -> list[dict] | None:
    items: list[dict] = []
    page = 1
    try:
        while True:
            request = urllib.request.Request(
                f'{API}/invoices/sync-records?approval_number='
                f'{urllib.parse.quote(business_id)}&page={page}&page_size=100',
                headers={'Authorization': f'Bearer {_token()}'})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
            batch = body.get('items') or []
            items.extend(batch)
            if not batch or len(items) >= (body.get('total') or 0):
                return items
            page += 1
    except Exception:  # noqa: BLE001
        return None


def compare_mirror_rows(order: dict) -> dict:
    """新版后台镜像只按稳定源记录字段核验，不与审批明细强行逐项比较。"""
    business_id = order['business_id']
    backend = pool_records(business_id)
    if backend is None:
        return {'business_id': business_id, 'mode': 'source-record-mirror',
                'error': '后台源记录查询失败'}
    backend_by_id = {int(item['id']): item for item in backend}
    sheet_by_id: dict[int, list[dict]] = defaultdict(list)
    approval_only = []
    invalid_rows = []
    for row in order['mirror_rows']:
        if row['document_type'] == 'approval-only':
            approval_only.append(row['row'])
        elif row['source_id'] is None:
            invalid_rows.append(row['row'])
        else:
            sheet_by_id[row['source_id']].append(row)

    issues = []
    if invalid_rows:
        issues.append({'col': SOURCE_RECORD_HEADER, 'rows': invalid_rows,
                       'note': '镜像行缺少数字源记录ID'})
    for source_id, mirror_rows in sheet_by_id.items():
        if len(mirror_rows) > 1:
            issues.append({'source_id': source_id,
                           'rows': [row['row'] for row in mirror_rows],
                           'note': '同一源记录ID在表内重复'})
    missing = sorted(set(backend_by_id) - set(sheet_by_id))
    extra = sorted(set(sheet_by_id) - set(backend_by_id))
    if missing:
        issues.append({'col': SOURCE_RECORD_HEADER, 'missing': missing,
                       'note': '后台源记录未镜像'})
    if extra:
        issues.append({'col': SOURCE_RECORD_HEADER, 'extra': extra,
                       'note': '表内源记录后台不存在'})

    field_map = {
        'document_type': 'document_type',
        'file_name': 'file_name',
        'status': 'status',
    }
    for source_id in sorted(set(backend_by_id) & set(sheet_by_id)):
        backend_row = backend_by_id[source_id]
        sheet_row = sheet_by_id[source_id][0]
        for sheet_key, backend_key in field_map.items():
            left = str(sheet_row.get(sheet_key) or '').strip()
            right = str(backend_row.get(backend_key) or '').strip()
            if left != right:
                issues.append({'source_id': source_id, 'row': sheet_row['row'],
                               'col': sheet_key, 'sheet': left, 'backend': right})

        # invoice 镜像行必须展示该张发票自己的票面含税金额，不能把整单
        # approval_claim_amount 复制到每一张发票。voucher 没有稳定票面金额，
        # 仍按审批认领金额展示，因此这里只强校验 invoice。
        if str(backend_row.get('document_type') or '').strip().lower() == 'invoice':
            sheet_amount = _amount_value(sheet_row['semantic'][5])
            backend_amount = _amount_value(str(backend_row.get('total_with_tax') or ''))
            if sheet_amount != backend_amount:
                issues.append({
                    'source_id': source_id,
                    'row': sheet_row['row'],
                    'col': '报销金额（元）',
                    'sheet': str(sheet_row['semantic'][5] or '').strip(),
                    'backend': str(backend_row.get('total_with_tax') or '').strip(),
                    'note': '发票镜像行应使用该张发票的票面含税金额',
                })
    if backend_by_id and approval_only:
        issues.append({'rows': approval_only,
                       'note': '后台已有源记录，但表内仍有 approval-only 镜像行'})
    if not backend_by_id and len(approval_only) != 1:
        issues.append({'rows': approval_only,
                       'note': '无后台源记录审批应且只应有一行 approval-only'})
    return {
        'business_id': business_id,
        'mode': 'source-record-mirror',
        'issues': issues,
        'backend_records': len(backend_by_id),
        'sheet_source_records': len(sheet_by_id),
        'approval_only': len(approval_only),
    }


def list_all_approvals(start: datetime, end: datetime) -> dict:
    """按 30 天窗口翻页，返回 {businessId: list_item}。"""
    found: dict[str, dict] = {}
    cursor_start = start
    while cursor_start < end:
        window_end = min(cursor_start + timedelta(days=30), end)
        cursor = None
        while True:
            command = [
                'oa', 'approval', 'list-by-admin', '--process-code', PROCESS,
                '--start', cursor_start.isoformat(timespec='seconds'),
                '--end', window_end.isoformat(timespec='seconds'),
                '--limit', '20', '--format', 'json']
            if cursor is not None:
                command.extend(['--cursor', str(cursor)])
            data, err = call_json(command)
            if data is None:
                print(f'  ⚠️ 列表查询失败 {cursor_start:%m-%d}~{window_end:%m-%d}: {err}', file=sys.stderr)
                break
            body = data.get('result') or {}
            for item in body.get('list') or []:
                if item.get('businessId'):
                    found[item['businessId']] = item
            if not body.get('hasMore'):
                break
            nxt = body.get('nextCursor')
            if nxt in (None, '', cursor):
                break
            cursor = str(nxt)
        cursor_start = window_end
    return found


def _parse_sheet_dt(text: str) -> datetime | None:
    """解析表里 'yyyy/m/d H:MM:SS' 形态的时间。

    ⚠️ 不能用 `datetime.fromisoformat`：它要求月/日补零，
    而表里是 `2026/9/19 15:23:55`，`fromisoformat` 直接抛 ValueError，
    结果 ±1 秒容差永远不生效、每次都误报时间不一致。
    """
    text = (text or '').strip().replace('/', '-')
    if not text:
        return None
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def dates_equal(a: str, b: str) -> bool:
    """时间列对比，容忍 ±1 秒漂移（表格把时间存成日期序列号时的浮点误差）。"""
    if a == b:
        return True
    da = _parse_sheet_dt(a)
    db = _parse_sheet_dt(b)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) <= 1


def compare_order(order: dict, block: dict) -> dict:
    lines = order['lines']
    issues = []
    informational = []

    # 单级列：只看第一行
    first = lines[0][1]
    for col in ORDER_LEVEL_COMPARE:
        sheet_value = first[col].strip()
        mapped = ''
        if col == 1:
            mapped = block['has_invoice']
        elif col == 3:
            mapped = block['company']
        elif col == 9:
            mapped = block['total']
        elif col == 10:
            mapped = block['total_upper']
        elif col == 11:
            mapped = block['recipient']
        elif col == 12:
            mapped = block['business_id']
        elif col == 13:
            mapped = block['created_at']
        elif col == 14:
            mapped = block['creator']
        elif col == 15:
            mapped = block['result']
        elif col == 16:
            mapped = block['status']
        elif col == 17:
            mapped = block['finished_at']
        if col in SECOND_TOLERANT and dates_equal(sheet_value, mapped):
            continue
        verdict = same_value(col, sheet_value, mapped)
        if verdict == 'different':
            issues.append({'col': COLUMN_NAMES[col], 'row': lines[0][0],
                           'sheet': sheet_value, 'mapped': mapped})
        elif verdict == 'equivalent':
            informational.append({'col': COLUMN_NAMES[col], 'row': lines[0][0],
                                  'sheet': sheet_value, 'mapped': mapped,
                                  'note': '写法不同但语义相同'})

    # ── 明细行数 / 分组 ────────────────────────────────────────────────
    # 表已是「一行一张发票」形态，所以**表内行数天然 ≥ 审批单明细行数**，
    # 拿两者直接比会满屏假报（2026-09-23 之前就是这么报的：53 单报 50 处）。
    # 正确的判据分三层：
    #   ① 表内行数 **少于** 明细行数 → 真有明细整块没登记 → 报警
    #   ② 行数够，但按「金额非空」切出来的**分组数 ≠ 明细行数** → 报警
    #   ③ 分组数对、行数更多 → 就是拆行，记一条提示
    groups = collapse_detail_groups(lines)
    if len(lines) < block['row_count']:
        issues.append({'col': '明细行数', 'row': lines[0][0], 'sheet': len(lines),
                       'mapped': block['row_count'],
                       'note': '表内行数少于审批单明细行数 → 有明细整块没登记'})
    elif len(groups) != block['row_count']:
        issues.append({'col': '明细分组数', 'row': lines[0][0],
                       'sheet': len(groups), 'mapped': block['row_count'],
                       'note': '按「报销金额非空」切出来的组数与明细行数不符'})
    elif len(lines) > block['row_count']:
        informational.append({
            'col': '拆行', 'row': lines[0][0], 'sheet': f'{len(lines)} 行',
            'note': f'{block["row_count"]} 个明细拆成 {len(lines)} 行（一行一张发票）'})

    # ── 逐明细组比对（E–I），按组序对齐 ────────────────────────────────
    for index, group in enumerate(groups):
        if index >= len(block['rows']):
            break
        row_no, line = group['lines'][0]
        mapped_row = block['rows'][index]
        for col in PER_ROW_COMPARE:
            sheet_value = line[col].strip()
            mapped = mapped_row.get(HEADER[col], '').strip()
            verdict = same_value(col, sheet_value, mapped)
            if verdict == 'different':
                issues.append({'col': COLUMN_NAMES[col], 'row': row_no,
                               'sheet': sheet_value, 'mapped': mapped})
            elif verdict == 'equivalent':
                informational.append({'col': COLUMN_NAMES[col], 'row': row_no,
                                      'sheet': sheet_value, 'mapped': mapped,
                                      'note': '写法不同但语义相同'})

    # C 发票号只作参考：来源是票池，审批单表单里多半没有这一列。
    # ⚠️ 且**同一明细内多张票的先后顺序无规则**（同额票谁前谁后都对），
    #    所以这里只数「有值行数」，不做序列比对。
    sheet_c = [line[2].strip() for _n, line in lines]
    if any(sheet_c):
        informational.append({'col': '发票号', 'rows': len([c for c in sheet_c if c]),
                              'note': '表内有值（来自票池回填），审批单表单里没有这一列'})

    return {'business_id': order['business_id'], 'issues': issues,
            'informational': informational, 'row_count_sheet': len(lines),
            'row_count_mapped': block['row_count'], 'group_count': len(groups)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', help='把完整结果写到这个路径')
    ap.add_argument('--only', help='只比对一个审批编号')
    args = ap.parse_args()

    print('读取表内现状…')
    try:
        rows, raw_rows, technical_cols = read_sheet()
    except HeaderMismatch as exc:
        print('❌ 表头对不上，无法比对（不给出结论）：')
        print(f'   {exc}')
        return 2
    orders = group_sheet_orders(rows, raw_rows, technical_cols)
    print(f'  表内 {len(orders)} 个唯一审批编号 / '
          f'{sum(len(o["all_lines"]) for o in orders)} 行')
    if args.only:
        orders = [o for o in orders if o['business_id'] == args.only]
        if not orders:
            print(f'表里没有审批编号 {args.only}', file=sys.stderr)
            return 1

    # ⚠️ 窗口要用**全量最早/最晚**，不能拿 orders[-1] / orders[0] 当端点。
    # 这里原来写着「表是按创建时间倒序的」——**那个假设 2026-09-23 起就不成立了**：
    # 人工在表底追加了序号 66 那份副本（`202608081550000152912`，8 月 8 日），
    # 而它上面一行是 6 月 15 日的单 → `orders[-1]` 变成 8/8，
    # 查询窗口被压成「8/7 ~ 9/23」，**26 张 7~8 月的单全被误判成「列表里没有」**
    # （实测表现：53 单里 27 单找不到）。端点一律从全部单里取 min/max。
    # 旧人工审批块只存在于全量镜像开始前的表头区域；新版镜像行即使同一审批号
    # 连续多行，也不能继续按审批明细模型比较。
    legacy_orders = [o for o in orders if o['legacy_blocks']]
    approvals = {}
    if legacy_orders:
        days = sorted(datetime.strptime(o['business_id'][:8], '%Y%m%d') for o in legacy_orders)
        first_day, last_day = days[0], days[-1]
        print(f'只为旧人工块拉审批列表 {first_day:%Y-%m-%d} ~ '
              f'{last_day + timedelta(days=1):%Y-%m-%d} …')
        approvals = list_all_approvals(first_day - timedelta(days=1), last_day + timedelta(days=2))
        print(f'  审批列表拿到 {len(approvals)} 张')

    results = []
    total_issues = 0
    for index, order in enumerate(orders, start=1):
        bid = order['business_id']
        order_results = []

        mirror_result = compare_mirror_rows(order)
        order_results.append(mirror_result)

        # 旧人工块仍按审批表单映射核验；新镜像块只按 Invoice.id/技术字段核验。
        if order['legacy_blocks']:
            item = approvals.get(bid)
            if item is None:
                order_results.append({'business_id': bid, 'mode': 'legacy-approval-block',
                                      'error': '审批列表里找不到旧人工块对应审批'})
            else:
                detail, err = call_json(['oa', 'approval', 'detail', '--instance-id',
                                         item['processInstanceId'], '--format', 'json'])
                if detail is None or not detail.get('result'):
                    order_results.append({'business_id': bid, 'mode': 'legacy-approval-block',
                                          'error': f'详情失败: {err}'})
                else:
                    block = build_order_block(detail['result'], item)
                    for legacy in order['legacy_blocks']:
                        legacy_order = {'business_id': bid, 'row_start': legacy['row_start'],
                                        'lines': legacy['lines']}
                        legacy_result = compare_order(legacy_order, block)
                        legacy_result['mode'] = 'legacy-approval-block'
                        order_results.append(legacy_result)

        results.extend(order_results)
        bad = sum(len(result.get('issues') or []) + (1 if result.get('error') else 0)
                  for result in order_results)
        total_issues += bad
        flag = '✅' if bad == 0 else f'❌ {bad} 处'
        print(f'  [{index:>3}/{len(orders)}] {bid} '
              f'旧块{len(order["legacy_blocks"])} / 镜像{len(order["mirror_rows"])}行 {flag}')
        for result in order_results:
            if result.get('error'):
                print(f'        ⚠️ {result["mode"]}: {result["error"]}')
            for issue in (result.get('issues') or [])[:6]:
                print('        ❌ ' + json.dumps(issue, ensure_ascii=False))

    clean = sum(1 for order in orders if all(
        not result.get('issues') and not result.get('error')
        for result in results if result.get('business_id') == order['business_id']))
    print()
    print(f'══ 合计：{len(orders)} 个唯一审批编号，完全一致 {clean} 个，问题 {total_issues} 处 ══')

    if args.json:
        Path(args.json).write_text(json.dumps({
            'checked_at': datetime.now(SHANGHAI).isoformat(),
            'orders': len(orders), 'clean': clean, 'issues': total_issues,
            'results': results,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'已写入 {args.json}')

    return 0 if total_issues == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
