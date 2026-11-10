#!/usr/bin/env python3
"""《报销统计表》(axls) 一致性巡检。

2026-09-22 起同步目标从 AI 表格 hERWDMS 换成钉钉在线表格《报销统计表》，
旧巡检（`verify_dingtalk_sync_consistency.py` 七查 A–G）是对着 AI 表格的，
**已停用**（AI 表格不再写，继续查只会天天报「本地未同步」的假警）。

════════════════════════════════════════════════════════════════════════
这一版查什么
════════════════════════════════════════════════════════════════════════
  A 表里有、钉钉已通过列表里没有      → 幽灵行 / 审批编号抄错
  B 钉钉已通过、表里没有              → 漏同步
  C 同一源记录ID在表里出现多次        → 同一后台记录被写了两遍
  D 明细行数 / 明细分组对不上        → 明细缺失（表是「一行一张发票」，见下面 D 类注释）
  E 表里 C 列空、但票池有票号         → 可自动补（仅提示，不影响退出码）
  F 后台源记录（发票）没进表          → 漏镜像
  G 表里源记录ID在本单后台已不存在     → 多余行 / 记录被删 / 行挂错块
  H 同一审批编号内同一发票号多行      → 重复登记（财务口径的「重复登了」，见 H 类注释）
  I 逐行列（源记录ID/发票号/金额…）被纵向合并 → 身份列要被吞掉，见 I 类注释

退出码：0 通过 / 1 数据不一致 / 2 数据一致但核对没跑完（拉不到审批单）

用法：
  python3 verify_reimbursement_sheet_consistency.py
  python3 verify_reimbursement_sheet_consistency.py --since 2026-06-15 --json <结果.json>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import (  # noqa: E402
    HEADER, PER_ROW_COLUMNS, SEM_INDEX, HeaderMismatch, build_detail_rows,
    collapse_detail_groups, normalize_header_name, resolve_column_map,
)
from dingtalk_sync_worker import (  # noqa: E402
    SOURCE_FILE_HEADER, SOURCE_RECORD_HEADER, SOURCE_STATUS_HEADER,
    SOURCE_TYPE_HEADER, is_ledger_record, read_sheet_cells,
)

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
NODE = '4lgGw3P8vPbXxd01hZpympmRV5daZ90D'
SHEET_ID = 'kgqie6hm'
PROCESS = 'PROC-FF9942ED-92C0-46BA-9923-3861BBB66AAE'
API = 'http://127.0.0.1:18841/api'
SHANGHAI = timezone(timedelta(hours=8))

#: 读全表：列读到 Z（表列顺序可能被人工调整过，别只读 A–R）。
#: 不能卡在 1000 行；生产表已在 2026-10-08 突破该行数。
READ_RANGE = 'A1:Z10000'

#: 表里第 1 张单的审批编号日期（序号 2 是 2026-06-15），全量核对默认从这里开始
DEFAULT_SINCE = '2026-06-15'
#: 已确认、暂不处理的老缺口清单（不计入退出码）
DEFAULT_KNOWN_PATH = Path('/Volumes/D 盘/Codex/发票管理系统/audit/consistency/known_issues.json')
RETRIES = 3
RETRY_SLEEP = 2
TRANSIENT_HINTS = ('HSFTimeOutException', 'internalError', 'timeout',
                   'Timeout', 'ServiceUnavailable', 'network')
_TOKEN_CACHE: dict = {}


def call_json(args: list[str], timeout: int = 60):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            out = subprocess.check_output([DWS, *args], text=True, timeout=timeout)
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
                    if any(h in blob for h in TRANSIENT_HINTS):
                        last = blob[:300]
                    else:
                        return None, blob[:300]
                else:
                    return data, None
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)
    return None, last


def _token() -> str:
    if 'token' not in _TOKEN_CACHE:
        request = urllib.request.Request(
            f'{API}/auth/login',
            data=json.dumps({'username': 'admin', 'password': 'qwe123'}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            _TOKEN_CACHE['token'] = json.loads(response.read())['access_token']
    return _TOKEN_CACHE['token']


def pool_invoices(business_id: str) -> list[dict] | None:
    """后台该审批的全部源记录；不按票号或 document_type 过滤。"""
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


def read_sheet_orders() -> tuple[list[dict], int]:
    """读全表，按**表头解析出来的列映射**归一化成语义行，再按单分组。

    ⚠️ **不许按固定列位取值**：表列顺序被人调整过（2026-09-23「审批编号」
    从 M 列被拖到 B 列），硬编码下标读到的就成了「大写金额」，会报出
    几十条假的 A/B/C 类。这里改成：先用 `resolve_column_map` 解析表头，
    把每行**重排成语义顺序**（= mapper 的 HEADER 顺序），下游一律用语义下标。
    表头对不上就抛 HeaderMismatch，宁可报错也不给错结论。
    """
    raw_rows: list[list[str]] = []
    for r in read_sheet_cells():
        line = []
        for c in r:
            v = c.get('value') if isinstance(c, dict) else c
            line.append('' if v is None else str(v))
        raw_rows.append(line)
    if not raw_rows:
        raise RuntimeError('读表返回空')

    col_map = resolve_column_map(raw_rows[0])
    width = len(HEADER)
    rows: list[list[str]] = []
    for line in raw_rows:
        semantic = [''] * width
        for index, col in col_map.items():
            if col < len(line):
                semantic[index] = line[col]
        rows.append(semantic)

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

    # 一张审批现在可能同时存在两种块：
    #   1) 旧人工审批明细块（技术列全空，审批编号通常只在首行）
    #   2) 新后台源记录镜像块（每行都有审批编号、源记录ID/单据类型）
    # 不能再用「审批编号非空就新建订单」：那会把一张审批的 N 条镜像误拆成 N 张单。
    orders_by_id: dict[str, dict] = {}
    current_business_id = ''
    last_row = 1
    for raw_line, line, index in zip(raw_rows[1:], rows[1:], range(2, len(rows) + 1)):
        if any(str(x).strip() for x in raw_line):
            last_row = index
        business_id = line[12].strip()
        if business_id:
            current_business_id = business_id
        if not current_business_id or not any(str(x).strip() for x in raw_line):
            continue

        order = orders_by_id.setdefault(current_business_id, {
            'business_id': current_business_id,
            'first_row': index,
            'all_lines': [],
            'legacy_blocks': [],
            'mirror_lines': [],
            'source_record_ids': set(),
            'source_record_id_rows': defaultdict(list),
            'invoice_number_rows': defaultdict(list),
            'approval_only_rows': [],
        })
        order['all_lines'].append((index, line))

        source_value = str(raw_line[technical_cols[SOURCE_RECORD_HEADER]] or '').strip()
        source_type = str(raw_line[technical_cols[SOURCE_TYPE_HEADER]] or '').strip()
        is_mirror = bool(source_value or source_type)
        if is_mirror:
            order['mirror_lines'].append((index, line))
            if source_type == 'approval-only':
                order['approval_only_rows'].append(index)
            if source_value.isdigit():
                source_id = int(source_value)
                order['source_record_ids'].add(source_id)
                order['source_record_id_rows'][source_id].append(index)
            invoice_no = line[SEM_INDEX['发票号']].strip()
            if invoice_no:
                order['invoice_number_rows'][invoice_no].append(
                    {'row': index, 'source_id': source_value, 'type': source_type})
            continue

        # 旧人工块仍按「审批编号首行 + 后续纵向合并行」分块，只供 D/E 明细检查。
        if business_id or not order['legacy_blocks']:
            order['legacy_blocks'].append({'first_row': index, 'lines': []})
        order['legacy_blocks'][-1]['lines'].append((index, line))

    return list(orders_by_id.values()), last_row


def list_approved(start: datetime, end: datetime):
    """返回 {businessId: list_item}，只保留审批通过(agree)的。

    注意 list-by-admin 对**驳回**的单也返回 status=COMPLETED，
    只有 detail.processInstanceResult 能区分，所以必须逐个拉 detail 判定。
    """
    approved: dict[str, dict] = {}
    inspect_failed: list[str] = []
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
                inspect_failed.append(f'列表 {cursor_start:%m-%d}~{window_end:%m-%d}: {err}')
                break
            body = data.get('result') or {}
            for item in body.get('list') or []:
                bid = item.get('businessId')
                if not bid or item.get('status') != 'COMPLETED':
                    continue
                detail, derr = call_json(['oa', 'approval', 'detail', '--instance-id',
                                          item['processInstanceId'], '--format', 'json'])
                if detail is None or not detail.get('result'):
                    inspect_failed.append(f'详情 {bid}: {derr}')
                    continue
                body_detail = detail['result']
                if str(body_detail.get('processInstanceResult') or '').lower() != 'agree':
                    continue
                approved[bid] = {'list_item': item, 'detail': body_detail}
            if not body.get('hasMore'):
                break
            nxt = body.get('nextCursor')
            if nxt in (None, '', cursor):
                break
            cursor = str(nxt)
        cursor_start = window_end
    return approved, inspect_failed


def _column_index(letters: str) -> int:
    out = 0
    for ch in letters:
        out = out * 26 + (ord(ch) - 64)
    return out - 1


#: 逐行列：这些列一旦被纵向合并，非块首行的值就会被吞成空。
#: 2026-11-07 的事故就是这么来的——「源记录ID」按审批块合并，599 个 ID 被吞，
#: 同步 worker 认不出这些单已经登记过，把 46 张单在表尾整单重写了一遍。
PER_ROW_HEADERS = ([HEADER[i] for i in PER_ROW_COLUMNS]
                   + [SOURCE_RECORD_HEADER, SOURCE_TYPE_HEADER,
                      SOURCE_FILE_HEADER, SOURCE_STATUS_HEADER])


def merged_per_row_columns() -> tuple[list[dict], str]:
    """被纵向合并的逐行列（只报事实，不改表）。返回 (越界合并列表, 错误说明)。"""
    info_data, info_err = call_json(['sheet', 'info', '--node', NODE, '--sheet-id', SHEET_ID])
    if info_data is None:
        return [], f'读取合并区域失败: {info_err}'
    header_data, header_err = call_json(['sheet', 'range', 'read', '--node', NODE,
                                        '--sheet-id', SHEET_ID, '--range', 'A1:Z1'])
    if header_data is None:
        return [], f'读取表头失败: {header_err}'
    header_rows = header_data.get('cells') or []
    if not header_rows:
        return [], '表头读取为空'

    def text_of(cell) -> str:
        value = cell.get('value') if isinstance(cell, dict) else cell
        return '' if value is None else str(value).strip()

    position = {}
    for index, cell in enumerate(header_rows[0]):
        name = normalize_header_name(text_of(cell))
        if name and name not in position:
            position[name] = index
    offenders = []
    for rng in ((info_data.get('data') or info_data).get('mergedRanges') or []):
        match = re.match(r'^([A-Z]+)(\d+):([A-Z]+)(\d+)$', str(rng))
        if not match:
            continue
        start_col, end_col = _column_index(match.group(1)), _column_index(match.group(3))
        start_row, end_row = int(match.group(2)), int(match.group(4))
        if end_row <= start_row or start_col != end_col:
            continue  # 只看单列纵向合并
        name = next((h for h, i in position.items() if i == start_col), '')
        if name in PER_ROW_HEADERS:
            offenders.append({'列': name, '范围': rng, '被吞行数': end_row - start_row})
    return offenders, ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default=DEFAULT_SINCE, help='核对起始日期 yyyy-mm-dd')
    ap.add_argument('--json', help='结果落盘路径')
    ap.add_argument('--check-pool', action='store_true',
                    help='同时查 E 类（表里 C 列空但票池有票）——多一批 HTTP 请求')
    ap.add_argument('--known', help=f'已知问题豁免清单路径（默认 {DEFAULT_KNOWN_PATH}）')
    ap.add_argument('--expected-json',
                    help='可选：以预检 JSON 的 approved_ids 作为本次覆盖范围（全量历史终检用）')
    args = ap.parse_args()

    problems: dict[str, list] = {'A_表里有钉钉没有': [], 'B_钉钉有表里没有': [],
                                 'C_表内重复审批编号': [], 'D_明细行数不一致': [],
                                 'E_可补发票号': [], 'F_后台源记录未镜像': [],
                                 'G_表内多余源记录': [], 'H_同审批同发票号重复登记': [],
                                 'I_逐行列被纵向合并': [],
                                 'G2_表内凭证行留档（不计异常）': []}
    inspect_failed: list[str] = []

    print('读取表内现状…')
    try:
        sheet_orders, last_row = read_sheet_orders()
    except HeaderMismatch as exc:
        print('❌ 表头对不上，无法核对（不给出结论，避免误报）：')
        print(f'   {exc}')
        print('   表格列被人改过名或删过列？先核对表头再重跑。')
        return 2
    print(f'  表内 {len(sheet_orders)} 个唯一审批编号 / '
          f'{sum(len(o["all_lines"]) for o in sheet_orders)} 行，末行 {last_row}')

    since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=SHANGHAI)
    now = datetime.now(SHANGHAI) + timedelta(days=1)
    expected_ids: set[str] | None = None
    if args.expected_json:
        expected_body = json.loads(Path(args.expected_json).read_text(encoding='utf-8'))
        expected_ids = {str(value).strip() for value in expected_body.get('approved_ids') or []
                        if str(value).strip()}
        approved = {business_id: {'list_item': {}, 'detail': {}}
                    for business_id in expected_ids}
        failed = []
        print(f'使用预检清单核对全量历史审批：{len(approved)} 张')
    else:
        print(f'拉钉钉已通过审批单 {since:%Y-%m-%d} ~ {now:%Y-%m-%d} …')
        approved, failed = list_approved(since, now)
        inspect_failed.extend(failed)
        print(f'  已通过 {len(approved)} 张（核对失败 {len(failed)} 项）')

    sheet_ids = [o['business_id'] for o in sheet_orders]
    sheet_id_set = set(sheet_ids)

    # A 类：表里有、钉钉没有
    for order in sheet_orders:
        bid = order['business_id']
        if bid in approved:
            continue
        # 早于核对窗口的单不判（可能是窗口外，不是幽灵）
        try:
            day = datetime.strptime(bid[:8], '%Y%m%d').replace(tzinfo=SHANGHAI)
        except ValueError:
            problems['A_表里有钉钉没有'].append(
                {'审批编号': bid, '行': order['first_row'], '原因': '审批编号格式非法'})
            continue
        if day < since:
            continue
        problems['A_表里有钉钉没有'].append(
            {'审批编号': bid, '行': order['first_row'], '原因': '钉钉已通过列表里找不到（驳回单不该在表里？）'})

    # B 类：钉钉有、表里没有
    for bid in approved:
        if bid not in sheet_id_set:
            problems['B_钉钉有表里没有'].append(
                {'审批编号': bid, '日期': f'{bid[:4]}-{bid[4:6]}-{bid[6:8]}',
                 '标题': approved[bid].get('list_item', {}).get('title', '')})

    # C 类：技术唯一键重复。2026-09-29 起同一审批编号允许存在旧人工块 + 新镜像块，
    # 审批编号多块共存是全量迁移设计，不再算错误；真正不能重复的是 Invoice.id。
    for order in sheet_orders:
        duplicates = {
            source_id: rows for source_id, rows in order['source_record_id_rows'].items()
            if len(rows) > 1
        }
        if duplicates:
            problems['C_表内重复审批编号'].append({
                '审批编号': order['business_id'],
                '问题': '同一源记录ID在表内重复',
                '重复源记录': duplicates,
            })

    # D 类：明细行数 / 明细分组
    # ⚠️ 表是「一行一张发票」（2026-09-23 晨哥定的口径）：一个明细挂 N 张票就拆成 N 行，
    #    所以 **表内行数 ≥ 审批单明细行数**，直接比两者会天天报一堆假警
    #    （实测这么比会报 12 条，绝大多数只是「拆行」造成的口径差异）。
    #    判据改成三层，只有前两层算问题：
    #      ① 表内行数 **少于** 明细行数           → 真有明细整块没登记
    #      ② 「报销金额非空」切出的**组数** ≠ 明细行数 → 分组对不上
    #      ③ 组数对、行数更多                     → 就是拆行，不算问题
    for order in sheet_orders:
        entry = approved.get(order['business_id'])
        if entry is None:
            continue
        if not entry.get('detail'):
            continue
        expected_rows = build_detail_rows(entry['detail'])
        legacy_blocks = order.get('legacy_blocks') or []
        if not legacy_blocks:
            # 只有后台源记录镜像的审批不套用审批明细分组规则；镜像一行对应一条 Invoice。
            continue
        for legacy in legacy_blocks:
            actual = len(legacy['lines'])
            groups = collapse_detail_groups(legacy['lines'])
            if actual < len(expected_rows):
                problems['D_明细行数不一致'].append(
                    {'审批编号': order['business_id'], '起始行': legacy['first_row'],
                     '表内行数': actual, '审批单明细行数': len(expected_rows),
                     '问题': '旧人工块行数少于明细行数 → 有明细整块没登记'})
                continue
            if len(groups) != len(expected_rows):
                problems['D_明细行数不一致'].append(
                    {'审批编号': order['business_id'], '起始行': legacy['first_row'],
                     '表内明细组数': len(groups), '审批单明细行数': len(expected_rows),
                     '问题': '旧人工块按「报销金额非空」切出的组数与审批明细数不符'})
                continue
            for offset, group in enumerate(groups):
                if offset >= len(expected_rows):
                    break
                row_no, line = group['lines'][0]
                expected = expected_rows[offset]
                blank = not line[4].strip() and not line[5].strip()
                has_data = bool(expected.get('项目名称') or expected.get('报销金额（元）'))
                if blank and has_data:
                    problems['D_明细行数不一致'].append(
                        {'审批编号': order['business_id'], '行': row_no,
                         '问题': '旧人工块明细为空但审批单有值',
                         '审批单应为': {'项目名称': expected.get('项目名称'),
                                        '报销金额（元）': expected.get('报销金额（元）')}})

    # E 类（可选）：表里 C 列空但票池有票
    pool_cache: dict[str, list[dict] | None] = {}

    def records_for(business_id: str) -> list[dict] | None:
        if business_id not in pool_cache:
            pool_cache[business_id] = pool_invoices(business_id)
        return pool_cache[business_id]

    if args.check_pool:
        for order in sheet_orders:
            legacy_lines = [line for block in order.get('legacy_blocks') or [] for line in block['lines']]
            blank_rows = [row for row, line in legacy_lines
                          if not line[2].strip() and line[5].strip()]
            if not blank_rows:
                continue
            items = records_for(order['business_id'])
            if items is None:
                inspect_failed.append(f'票池查询失败 {order["business_id"]}')
                continue
            numbers = [i.get('invoice_number') for i in items if i.get('invoice_number')]
            if numbers:
                problems['E_可补发票号'].append(
                    {'审批编号': order['business_id'], '旧人工块空行': blank_rows,
                     '池内票号数': len(numbers)})

    # F/G：严格按后台 Invoice.id 对账。业务重复（同票号/同金额）不算技术错误。
    for order in sheet_orders:
        items = records_for(order['business_id'])
        if items is None:
            inspect_failed.append(f'源记录查询失败 {order["business_id"]}')
            continue
        # 凭证只留档不进台账（见同步 worker 的 is_ledger_record），不能算 F 类缺口。
        backend_ids = {int(item['id']) for item in items if is_ledger_record(item)}
        # G 也只认「这条 ID 在本单的后台记录里根本找不到」——记录被删了、
        # 或行挂到了别的审批块下。表里的凭证行是历史留档（凭证行的 ID 属于本单，
        # 只是 document_type 不是发票），2026-11-07 那 81 条 / 191 个 ID 全是这种，
        # 混进 G 就把真异常（基线 2 条）淹没了，所以单独列一条不计异常。
        all_backend_ids = {int(item['id']) for item in items}
        sheet_source_ids = set(order.get('source_record_ids') or set())
        missing = sorted(backend_ids - sheet_source_ids)
        voucher_rows = sorted(sheet_source_ids & (all_backend_ids - backend_ids))
        extra = sorted(sheet_source_ids - all_backend_ids)
        if missing:
            problems['F_后台源记录未镜像'].append(
                {'审批编号': order['business_id'], '缺少源记录ID': missing,
                 '后台记录数': len(backend_ids), '表内源记录数': len(sheet_source_ids)})
        if voucher_rows:
            problems['G2_表内凭证行留档（不计异常）'].append(
                {'审批编号': order['business_id'], '凭证源记录ID': voucher_rows})
        if extra:
            problems['G_表内多余源记录'].append(
                {'审批编号': order['business_id'], '多余源记录ID': extra})

    # H 类：同一审批编号内，同一发票号登记了多行。
    # C 类只查「同一源记录ID 重复」，抓不到这种形态：一张票被两条不同的后台记录
    # （同一附件重复上传，或手动上传 + 定时导入各一份）各登记一次，
    # 财务 2026-11 反馈的 202610151406000003502 / 202610281706000420279 就是这么来的。
    for order in sheet_orders:
        repeated = {no: rows for no, rows in order['invoice_number_rows'].items()
                    if len({str(r['source_id']) for r in rows}) > 1}
        if repeated:
            problems['H_同审批同发票号重复登记'].append({
                '审批编号': order['business_id'],
                '重复发票号': {no: [r['row'] for r in rows] for no, rows in repeated.items()},
                '明细': [r for rows in repeated.values() for r in rows],
            })

    # I 类：逐行列被纵向合并 —— 身份列即将被吞掉的信号。
    # 2026-11-07 那次「源记录ID」被按审批块合并，599 个 ID 一次消失，
    # C/F/G/H 全被带偏，同步 worker 还把 46 张单整单重写到表尾。
    # 按审批块合并本身是财务给块级列排版的手势，只有落在逐行列上才算事故。
    merged_offenders, merged_err = merged_per_row_columns()
    if merged_err:
        inspect_failed.append(merged_err)
    for item in merged_offenders:
        problems['I_逐行列被纵向合并'].append(item)

    data_problems = sum(len(problems[k]) for k in
                        ('A_表里有钉钉没有', 'B_钉钉有表里没有',
                         'C_表内重复审批编号', 'D_明细行数不一致',
                         'F_后台源记录未镜像', 'G_表内多余源记录',
                         'H_同审批同发票号重复登记', 'I_逐行列被纵向合并'))

    # 已知问题豁免：把「已确认、暂不处理」的老缺口摘出去单独报，
    # 不计入退出码。否则每天刷同一条，巡检就会被人忽略——
    # 巡检一旦变成噪音，真正的新闻也会被跳过。
    known_applied: dict[str, list] = {}
    known_path = Path(args.known) if args.known else DEFAULT_KNOWN_PATH
    if known_path.exists():
        try:
            known = json.loads(known_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            known = {}
        for category, ids in (known.get('ignore') or {}).items():
            if category not in problems:
                continue
            id_set = set(ids or [])
            kept, hit = [], []
            for item in problems[category]:
                key = item.get('审批编号') or item.get('业务键')
                if key in id_set:
                    hit.append(item)
                else:
                    kept.append(item)
            problems[category] = kept
            if hit:
                known_applied[category] = hit
        data_problems = sum(len(problems[k]) for k in
                           ('A_表里有钉钉没有', 'B_钉钉有表里没有',
                            'C_表内重复审批编号', 'D_明细行数不一致',
                            'F_后台源记录未镜像', 'G_表内多余源记录',
                            'H_同审批同发票号重复登记', 'I_逐行列被纵向合并'))
        known_total = sum(len(v) for v in known_applied.values())
        if known_total:
            print(f'ℹ️  已知问题豁免 {known_total} 条（见 {known_path.name}，不计入退出码）')
            for category, items in known_applied.items():
                print(f'    {category}：{len(items)} 条')

    lookup_failed = len(inspect_failed)

    print()
    for name, items in problems.items():
        if items:
            print(f'{name}：{len(items)} 条')
            # 「不计异常」的信息类只给个样例，别把真正的 A–I 类结论淹在刷屏里
            for item in items[:3 if '不计异常' in name else 10]:
                print('   ', json.dumps(item, ensure_ascii=False))
    if not data_problems:
        print('✅ 数据一致：A/B/C/D/F/G/H 七类均为 0')
    if problems['E_可补发票号']:
        print(f'ℹ️  E 类提示：{len(problems["E_可补发票号"])} 张单的 C 列可补（不影响退出码）')
    if inspect_failed:
        print(f'⚠️  核对失败 {lookup_failed} 项（属「没跑完」，不是「数据错」）')
        for item in inspect_failed[:5]:
            print('   ', item)

    result = {
        'checked_at': datetime.now(SHANGHAI).isoformat(),
        'sheet': {
            'orders': len(sheet_orders),
            'unique_business_ids': len(sheet_orders),
            'data_rows': sum(len(o['all_lines']) for o in sheet_orders),
            'source_record_ids': sum(len(o['source_record_ids']) for o in sheet_orders),
            'approval_only': sum(len(o['approval_only_rows']) for o in sheet_orders),
            'rows': last_row,
            'sheet_id': SHEET_ID,
        },
        'approved_in_dingtalk': len(approved),
        'since': args.since,
        'data_problems': data_problems,
        'lookup_failed': lookup_failed,
        'problems': problems,
        'known_problems': known_applied,
        'inspect_failed': inspect_failed,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'\n已写入 {args.json}')

    if data_problems:
        return 1
    if lookup_failed:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
