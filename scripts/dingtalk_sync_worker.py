#!/usr/bin/env python3
"""宿主机队列 worker：把「日常报销审批单」写进钉钉在线表格《报销统计表》(axls)。

2026-09-22 改造：目标从 **AI 表格 hERWDMS** 换成 **在线电子表格《报销统计表》**。
旧的 aitable 版本备份在 `dingtalk_sync_worker.py.bak-aitable-20260922`。

════════════════════════════════════════════════════════════════════════
为什么不是「改个 ID」
════════════════════════════════════════════════════════════════════════
两套目标的模型根本不同：

  AI 表格（旧）                《报销统计表》（新）
  ────────────────────────    ────────────────────────
  1 张发票 = 1 行              1 张审批单 = N 行明细
  票号做唯一键，可 upsert      **没有 key 列**，只能按 M 列查重后追加
  来源是 invoices 表           来源是**审批单表单**（E–I/K/L 库里没有）
  record-upsert-by-key         sheet range update 块写 + merge-cells

所以：幂等自己做（M 列查重），位置自己算（追加到末尾），
单级的 12 列写完还要做纵向合并。

════════════════════════════════════════════════════════════════════════
队列协议
════════════════════════════════════════════════════════════════════════
`dingtalk-sync-queue/order-<businessId>.json`：

  {
    "kind": "reimbursement_order",
    "business_id": "202609181416000253467",
    "block": { ...dingtalk_reimbursement_mapper.build_order_block()... },
    "invoice_numbers_by_row": ["2631...", ""],   # C 列逐行（可缺省）
    "queued_at": "2026-09-22T15:30:00+08:00"
  }

处理完移入 `completed/`（成功）或 `failed/`（失败）。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import (  # noqa: E402
    SEM_INDEX, HeaderMismatch, as_text, col_letter, fmt_date, normalize_amount,
    normalize_header_name, resolve_column_map,
)

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
NODE = '4lgGw3P8vPbXxd01hZpympmRV5daZ90D'
SHEET_ID = 'kgqie6hm'
API = 'http://127.0.0.1:18841/api'
QUEUE = Path('/Volumes/D 盘/Codex/发票管理系统/dingtalk-sync-queue')
COMPLETED = QUEUE / 'completed'
FAILED = QUEUE / 'failed'
SHANGHAI = timezone(timedelta(hours=8))

#: 读全表：列读到 Z（表列顺序可能被人工调整过，别只读 A–R）。
#: 行上限必须留足余量；2026-10-08 表突破 1000 行后，旧范围会漏读新行并导致重复追加。
READ_RANGE = 'A1:Z10000'
#: 单次 range read 会被服务端**按单元格总数**截断（实测 26 列 × 1153 行就到顶，
#: 返回 hasMore=true / truncationReasons=['max_cells']）。所以必须分块读到尾部，
#: 否则 last_row 偏小 → 已存在的单被当成新单重复追加。
READ_CHUNK_ROWS = 800
LOCK_FILE = QUEUE / '.dingtalk-sync-worker.lock'
RETRIES = 3
RETRY_SLEEP = 2
TRANSIENT_HINTS = ('HSFTimeOutException', 'internalError', 'timeout',
                   'Timeout', 'ServiceUnavailable', 'network')
PENDING_RECORD_STATUSES = {'已上传', '解析中'}


def is_ledger_record(record: dict) -> bool:
    """凭证（非发票附件）只用于备份查看，不进《报销统计表》。"""
    return str(record.get('document_type') or '').strip().lower() != 'voucher'

#: 语义列名（列顺序一律按表头解析，见 resolve_column_map）
SEM_SERIAL = SEM_INDEX['序号']
SEM_INVOICE_NO = SEM_INDEX['发票号']
SEM_PROJECT = SEM_INDEX['项目名称']
SEM_AMOUNT = SEM_INDEX['报销金额（元）']
SEM_TOTAL = SEM_INDEX['合计金额（元）']
SEM_UPPER = SEM_INDEX['合计金额（元）（大写）']
SEM_BUSINESS_ID = SEM_INDEX['审批编号']
SOURCE_RECORD_HEADER = '源记录ID'
SOURCE_TYPE_HEADER = '单据类型'
SOURCE_FILE_HEADER = '源文件名'
SOURCE_STATUS_HEADER = '后台状态'
SOURCE_EXTRA_HEADERS = [
    SOURCE_RECORD_HEADER, SOURCE_TYPE_HEADER, SOURCE_FILE_HEADER, SOURCE_STATUS_HEADER,
]


# ── dws 调用 ────────────────────────────────────────────────────────────

def call_json(args: list[str], timeout: int = 60):
    """调 dws 并解析 JSON。抽风类错误重试，参数/权限错立刻返回。"""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            out = subprocess.check_output([DWS, *args], text=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            last = f'退出码 {exc.returncode}: {(exc.output or "")[:300]}'
        except Exception as exc:  # noqa: BLE001
            last = f'{type(exc).__name__}: {exc}'
        else:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                last = f'非 JSON 输出: {out[:300]}'
            else:
                if data.get('error'):
                    blob = json.dumps(data['error'], ensure_ascii=False)
                    if any(hint in blob for hint in TRANSIENT_HINTS):
                        last = blob[:300]
                    else:
                        return None, blob[:300]
                else:
                    return data, None
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)
    return None, last


# ── 票池（C 列发票号的来源） ────────────────────────────────────────────

#: 单个 worker 进程内复用登录 token
_TOKEN_CACHE: dict = {}


def login_token() -> str:
    if 'token' not in _TOKEN_CACHE:
        import urllib.request
        request = urllib.request.Request(
            f'{API}/auth/login',
            data=json.dumps({'username': 'admin', 'password': 'qwe123'}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            _TOKEN_CACHE['token'] = json.loads(response.read())['access_token']
    return _TOKEN_CACHE['token']


def fetch_pool_invoices(business_id: str) -> list[dict]:
    """按审批编号读取后台全部源记录，不做业务过滤。

    同步层只做镜像：invoice / voucher、空票号、重复票号都原样返回。
    技术幂等唯一依据是后台 ``Invoice.id``，不是发票号或审批编号。
    """
    import urllib.error
    import urllib.request
    items: list[dict] = []
    page = 1
    try:
        while True:
            import urllib.parse
            url = (f'{API}/invoices/sync-records?approval_number='
                   f'{urllib.parse.quote(business_id)}&page={page}&page_size=100')
            request = urllib.request.Request(
                url, headers={'Authorization': f'Bearer {login_token()}'})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
            batch = body.get('items') or []
            items.extend(batch)
            total = body.get('total') or 0
            if not batch or len(items) >= total or len(items) >= 1000:
                break
            page += 1
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        print(f'取票池失败（C 列将留空）business={business_id}: {exc}', file=sys.stderr)
        return []
    return sorted(items, key=lambda item: int(item.get('id') or 0))


# ── 表格状态 ────────────────────────────────────────────────────────────

def _row_values(row) -> list[str]:
    out = []
    for cell in row:
        value = cell.get('value') if isinstance(cell, dict) else cell
        out.append('' if value is None else str(value))
    return out


def _resolve_extra_columns(header: list[str]) -> dict[str, int]:
    lookup = {
        normalize_header_name(value): index
        for index, value in enumerate(header)
        if normalize_header_name(value)
    }
    missing = [name for name in SOURCE_EXTRA_HEADERS
               if normalize_header_name(name) not in lookup]
    if missing:
        raise HeaderMismatch(
            f'原始镜像同步缺少技术列：{"、".join(missing)}。'
            f'请在空白列 S–V 建立：{" / ".join(SOURCE_EXTRA_HEADERS)}')
    return {name: lookup[normalize_header_name(name)] for name in SOURCE_EXTRA_HEADERS}


def read_sheet_cells() -> list[list[dict]]:
    """分块读全表（A→Z），返回**按绝对行号连续**的 cells，行 1 是表头。

    ⚠️ 单次 `range read` 会被服务端按单元格总数截断：实测 A1:Z10000 只回约
    1153 行（hasMore=true / truncationReasons=['max_cells']）。表突破该行数后
    一次读会静默漏掉尾部 → last_row 偏小 → 已存在的单被当成新单重复追加。
    """
    cells: list[list[dict]] = []
    start = 1
    for _ in range(40):  # 上限兜底：800 行 × 40 = 3.2 万行，远超台账规模
        end = start + READ_CHUNK_ROWS - 1
        data, err = call_json(['sheet', 'range', 'read', '--node', NODE,
                               '--sheet-id', SHEET_ID,
                               '--range', f'A{start}:Z{end}', '--format', 'json'],
                               timeout=180)
        if data is None:
            raise RuntimeError(f'读表失败（不写表，本轮直接放弃）: {err}')
        chunk = data.get('cells') or []
        if not chunk:
            return cells
        cells.extend(chunk)
        last_returned = (data['rowIndices'][-1] if data.get('rowIndices') else end)
        non_empty = any((c.get('value') if isinstance(c, dict) else c)
                        for row in chunk for c in row)
        if not non_empty and not data.get('hasMore'):
            return cells
        start = last_returned + 1
    raise RuntimeError(f'读表超过 40 块仍未到表尾（已读 {len(cells)} 行）')


def read_sheet_state() -> dict:
    """读全表，算出源记录 ID、最后一行、最大序号和列映射。

    每次运行只读一次（不是每个 job 读一次）——表在变大，读一次几秒钟。

    ⚠️ **列位置一律按表头解析**（`resolve_column_map`），不许用固定下标：
    表列顺序被人改过（2026-09-23：审批编号从 M 挪到 B），硬编码下标会
    查重读错列 → 判成新单 → 重复写入 + 串位。表头对不上就抛 HeaderMismatch，
    本轮直接放弃、绝不写表。
    """
    cells = read_sheet_cells()
    rows = [_row_values(r) for r in cells]
    if not rows:
        raise RuntimeError('读表返回空（不写表，本轮直接放弃）')

    col_map = resolve_column_map(rows[0])
    extra_cols = _resolve_extra_columns(rows[0])
    col_count = max(len(rows[0]), max(col_map.values()) + 1, max(extra_cols.values()) + 1)
    semantic_cols = sorted(col_map.values())

    def cell(line: list[str], semantic: int) -> str:
        col = col_map[semantic]
        return line[col].strip() if col < len(line) else ''

    known: set[str] = set()
    serial_by_business_id: dict[str, int] = {}
    source_record_ids_by_business_id: dict[str, set[int]] = {}
    blank_id_rows_by_business_id: dict[str, int] = {}
    line_keys_by_business_id: dict[str, dict[str, set[str]]] = {}
    initialized_business_ids: set[str] = set()
    current_serial = 0
    current_business_id = ''
    known_source_record_ids: set[int] = set()
    file_col = extra_cols[SOURCE_FILE_HEADER]
    for line in rows[1:]:
        serial_text = cell(line, SEM_SERIAL)
        if serial_text.isdigit():
            current_serial = int(serial_text)
        business_id = cell(line, SEM_BUSINESS_ID)
        if business_id:
            current_business_id = business_id
            known.add(business_id)
            source_record_ids_by_business_id.setdefault(business_id, set())
            line_keys_by_business_id.setdefault(
                business_id, {'invoice_no': set(), 'file_name': set()})
            if current_serial:
                serial_by_business_id.setdefault(business_id, current_serial)
        source_col = extra_cols[SOURCE_RECORD_HEADER]
        source_value = line[source_col].strip() if source_col < len(line) else ''
        source_type_col = extra_cols[SOURCE_TYPE_HEADER]
        source_type = line[source_type_col].strip() if source_type_col < len(line) else ''
        if current_business_id and source_type == 'approval-only':
            initialized_business_ids.add(current_business_id)
        if source_value.isdigit():
            source_id = int(source_value)
            known_source_record_ids.add(source_id)
            if current_business_id:
                source_record_ids_by_business_id.setdefault(current_business_id, set()).add(source_id)
                initialized_business_ids.add(current_business_id)
        elif current_business_id and any(cell(line, sem) for sem in col_map):
            # 表里有行、行上没 ID —— 身份列被人工合并/删列吞掉的块。
            # 2026-11-07 就是这种块让 worker 误判「还没登记」，把 46 张单整单追加到表尾。
            blank_id_rows_by_business_id[current_business_id] = \
                blank_id_rows_by_business_id.get(current_business_id, 0) + 1
        if current_business_id:
            keys = line_keys_by_business_id[current_business_id]
            invoice_no = cell(line, SEM_INVOICE_NO)
            file_name = line[file_col].strip() if file_col < len(line) else ''
            if invoice_no:
                keys['invoice_no'].add(invoice_no)
            if file_name:
                keys['file_name'].add(file_name)

    # 明细指纹保留给只读人工核对工具；不再参与同步拦截。
    # 给自动补台账前的「疑似重复报销」闸门用（见 ledger_guard.py）——
    # 顺便在这一次读表里算出来，不自成另一次读表。
    fingerprints: dict[str, set] = {}
    current = None
    for line in rows[1:]:
        business_id = cell(line, SEM_BUSINESS_ID)
        if business_id:
            current = business_id
            fingerprints.setdefault(business_id, set())
        if current is None:
            continue
        project = cell(line, SEM_PROJECT)
        amount = cell(line, SEM_AMOUNT)
        if project or amount:
            fingerprints[current].add((project, amount))

    last_row = 1
    for index, line in enumerate(rows, start=1):
        if any(cell(line, sem) for sem in col_map):
            last_row = index

    max_serial = 0
    for line in rows[1:]:
        first = cell(line, SEM_SERIAL)
        if first.isdigit():
            max_serial = max(max_serial, int(first))

    return {'known_business_ids': known,
            'initialized_business_ids': initialized_business_ids,
            'serial_by_business_id': serial_by_business_id,
            'source_record_ids_by_business_id': source_record_ids_by_business_id,
            'blank_id_rows_by_business_id': blank_id_rows_by_business_id,
            'line_keys_by_business_id': line_keys_by_business_id,
            'known_source_record_ids': known_source_record_ids,
            'last_row': last_row,
            'max_serial': max_serial, 'row_count': len(rows),
            'col_map': col_map, 'extra_cols': extra_cols, 'col_count': col_count,
            'line_fingerprints': fingerprints,
            'header': [str(x).strip() for x in rows[0]],
            'semantic_cols': semantic_cols}


# ── 写一行块 ────────────────────────────────────────────────────────────

def _cells(line: list[str]) -> list[dict]:
    """一行值 → dws 的 cell 对象数组。空值用 {} 跳过（保留原值=空）。"""
    out = []
    for value in line:
        text = '' if value is None else str(value)
        out.append({'type': 'text', 'text': text} if text.strip() else {})
    return out


def _remap_row(values: list[str], col_map: dict[int, int], col_count: int) -> list[str]:
    """语义顺序的 18 个值 → 按表内实际列序摆放。

    例：语义「审批编号」是第 13 个值，若表里审批编号在 B 列，就摆到 index 1。
    """
    width = max(col_count, max(col_map.values()) + 1, len(values))
    out = [''] * width
    for semantic, col in col_map.items():
        if semantic < len(values):
            out[col] = values[semantic]
    return out


def _to_cell_row(values: list[str]) -> list[dict]:
    """裸值行 → dws 要求的 cell object 行。

    `dws sheet range update --help` 明确规定每个 cell 必须是
    `{"type":"text","text":"..."}` / `{"type":"richText",...}` / `{}`；
    **裸字符串 / 数字会被参数校验直接拒收**：

        --values[0][0]: 不支持原始值 67，每个单元格必须是 object（如 {"type":"text","text":"..."}）

    ⚠️ 空值这里写 `{"type":"text","text":""}`（显式写空），**不用 `{}`**。
    `{}` 的语义是「跳过该格、保留原值」；本 worker 虽是往表**末尾追加**新块，
    但万一目标行有残留（人工插行等），`{}` 会把旧值留下来，显式写空更确定。
    """
    return [{'type': 'text', 'text': '' if v is None else str(v)} for v in values]


def _record_semantic_row(record: dict, block: dict, serial: int,
                         include_order_totals: bool) -> list[str]:
    """One backend record becomes exactly one semantic ledger row."""
    line = [''] * len(SEM_INDEX)
    line[SEM_INDEX['序号']] = str(serial)
    line[SEM_INDEX['是否有发票']] = block.get('has_invoice', '')
    line[SEM_INDEX['发票号']] = as_text(record.get('invoice_number'))
    line[SEM_INDEX['公司名称']] = as_text(record.get('buyer_name')) or block.get('company', '')
    line[SEM_INDEX['项目名称']] = as_text(record.get('item_name')) or as_text(record.get('file_name'))
    # 后台镜像行是一行一条源记录：真实发票应展示该张发票自己的票面金额，
    # 不能把整单 approval_claim_amount 复制到每张发票上，否则一单多票会重复显示整单金额。
    # voucher / approval-only 没有可靠票面金额时，才回退到审批认领金额。
    document_type = as_text(record.get('document_type')).strip().lower()
    if document_type == 'invoice' and record.get('total_with_tax') not in (None, ''):
        amount = record.get('total_with_tax')
    else:
        amount = record.get('approval_claim_amount')
        if amount in (None, ''):
            amount = record.get('total_with_tax')
    line[SEM_INDEX['报销金额（元）']] = normalize_amount(amount)
    line[SEM_INDEX['费用发生日期']] = fmt_date(record.get('issue_date'))
    line[SEM_INDEX['费用类型']] = as_text(record.get('document_type'))
    line[SEM_INDEX['费用使用说明']] = as_text(record.get('file_name'))
    if include_order_totals:
        line[SEM_INDEX['合计金额（元）']] = block.get('total', '')
        line[SEM_INDEX['合计金额（元）（大写）']] = block.get('total_upper', '')
    line[SEM_INDEX['收款账户']] = block.get('recipient', '')
    line[SEM_INDEX['审批编号']] = block.get('business_id', '')
    line[SEM_INDEX['创建时间']] = block.get('created_at', '')
    line[SEM_INDEX['创建人']] = block.get('creator', '')
    line[SEM_INDEX['审批结果']] = block.get('result', '')
    line[SEM_INDEX['审批状态']] = block.get('status', '')
    line[SEM_INDEX['完成时间']] = block.get('finished_at', '')
    return line


def write_records(block: dict, records: list[dict], start_row: int, serial: int,
                  col_map: dict[int, int], extra_cols: dict[str, int], col_count: int,
                  include_order_totals: bool, dry_run: bool = False) -> dict:
    """Append source records without merging, deduplication, or business inference."""
    if not records:
        return {'ok': True, 'range': '', 'row_count': 0, 'serial': serial, 'steps': []}
    width = max(col_count, max(col_map.values()) + 1, max(extra_cols.values()) + 1)
    sheet_rows: list[list[str]] = []
    for index, record in enumerate(records):
        semantic = _record_semantic_row(
            record, block, serial, include_order_totals and index == 0)
        row = _remap_row(semantic, col_map, width)
        row[extra_cols[SOURCE_RECORD_HEADER]] = as_text(record.get('id'))
        row[extra_cols[SOURCE_TYPE_HEADER]] = as_text(record.get('document_type'))
        row[extra_cols[SOURCE_FILE_HEADER]] = as_text(record.get('file_name'))
        row[extra_cols[SOURCE_STATUS_HEADER]] = as_text(record.get('status'))
        sheet_rows.append(row)

    end_row = start_row + len(sheet_rows) - 1
    last_letter = col_letter(width - 1)
    write_range = f'A{start_row}:{last_letter}{end_row}'
    cell_rows = [_to_cell_row(row) for row in sheet_rows]
    steps: list[dict] = []
    source_record_ids = [int(r['id']) for r in records if str(r.get('id') or '').isdigit()]
    if dry_run:
        return {'ok': True, 'dry_run': True, 'range': write_range,
                'row_count': len(sheet_rows), 'serial': serial,
                'source_record_ids': source_record_ids,
                'steps': [{'step': 'write', 'range': write_range, 'dry_run': True,
                           'rows': len(sheet_rows), 'payload_preview': cell_rows[:2]}]}

    data, err = call_json([
        'sheet', 'range', 'update', '--node', NODE, '--sheet-id', SHEET_ID,
        '--range', write_range,
        '--values', json.dumps(cell_rows, ensure_ascii=False),
        '--yes', '--format', 'json'])
    if data is None:
        return {'ok': False, 'failed_step': 'write', 'range': write_range, 'error': err}
    steps.append({'step': 'write', 'range': write_range, 'ok': True})

    source_letter = col_letter(extra_cols[SOURCE_RECORD_HEADER])
    verify, verify_err = call_json([
        'sheet', 'range', 'read', '--node', NODE, '--sheet-id', SHEET_ID,
        '--range', f'{source_letter}{start_row}:{source_letter}{end_row}', '--format', 'json'])
    if verify is None:
        return {'ok': False, 'failed_step': 'verify', 'range': write_range,
                'error': f'源记录 ID 回读失败: {verify_err}', 'steps': steps}
    seen = {
        str(cell.get('value') if isinstance(cell, dict) else cell).strip()
        for row in (verify.get('cells') or []) for cell in row
    }
    expected = {str(record['id']) for record in records if str(record.get('id') or '').isdigit()}
    matched = expected <= seen
    steps.append({'step': 'verify', 'ok': matched,
                  'expected_source_ids': sorted(expected), 'found_source_ids': sorted(seen)})
    if not matched:
        return {'ok': False, 'failed_step': 'verify', 'range': write_range,
                'error': f'源记录 ID 回读不完整，缺少 {sorted(expected - seen)}', 'steps': steps}
    return {'ok': True, 'range': write_range, 'row_count': len(sheet_rows),
            'serial': serial, 'source_record_ids': source_record_ids,
            'steps': steps}


# ── 主流程 ──────────────────────────────────────────────────────────────

def _job_paths() -> list[Path]:
    """只认《报销统计表》的订单 job。

    ⚠️ 同一个队列目录里还有历史遗留的 AI 表格 job（`<invoice_id>.json`，
    kind 为空的旧格式）。**这里必须忽略它们**——否则新 worker 会拿表格逻辑
    去处理发票 job，一股脑判失败、把队列搞脏。旧 job 不删不动，留着归档。
    """
    paths = []
    for path in sorted(QUEUE.glob('*.json')):
        if not path.is_file():
            continue
        if path.name.startswith('order-'):
            paths.append(path)
    return paths


def is_order_job(job_path: Path) -> bool:
    try:
        job = json.loads(job_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return False
    return job.get('kind') == 'reimbursement_order' and bool(job.get('block'))


def gate_damaged_identity(unsynced: list[dict], state: dict,
                          business_id: str) -> tuple[str, list[str]]:
    """身份列被吞过的审批块：判断「这条记录在不在表里」不能只认源记录ID。

    返回 `('', [])` 表示照常追加；否则返回跳过原因和表里认不出来的票号。
    判据换成表里的硬内容（发票号 / 源文件名）：
      * 每条待写记录都能在块里找到同内容的行 → 台账已经镜像了它们，只是 ID 被抹了，
        跳过（补 ID 是 repair_ledger_identity_columns.py 的活）；
      * 有找不到的 → 说不清这块到底缺不缺行，整单让路。宁可留一条巡检 F 让人补，
        也不能像 2026-11-07 那样把整单再追加一遍，让财务再看一遍重复行。
    """
    if not state.get('blank_id_rows_by_business_id', {}).get(business_id):
        return '', []
    keys = state.get('line_keys_by_business_id', {}).get(business_id) or {}
    known_no = keys.get('invoice_no') or set()
    known_file = keys.get('file_name') or set()
    unknown = []
    for record in unsynced:
        invoice_no = as_text(record.get('invoice_number'))
        file_name = as_text(record.get('file_name'))
        if (invoice_no and invoice_no in known_no) or (file_name and file_name in known_file):
            continue
        unknown.append(invoice_no or file_name or str(record.get('id')))
    if not unknown:
        return 'source-records-mirrored-without-id', []
    return 'identity-columns-blank-need-relink', unknown


def process(job_path: Path, state: dict, dry_run: bool = False) -> dict:
    job = json.loads(job_path.read_text(encoding='utf-8'))
    business_id = str(job.get('business_id') or '').strip()
    block = job.get('block') or {}
    if not business_id or not block:
        return {'success': False, 'error': 'job 缺少 business_id 或 block'}

    records = fetch_pool_invoices(business_id)
    pending = [record for record in records
               if str(record.get('status') or '') in PENDING_RECORD_STATUSES
               and is_ledger_record(record)]
    if pending:
        return {'success': True, 'deferred': 'source-records-processing',
                'business_id': business_id, 'backend_records': len(records),
                'pending_source_record_ids': [int(record['id']) for record in pending]}

    present_for_order = state['source_record_ids_by_business_id'].get(business_id, set())
    initialized_business_ids = state.get('initialized_business_ids', set())
    legacy_bootstrap = business_id in state['known_business_ids'] and business_id not in initialized_business_ids
    # 晨哥已确认拓展全量台账且重复由人工清洗：存量无源记录 ID 的旧块不再阻断，
    # 首次在表尾追加完整后台镜像，并以新行里的 Invoice.id 建立后续增量幂等。
    # 凭证只留档不进台账：清掉表里的凭证行后，这里必须同样过滤，否则会被重新追加。
    unsynced = [record for record in records
                if is_ledger_record(record)
                and int(record.get('id') or 0) not in present_for_order]
    needs_header_row = not records and business_id not in initialized_business_ids
    if not unsynced and not needs_header_row:
        return {'success': True, 'skipped': 'all-source-records-present',
                'business_id': business_id, 'backend_records': len(records)}

    blocked, unknown = gate_damaged_identity(unsynced, state, business_id)
    if blocked:
        return {'success': True, 'skipped': blocked, 'business_id': business_id,
                'backend_records': len(records),
                'source_records_without_identity_anchor': len(unsynced),
                'content_not_in_sheet': unknown[:20]}

    # 同一审批可分多次增量追加；审批编号存在不再构成跳过理由。
    existing_serial = state['serial_by_business_id'].get(business_id, 0)
    serial = existing_serial or (state['max_serial'] + 1)
    start_row = state['last_row'] + 1
    write_records_list = unsynced
    header_only = False
    if needs_header_row:
        # 无票审批也必须进入全量台账。用一个无源记录ID的审批头行表达「该单已初始化」；
        # 后续若补进真实附件，新的 Invoice.id 仍会继续增量追加。
        write_records_list = [{
            'id': '', 'invoice_number': '', 'buyer_name': block.get('company', ''),
            'item_name': (block.get('rows') or [{}])[0].get('项目名称', ''),
            'approval_claim_amount': (block.get('rows') or [{}])[0].get('报销金额（元）', ''),
            'issue_date': (block.get('rows') or [{}])[0].get('费用发生日期', ''),
            'document_type': 'approval-only', 'file_name': '',
        'status': ('Excel回填·审批接口历史时间戳失效'
                   if block.get('fallback_source') == 'excel-export' else '无后台附件'),
        }]
        header_only = True
    outcome = write_records(
        block, write_records_list, start_row, serial,
        col_map=state['col_map'], extra_cols=state['extra_cols'],
        col_count=state['col_count'],
        include_order_totals=(business_id not in state['known_business_ids'] or legacy_bootstrap),
        dry_run=dry_run)
    if not outcome.get('ok'):
        return {'success': False, 'business_id': business_id, **outcome}

    if not dry_run:
        # 就地推进游标，同一次运行里的后续 job 接着往下排。
        state['known_business_ids'].add(business_id)
        state['serial_by_business_id'].setdefault(business_id, serial)
        written_ids = {int(record['id']) for record in unsynced
                       if str(record.get('id') or '').isdigit()}
        state['known_source_record_ids'].update(written_ids)
        state['source_record_ids_by_business_id'].setdefault(business_id, set()).update(written_ids)
        state.setdefault('initialized_business_ids', set()).add(business_id)
        state['last_row'] = start_row + outcome['row_count'] - 1
        state['max_serial'] = max(state['max_serial'], serial)

    return {'success': True, 'business_id': business_id,
            'serial': serial, 'row_count': outcome['row_count'],
            'range': outcome['range'], 'steps': outcome.get('steps', []),
            'backend_records': len(records),
            'legacy_bootstrap': legacy_bootstrap,
            'header_only': header_only,
            'source_records_written': len(unsynced),
            'source_record_ids': [int(record['id']) for record in unsynced
                                  if str(record.get('id') or '').isdigit()]}


def main() -> int:
    ap = argparse.ArgumentParser(description='报销统计表同步 worker')
    ap.add_argument('--dry-run', action='store_true',
                    help='只算位置、不写表（用于验证 job 与落位）')
    ap.add_argument('--job', help='只处理指定的 job 文件')
    args = ap.parse_args()

    QUEUE.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open('a+')
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # 上一轮仍在运行；launchd 每 10 秒再触发，不能并发读旧游标后重复写表。

    paths = [Path(args.job)] if args.job else _job_paths()
    if not paths:
        return 0  # 队列空，静默退出（launchd 每 10 秒跑一次，别刷日志）

    # 名字像 order-* 但内容不对的（损坏 / 半个文件），直接跳过留给人工，别删
    usable = []
    for job_path in paths:
        if is_order_job(job_path):
            usable.append(job_path)
        else:
            print(f'跳过非《报销统计表》job（不删）：{job_path.name}', file=sys.stderr)
    if not usable:
        return 0

    try:
        state = read_sheet_state()
    except HeaderMismatch as exc:
        print(f'❌ 表格列顺序与预期不符，本轮不写表（避免写错列）：{exc}',
              file=sys.stderr)
        print('   表头若确属人为调整，请同步更新 mapper 的 HEADER，别改回硬编码下标。',
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f'跳过本轮：{exc}', file=sys.stderr)
        return 1

    exit_code = 0
    for job_path in usable:
        try:
            report = process(job_path, state, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            report = {'success': False, 'error': f'{type(exc).__name__}: {exc}'}

        report['processed_at'] = datetime.now(SHANGHAI).isoformat()
        report['job_file'] = job_path.name
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            continue

        # 报告名统一按审批编号走，不跟着源文件名跑
        # （手工用 --job /tmp/xxx.json 试跑时，否则会在 completed 里留下乱七八糟的名字）
        report_name = f"order-{report.get('business_id') or job_path.stem}.json"
        if report.get('deferred'):
            # 后台识别仍在进行：保留原 job 等下一个 10 秒周期，不能把半成品写入表，
            # 也不能当成功搬进 completed 后永远丢掉后续识别结果。
            continue

        target = COMPLETED if report.get('success') else FAILED
        target.mkdir(parents=True, exist_ok=True)
        (target / report_name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if report.get('success'):
            try:
                job_path.unlink()
            except FileNotFoundError:
                # launchd 10 秒实例可能与手工批次同时消费同一个 job；另一实例已经
                # 成功写表并删除时，这里视为幂等成功，不要把 ENOENT 报成失败。
                pass
        else:
            exit_code = 1
            print(f'❌ {job_path.name}: {report.get("error")}', file=sys.stderr)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
