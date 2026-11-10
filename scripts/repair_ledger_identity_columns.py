#!/usr/bin/env python3
"""把台账里丢掉的「源记录ID / 审批编号」补回来——只填空格，不改任何已有值，不删任何行。

2026-11-07 的发现：表被人整体重排过（46 张单的同一个序号被拆成两段），并且
「源记录ID」从 873 行掉到 272 行、「审批编号」只剩 175 张单。这两列是查重、
巡检、同步 worker 存在性判断的唯一依据，列一空：

  * 巡检 F/G/H 全乱（今天 F 从 1 跳到 50）；
  * 同步 worker 认不出已存在的单 → 会把整张单再追加一遍（那 46 段就是这么来的）；
  * 去重脚本看不见没 ID 的行 → 重复登记删不掉。

匹配只认后台的硬事实（审批编号 → 该单的记录），键的强弱依次是
「发票号+源文件名」>「发票号」>「源文件名」>「金额+单据类型」，
而且要求**一个键只对一个候选**——含糊的一律不写，宁可留着让人看。

用法：
  python3 repair_ledger_identity_columns.py                 # dry-run
  python3 repair_ledger_identity_columns.py --apply
  python3 repair_ledger_identity_columns.py --apply --json /tmp/relink.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import col_letter, normalize_header_name  # noqa: E402
from dingtalk_sync_worker import read_sheet_cells  # noqa: E402
from verify_reimbursement_sheet_consistency import (  # noqa: E402
    NODE, SHEET_ID, call_json, pool_invoices,
)

COLUMNS = ['序号', '审批编号', '发票号', '报销金额(元)', '项目名称', '费用发生日期',
           '费用使用说明', '源记录ID', '单据类型', '源文件名', '后台状态', '合计金额(元)']


def normalize_money(value) -> str:
    digits = ''.join(ch for ch in str(value) if (ch.isdigit() or ch == '.'))
    if not digits:
        return ''
    try:
        return f'{float(digits):.2f}'
    except ValueError:
        return digits


def read_rows() -> tuple[list[dict], dict[str, int]]:
    """非空行 → [{'row', 各列文本, 'serial', 'business', 'source_id', ...}]"""
    cells = read_sheet_cells()
    header = [normalize_header_name(text_of(c)) for c in cells[0]]
    positions = {}
    for name in COLUMNS:
        key = normalize_header_name(name)
        if key in header:
            positions[name] = header.index(key)

    rows = []
    for index, cells_row in enumerate(cells[1:], start=2):
        values = [text_of(c) for c in cells_row]
        if not any(v for v in values):
            continue

        def get(name: str) -> str:
            position = positions.get(name)
            return values[position] if position is not None and position < len(values) else ''

        rows.append({'row': index, 'serial': get('序号'), 'business': get('审批编号'),
                     'invoice_no': get('发票号'), 'source_id': get('源记录ID'),
                     'doc_type': get('单据类型'), 'file_name': get('源文件名'),
                     'amount': normalize_money(get('报销金额(元)')),
                     'item': get('项目名称'), 'total': get('合计金额(元)')})
    return rows, positions


def text_of(cell) -> str:
    value = cell.get('value') if isinstance(cell, dict) else cell
    return '' if value is None else str(value).strip()


def candidates(row: dict, records: list[dict]) -> list[dict]:
    """一条表行可能对应哪些后台记录（越强的键先试，命中即止）。"""
    keys = [
        lambda r: bool(row['invoice_no']) and bool(row['file_name'])
        and r['key'] == (row['invoice_no'], row['file_name']),
        lambda r: bool(row['invoice_no']) and r['invoice_number'] == row['invoice_no'],
        lambda r: bool(row['file_name']) and r['file_name'] == row['file_name'],
        lambda r: bool(row['doc_type']) and r['document_type'] == row['doc_type']
        and bool(row['amount']) and r['amount'] == row['amount'],
    ]
    for predicate in keys:
        hits = [r for r in records if predicate(r)]
        if hits:
            return hits
    return []


def fingerprint(row: dict) -> tuple:
    """行指纹：够认出「这两行是同一张票」，弱到不敢乱认的就退回空。"""
    if row['invoice_no']:
        return ('no', row['invoice_no'], row['file_name'])
    if row['file_name']:
        return ('file', row['file_name'], row['amount'])
    return ('', '', '')


def transfer_from_extra_copies(by_serial: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """同一序号被拆成多段时，把后面那段（带 ID 的新副本）的 ID 搬回第一段。

    现状是这样的：46 张单的原块「源记录ID」被清空了，同步 worker 认不出它们
    已经在表里，就在表尾又追加了一份带 ID 的副本。直接补 ID 会让同一个记录挂
    两行，所以先把 ID 搬回原位（只填空格），副本行留着当删除候选——它和原位行
    指的是同一条后台记录，删哪一行都不丢数据。
    """
    writes, skipped = [], []
    for serial, block in by_serial.items():
        clusters = []
        for row in sorted(block, key=lambda r: r['row']):
            if clusters and row['row'] == clusters[-1][-1]['row'] + 1:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        if len(clusters) < 2:
            continue
        canonical = [r for r in clusters[0] if not r['source_id']]
        for extra in clusters[1:]:
            for row in extra:
                if not row['source_id'].isdigit():
                    continue
                match = next((c for c in canonical
                              if fingerprint(c) == fingerprint(row) and fingerprint(row)[0]),
                             None)
                if match is None:
                    skipped.append({'行': row['row'], '序号': serial, '源记录ID': row['source_id'],
                                    '原因': '首段里没有与之同指纹的空 ID 行'})
                    continue
                canonical.remove(match)
                writes.append({'row': match['row'], 'column': '源记录ID',
                               'value': row['source_id'],
                               '依据': f'序号 {serial} 表尾副本第 {row["row"]} 行同一条记录'})
    return writes, skipped


def build_plan(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """返回 (要回写的单元格, 说不清的行, 统计)。"""
    by_serial: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row['serial']:
            by_serial[row['serial']].append(row)

    # 序号 → 审批编号：块里只要有一行还写着审批编号就认得出来
    serial_business: dict[str, str] = {}
    ambiguous: dict[str, set] = {}
    for serial, block in by_serial.items():
        numbers = {r['business'] for r in block if r['business']}
        if len(numbers) == 1:
            serial_business[serial] = numbers.pop()
        elif len(numbers) > 1:
            ambiguous[serial] = numbers

    writes: list[dict] = []
    unclear: list[dict] = []
    stats = {'块数': len(by_serial), '认得出审批编号': len(serial_business),
             '审批编号冲突块': len(ambiguous), '缺源记录ID行': 0, '补回ID行': 0,
             '说不清行': 0, '缺块首审批编号段': 0, '补审批编号段': 0,
             '从表尾副本搬回ID': 0, '后台查询失败块': 0}

    # ① 先把表尾副本手里的 ID 搬回原位（只填空，副本行本身不动）
    transfers, skipped = transfer_from_extra_copies(by_serial)
    writes += transfers
    stats['从表尾副本搬回ID'] = len(transfers)
    unclear += skipped
    moved = {item['row']: item['value'] for item in transfers}

    records_cache: dict[str, list[dict]] = {}
    for serial, block in sorted(by_serial.items(), key=lambda x: min(r['row'] for r in x[1])):
        business_id = serial_business.get(serial, '')
        block.sort(key=lambda r: r['row'])

        # ② 审批编号：每段连续行的首行必须写着它，否则下面的行会挂到上一段
        for start, end in ranges([r['row'] for r in block]):
            head = next((r for r in block if r['row'] == start), None)
            if head is None or head['business']:
                continue
            stats['缺块首审批编号段'] += 1
            if business_id:
                writes.append({'row': start, 'column': '审批编号', 'value': business_id,
                               '依据': f'同序号 {serial} 的其它行写着该审批编号'})
                stats['补审批编号段'] += 1
            else:
                unclear.append({'行': start, '序号': serial,
                                '原因': '整段没有审批编号，且该序号本身冲突'})

        if not business_id:
            continue
        if business_id not in records_cache:
            fetched = pool_invoices(business_id)
            if fetched is None:
                records_cache[business_id] = None
                stats['后台查询失败块'] += 1
                unclear.append({'序号': serial, '审批编号': business_id,
                                '原因': '后台该单记录查询失败，整块跳过'})
            else:
                records_cache[business_id] = [
                    {'id': int(r['id']),
                     'invoice_number': str(r.get('invoice_number') or '').strip(),
                     'file_name': str(r.get('file_name') or '').strip(),
                     'document_type': str(r.get('document_type') or '').strip(),
                     'amount': normalize_money(r.get('total_with_tax')),
                     'key': (str(r.get('invoice_number') or '').strip(),
                             str(r.get('file_name') or '').strip())}
                    for r in fetched]
        records = records_cache[business_id]
        if records is None:
            continue

        linked = {int(r['source_id']) for r in block if r['source_id'].isdigit()}
        linked |= {int(v) for v in moved.values()}
        free = [r for r in records if r['id'] not in linked]

        # ③ 源记录ID：只补还空着的行，且键必须唯一命中一条空闲记录
        pending = [r for r in block if not r['source_id'] and r['row'] not in moved]
        stats['缺源记录ID行'] += len(pending)
        taken: set[int] = set()
        for row in pending:
            hits = [c for c in candidates(row, free) if c['id'] not in taken]
            if len(hits) != 1:
                stats['说不清行'] += 1
                unclear.append({'行': row['row'], '序号': serial, '审批编号': business_id,
                                '发票号': row['invoice_no'], '源文件名': row['file_name'],
                                '候选': [c['id'] for c in hits],
                                '原因': '无候选' if not hits else f'{len(hits)} 条记录都说得通'})
                continue
            taken.add(hits[0]['id'])
            writes.append({'row': row['row'], 'column': '源记录ID', 'value': str(hits[0]['id']),
                           '依据': f'审批 {business_id} 内唯一命中'})
            stats['补回ID行'] += 1

    return writes, unclear, stats


def ranges(numbers: list[int]) -> list[tuple[int, int]]:
    out = []
    for n in sorted(numbers):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def merged_ranges() -> list[dict]:
    """当前表里的合并区域。块级列（审批编号/公司名称…）按审批块合并是财务的排版习惯，
    但「源记录ID」这类逐行列被合并就会把非锚点的值吞掉——2026-11-07 的事故就是这么来的。
    """
    data, _ = call_json(['sheet', 'info', '--node', NODE, '--sheet-id', SHEET_ID])
    info = (data or {}).get('data') or data or {}
    out = []
    for rng in info.get('mergedRanges') or []:
        match = re.match(r'^([A-Z]+)(\d+):([A-Z]+)(\d+)$', rng)
        if match:
            out.append({'range': rng, 'start_col': match.group(1), 'end_col': match.group(3),
                        'start': int(match.group(2)), 'end': int(match.group(4))})
    return out


def overlaps(merge: dict, letter: str, start: int, end: int) -> bool:
    return merge['start_col'] <= letter <= merge['end_col'] and not (
        end < merge['start'] or start > merge['end'])


def apply_writes(writes: list[dict], positions: dict[str, int],
                 rows: list[dict]) -> list[str]:
    """同列连续段合并成一次 range update，只写需要补的格子。

    落在合并区里的格子直接写会被服务端拒（MERGED_CELLS_CONFLICT），所以先取消那处合并、
    写完再按条件补回：只有「写的是锚点行」且「整段合并属于同一个序号块」时才重新合并，
    否则重新合并会立刻把刚写进去的非锚点值再吞掉。
    """
    serial_by_row = {r['row']: r['serial'] for r in rows}
    merges = merged_ranges()
    notes = []
    by_column: dict[str, list[dict]] = defaultdict(list)
    for item in writes:
        by_column[item['column']].append(item)
    for column, items in sorted(by_column.items()):
        letter = col_letter(positions[column])
        buckets = {item['row']: item['value'] for item in items}
        # 只合并「行号连续且每行都有值」的段：中间断一行就必须断开，
        # 否则 range 覆盖到没值的行，会把人家已有的内容刷成空。
        for start, end in contiguous_runs(sorted(buckets)):
            hit = [m for m in merges if overlaps(m, letter, start, end)]
            for merge in hit:
                call_json(['sheet', 'unmerge-cells', '--node', NODE, '--sheet-id', SHEET_ID,
                           '--range', merge['range']], timeout=120)
            values = [[{'type': 'text', 'text': buckets[row]}] for row in range(start, end + 1)]
            data, err = call_json(['sheet', 'range', 'update', '--node', NODE,
                                  '--sheet-id', SHEET_ID,
                                  '--range', f'{letter}{start}:{letter}{end}',
                                  '--values', json.dumps(values, ensure_ascii=False)],
                                  timeout=120)
            kept = 0
            if data:
                for merge in hit:
                    same_block = len({serial_by_row.get(n, '')
                                      for n in range(merge['start'], merge['end'] + 1)} - {''}) <= 1
                    anchor_only = (buckets.get(merge['start']) is not None
                                   and not any(buckets.get(n)
                                               for n in range(merge['start'] + 1, merge['end'] + 1)))
                    if same_block and anchor_only:
                        if call_json(['sheet', 'merge-cells', '--node', NODE,
                                      '--sheet-id', SHEET_ID,
                                      '--range', merge['range']], timeout=120)[0]:
                            kept += 1
            notes.append(f'{letter}{start}:{letter}{end} ×{len(values)}'
                         f'{"" if not hit else f" (拆合并 {len(hit)}、复原 {kept})"}'
                         f'{" ok" if data else f" 失败 {err}"}')
    return notes


def contiguous_runs(numbers: list[int]) -> list[tuple[int, int]]:
    out = []
    for n in numbers:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正回写空格')
    ap.add_argument('--json', help='把回写计划/结果写成 JSON 备查')
    args = ap.parse_args()

    rows, positions = read_rows()
    writes, unclear, stats = build_plan(rows)
    print(f"表内非空行 {len(rows)}")
    for key, value in stats.items():
        print(f'  {key}: {value}')
    print(f'待回写单元格 {len(writes)} 个（全是空格，不覆盖已有值）')
    for item in writes[:8]:
        print('   ', json.dumps(item, ensure_ascii=False))
    if unclear:
        print(f'⚠️  说不清 {len(unclear)} 行（不动）')
        for item in unclear[:8]:
            print('   ', json.dumps(item, ensure_ascii=False))

    if args.json:
        Path(args.json).write_text(json.dumps(
            {'writes': writes, 'unclear': unclear, 'stats': stats},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'计划已写入 {args.json}')

    if not writes:
        print('没有可补的格子。')
        return 1 if unclear else 0
    if not args.apply:
        print('\ndry-run：加 --apply 回写。')
        return 1

    notes = apply_writes(writes, positions, rows)
    print('回写：', '; '.join(notes))
    return 1 if any('失败' in note for note in notes) else 0


if __name__ == '__main__':
    sys.exit(main())
