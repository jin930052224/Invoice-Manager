#!/usr/bin/env python3
"""台账去重清理：把「同一审批编号内同一发票号登记多行」和残留的「凭证行」一次清掉。

判据都只认后台 Invoice.id，不按表里的文字猜（口径与巡检 H/G 类一致）：

  1. 同一审批编号 + 同一发票号 + 同一票面金额 + **同一个原件文件名** → 只留一行。
     保留优先级：已确认 > id 最小。金额不同、或原件文件名不同的**不自动删**
     （后者往往是两张不同发票被识别成同一个号），只报出来给人看。
  2. 表里 `单据类型 = voucher` 的行 → 凭证只留档不同步，删表内行（后台记录保留）。
     但一个审批块删完会一行的不剩时不动，避免这单在台账里整个消失。
  3. **同一条后台记录被两行引用**（巡检 C 类）→ 只删多出来的那一行，后台记录不动。
     2026-11-07 的表尾副本就是这么来的：「源记录ID」列被按审批块合并，599 个 ID
     被合并吞掉，同步 worker 认不出这些单已经登记过，把整单又追加了一遍。
     保留原位（行号小）那一行——那是人工登记的块；但凡副本行有保留行没有的
     逐行内容，就整组交人工，不删。
  4. 同一键（票号+原件+金额）在后台有 N 条记录、台账只有 M 行（M < N，且 M ≥ 1）
     → 多出来的 N-M 条只删后台。两种来路：表里的重复行被人工删掉后记录还留着；
     附件重复上传/定时导入各一份、登记时只写了一行而行上又没 ID（巡检 F 类那 15 单）。
     留着它们，下一轮同步会把「还没登记」的行追加到表尾，财务看到的重复又回来了。
     M = 0 时不动——那是「后台有、表里没有」的漏登记，不是重复。

「合计金额（元）」按约定写在块首行，删掉块首行会把它一起带走。
所以每块先记下原合计，删完后重读表，补到该块剩下的第一行。

用法：
  python3 cleanup_ledger_duplicates.py                 # dry-run，只看计划
  python3 cleanup_ledger_duplicates.py --apply
  python3 cleanup_ledger_duplicates.py --apply --include-vouchers

退出码：0 无需清理 / 1 有清理动作或待人工判断 / 2 执行失败
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dingtalk_reimbursement_mapper import col_letter, normalize_header_name  # noqa: E402
from dingtalk_sync_worker import read_sheet_cells  # noqa: E402
from verify_reimbursement_sheet_consistency import (  # noqa: E402
    API, NODE, SHEET_ID, _token, call_json, pool_invoices,
)

BLOCK_KEYS = {'序号': 'serial', '审批编号': 'business_id', '发票号': 'invoice_no',
              '合计金额（元）': 'total', '源记录ID': 'source_id', '单据类型': 'doc_type',
              '项目名称': 'item', '费用发生日期': 'date', '费用使用说明': 'usage',
              '源文件名': 'file_name', '后台状态': 'status', '报销金额（元）': 'amount'}

#: 逐行内容列：判断「镜像行的信息是不是已经在保留行里」时只看这些列。
#: 块级列（审批编号/创建时间/公司名称/合计金额…）按约定只写在块首行，
#: 保留行空着是正常形态，不能当成「只有副本才有」的数据。
PER_ROW_KEYS = ('invoice_no', 'item', 'amount', 'date', 'usage',
                'doc_type', 'file_name', 'status')

UNSAFE = '删除会丢掉块首行，剩下的行会串到上一个审批块'


#: 后台记录的处理进度（InvoiceStatus 的值是中文，别按枚举名比）。
#: 删重复时留「走得更靠前」的那条，和人工的习惯一致。
STATUS_RANK = {'已报销': 0, '已确认': 1, '未报销': 2, '待审核': 3,
               '待处理': 4, '解析中': 5, '已上传': 6}


def status_rank(record: dict) -> int:
    return STATUS_RANK.get(str(record.get('status') or '').strip(), 9)


def normalize_money(value) -> str:
    digits = ''.join(ch for ch in str(value) if (ch.isdigit() or ch == '.'))
    if not digits:
        return ''
    try:
        return f'{float(digits):.2f}'
    except ValueError:
        return digits


def read_block_rows() -> tuple[list[dict], dict[str, int], list[dict]]:
    """整表 → 审批镜像行 + 全部非空行（结构校验要用后者）。

    返回的 `sheet` 是**按表内行号顺序**的每一非空行：`{'row', 'business',
    'src', 'head'}`。「审批编号」只写在块首行，下面各行靠它向下继承——
    所以删行前必须确认「删掉的不是块首行、或这个块还剩别的行」，否则剩下
    的行会挂到**上一个**审批块下面（202606081540000230279 就是这样：块首
    305 行删掉后，306 行的发票会算进 69 号块）。
    """
    cells = read_sheet_cells()
    if not cells:
        raise RuntimeError('读表返回空')

    def text_of(cell) -> str:
        value = cell.get('value') if isinstance(cell, dict) else cell
        return '' if value is None else str(value).strip()

    header = [normalize_header_name(text_of(c)) for c in cells[0]]
    columns = {key: header.index(normalize_header_name(name))
               for name, key in BLOCK_KEYS.items()
               if normalize_header_name(name) in header}
    missing = [name for name, key in BLOCK_KEYS.items()
               if normalize_header_name(name) not in header]
    if missing:
        raise RuntimeError(f'表头缺少必要列：{missing}')

    rows: list[dict] = []
    sheet: list[dict] = []
    current_business_id = ''
    for index, cells_row in enumerate(cells[1:], start=2):
        values = [text_of(c) for c in cells_row]
        if not any(values):
            continue

        def get(key: str) -> str:
            position = columns[key]
            return values[position] if position < len(values) else ''

        business_id = get('business_id')
        if business_id:
            current_business_id = business_id
        source_id = get('source_id')
        src = int(source_id) if source_id.isdigit() else None
        sheet.append({'row': index, 'business': business_id,
                      'src': src, 'current': current_business_id,
                      'values': values})
        if src is None:
            continue  # 旧人工块没有源记录ID，不归本脚本管
        rows.append({'row': index, 'business_id': current_business_id,
                     'source_id': src, 'invoice_no': get('invoice_no'),
                     'doc_type': get('doc_type'), 'total': get('total'),
                     'amount': get('amount'), 'item': get('item'),
                     'date': get('date'), 'usage': get('usage'),
                     'file_name': get('file_name'), 'status': get('status'),
                     'head': bool(business_id)})
    return rows, columns, sheet


def attribute(sheet: list[dict], deleted: set[int]) -> dict[int, str]:
    """删掉 `deleted` 行后，每个源记录ID 会归属到哪个审批编号（按块首继承）。"""
    out: dict[int, str] = {}
    current = ''
    for line in sheet:
        if line['row'] in deleted:
            continue
        if line['business']:
            current = line['business']
        if line['src'] is not None:
            out[line['src']] = current
    return out



def plan(rows: list[dict], columns: dict[str, int], sheet: list[dict]):
    """返回 (重复删除计划, 凭证删除计划, 待人工判断, 镜像行删除计划, 未镜像重复记录)。"""
    duplicates: list[dict] = []
    vouchers: list[dict] = []
    mirrors: list[dict] = []
    ghost_dups: list[dict] = []
    manual: list[dict] = []

    # 「镜像过」是全局判断：某条记录被表里任何一行引用过，就算已经登记。
    mirrored_all = {row['source_id'] for row in rows}
    lines_of: dict[str, list[dict]] = defaultdict(list)
    for line in sheet:
        if line['current']:
            lines_of[line['current']].append(line)

    def cell_of(line: dict, key: str) -> str:
        position = columns.get(key)
        if position is None or position >= len(line['values']):
            return ''
        return str(line['values'][position] or '').strip()

    def row_key(line: dict) -> tuple:
        return (cell_of(line, 'invoice_no'), cell_of(line, 'file_name'),
                normalize_money(cell_of(line, 'amount')))

    def record_key(record: dict) -> tuple:
        return (str(record.get('invoice_number') or '').strip(),
                str(record.get('file_name') or '').strip(),
                normalize_money(record.get('total_with_tax')))

    by_business: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_business[row['business_id']].append(row)

    for business_id, block in sorted(by_business.items()):
        records = pool_invoices(business_id)
        if records is None:
            manual.append({'审批编号': business_id, '原因': '后台源记录查询失败'})
            continue
        by_id = {int(r['id']): r for r in records}

        # ① 同一条后台记录被表里两行引用（镜像行）。
        # 2026-11-07 的形态：源记录ID 那列被按审批块合并，599 个 ID 被吞，
        # 同步 worker 认不出这些块已经登记过，就把整单又追加到表尾。
        # 追加行的源记录ID 和原位行**是同一个**，所以后台一条都不能删
        # （原位行还指着它），只把多出来的表行删掉。
        by_src: dict[int, list[dict]] = defaultdict(list)
        for row in block:
            if row['source_id'] in by_id:
                by_src[row['source_id']].append(row)
        mirror_rows: set[int] = set()
        for src, items in sorted(by_src.items()):
            if len(items) < 2:
                continue
            keeper, extras = items[0], items[1:]
            # 保留原位那一行：原位是人工登记的块，表尾是机器重写的副本。
            # 副本里但凡有原位没有的逐行内容，就不删——宁留不丢。
            unique = {key: extra[key] for extra in extras for key in PER_ROW_KEYS
                      if extra.get(key) and not keeper.get(key)}
            if unique:
                manual.append({'审批编号': business_id, '源记录ID': src,
                               '保留行': keeper['row'],
                               '镜像行': [e['row'] for e in extras],
                               '只有副本有值': unique,
                               '原因': '镜像行带着保留行没有的内容，先不删'})
                continue
            mirrors.append({
                '审批编号': business_id, '源记录ID': src,
                '保留': keeper['row'],
                '删除': [{'行': e['row'], '源记录ID': src} for e in extras],
                '块合计': next((e['total'] for e in extras if e['total']), ''),
            })
            mirror_rows |= {e['row'] for e in extras}

        # ①b 同一张发票在表里两行，其中一行的「源记录ID」被合并吞掉。
        # 巡检 H 只比 source_id，比不出「有 ID 的行 + 空 ID 的行」这种；
        # 判据还是同一张原件：票号 + 源文件名 + 票面金额三项全同，且后台该键只有一条记录。
        # 这里必须从 sheet 行取数：`rows` 只收有源记录ID 的行，空 ID 行不在里面。
        block_lines: dict[int, dict] = {}
        for line in lines_of.get(business_id, []):
            block_lines[line['row']] = {
                'row': line['row'], 'source_id': line['src'],
                'invoice_no': cell_of(line, 'invoice_no'),
                'file_name': cell_of(line, 'file_name'),
                'amount': normalize_money(cell_of(line, 'amount')),
                'total': cell_of(line, 'total'), 'item': cell_of(line, 'item'),
                'date': cell_of(line, 'date'), 'usage': cell_of(line, 'usage'),
                'doc_type': cell_of(line, 'doc_type'), 'status': cell_of(line, 'status'),
            }
        by_key: dict[tuple, list[dict]] = defaultdict(list)
        for src_row in block:
            line = block_lines.get(src_row['row'])
            if line:
                by_key[(line['invoice_no'], line['file_name'], line['amount'])].append(line)
        for line in block_lines.values():
            if line['source_id'] is None and line['row'] not in mirror_rows:
                by_key[(line['invoice_no'], line['file_name'], line['amount'])].append(line)
        for key, items in sorted(by_key.items(), key=lambda x: str(x[0])):
            if not key[0] or len(items) < 2:
                continue
            anchored = [i for i in items if i['source_id'] in by_id]
            blanks = [i for i in items if i['source_id'] is None]
            if len(anchored) != 1 or not blanks:
                continue  # 两条都有 ID → 是两条记录的事（②/人工），不在这儿动
            if sum(1 for r in records if record_key(r) == key) != 1:
                continue  # 后台该键不止一条，认不出空行原本指谁
            keeper = anchored[0]
            unique = {k: b[k] for b in blanks for k in PER_ROW_KEYS
                      if b.get(k) and not keeper.get(k)}
            if unique:
                manual.append({'审批编号': business_id, '发票号': key[0],
                               '保留行': keeper['row'],
                               '镜像行': [b['row'] for b in blanks],
                               '只有副本有值': unique,
                               '原因': '空 ID 的重复行带着保留行没有的内容，先不删'})
                continue
            mirrors.append({
                '审批编号': business_id, '源记录ID': keeper['source_id'],
                '保留': keeper['row'],
                '删除': [{'行': b['row'], '源记录ID': keeper['source_id']} for b in blanks],
                '块合计': next((b['total'] for b in blanks if b['total']), ''),
            })
            mirror_rows |= {b['row'] for b in blanks}

        # ② 同一键（票号+原件+金额）后台有 N 条记录、台账只有 M 行（M < N）。
        # 来路有两样：表里的重复行被人工删掉后记录还留着；附件重复上传/定时导入
        # 各一份，登记时只写一行、行上又没 ID（巡检 F 那 15 单全是这种）。
        # 台账确实认这个键（M ≥ 1）才动手：多出来的 N-M 条是重复，删了不动台账；
        # M = 0 是「后台有、表里没有」的漏登记，不是重复，留着让人补。
        lines = [line for line in lines_of.get(business_id, [])
                 if line['row'] not in mirror_rows]
        rows_per_key = Counter(row_key(line) for line in lines)
        records_per_key: dict[tuple, list[int]] = defaultdict(list)
        for record in records:
            key = record_key(record)
            if key[0]:
                records_per_key[key].append(int(record['id']))
        for key, ids in sorted(records_per_key.items(), key=lambda x: str(x[0])):
            if not rows_per_key[key]:
                continue
            mirrored_ids = [i for i in ids if i in mirrored_all]
            keep_count = max(len(mirrored_ids), min(rows_per_key[key], len(ids)))
            surplus = len(ids) - keep_count
            candidates = [i for i in ids if i not in mirrored_all]
            if surplus <= 0 or len(candidates) < surplus:
                if surplus > 0:
                    manual.append({'审批编号': business_id, '发票号': key[0],
                                   '后台同键记录数': len(ids), '表内同键行数': rows_per_key[key],
                                   '已被表引用的记录': mirrored_ids,
                                   '原因': '多出来的只能从已被表引用的记录里删，先不删'})
                continue
            # 留「已确认 > id 最小」那条，删掉走得更浅、id 更大的重复记录（后一次导入）。
            ranked = sorted(candidates, key=lambda i: (status_rank(by_id[i]), i))
            for source_id in ranked[len(ranked) - surplus:]:
                ghost_dups.append({'审批编号': business_id, '发票号': key[0],
                                   '源记录ID': source_id, '文件': key[1],
                                   '表内同键行数': rows_per_key[key],
                                   '后台同键记录数': len(ids),
                                   '保留记录': ranked[:len(ranked) - surplus] + mirrored_ids})
            continue

        invoice_rows, voucher_rows = [], []
        for row in block:
            if row['row'] in mirror_rows:
                continue
            record = by_id.get(row['source_id'])
            if record is None:
                manual.append({'审批编号': business_id, '行': row['row'],
                               '原因': '表内源记录ID后台已不存在'})
                continue
            row['record'] = record
            if str(record.get('document_type')) == 'voucher' or row['doc_type'] == 'voucher':
                voucher_rows.append(row)
            else:
                invoice_rows.append(row)

        groups: dict[str, list[dict]] = defaultdict(list)
        for row in invoice_rows:
            number = str(row['record'].get('invoice_number') or '').strip()
            if number:
                groups[number].append(row)

        for invoice_no, items in sorted(groups.items()):
            if len(items) < 2:
                continue
            if len({normalize_money(i['record'].get('total_with_tax'))
                    for i in items}) > 1:
                manual.append({'审批编号': business_id, '发票号': invoice_no,
                               '原因': '同票号但票面金额不同',
                               '行': [(i['row'], i['source_id'],
                                       str(i['record'].get('total_with_tax')))
                                      for i in items]})
                continue
            keeper = sorted(items, key=lambda i: (
                0 if i['head'] else 1,
                status_rank(i['record']), i['source_id']))[0]
            # 「同一发票号」只说明识别结果相同，不说明是同一张原件：
            # 只有文件名也一致（同一附件重复入库）才敢删；文件名不同的
            # 很可能是两张不同发票被识别成同一个号，删了会丢原件，交人工。
            same_file = [i for i in items if i['row'] != keeper['row']
                         and (i['record'].get('file_name') or '')
                         == (keeper['record'].get('file_name') or '')]
            others = [i for i in items
                      if i['row'] != keeper['row'] and i not in same_file]
            if others:
                manual.append({'审批编号': business_id, '发票号': invoice_no,
                               '原因': '同票号但原件文件不同（疑似两张发票共用一个号）',
                               '行': [(i['row'], i['source_id'],
                                       str(i['record'].get('file_name')))
                                      for i in others]})
            if not same_file:
                continue
            duplicates.append({
                '审批编号': business_id, '发票号': invoice_no,
                '保留': {'行': keeper['row'], '源记录ID': keeper['source_id'],
                         '状态': keeper['record'].get('status'),
                         '文件': keeper['record'].get('file_name')},
                '删除': [{'行': i['row'], '源记录ID': i['source_id'],
                          '状态': i['record'].get('status'),
                          '文件': i['record'].get('file_name')}
                         for i in same_file],
                '块合计': next((i['total'] for i in block if i['total']), ''),
            })

        if voucher_rows:
            vouchers.append({
                '审批编号': business_id,
                '行': [v['row'] for v in voucher_rows],
                '删后剩余行': len(block) - len(voucher_rows),
                '块合计': next((i['total'] for i in block if i['total']), ''),
            })

    return duplicates, vouchers, manual, mirrors, ghost_dups


def contiguous_ranges(rows: list[int]) -> list[tuple[int, int]]:
    """[852, 853, 864] → [(864, 1), (852, 2)]：倒序，删前面的不影响后面的行号。

    返回的是 (首行, 行数)，不是 (末行, 行数)——之前这里把首末搞反，
    整段删除请求到了表外的行号，API 直接拒绝，115 行表尾副本一行都没删掉。
    """
    runs: list[list[int]] = []
    for row in sorted(set(rows)):
        if runs and row == runs[-1][0] + runs[-1][1]:
            runs[-1][1] += 1
        else:
            runs.append([row, 1])
    return [(start, length) for start, length in reversed(runs)]


def delete_backend_rows(source_ids: list[int]) -> tuple[list[int], list[str]]:
    """删后台重复记录，返回 (删成功的 ID, 失败说明)。

    失败最常见是 409：该票号还出现在**别的审批单**里，属于疑似重复报销证据，
    后台禁止删除——这类记录连表行一起留着，下轮照样会被巡检报出来。
    """
    done, failed = [], []
    token = _token()
    for source_id in sorted(source_ids):
        request = urllib.request.Request(f'{API}/invoices/{source_id}', method='DELETE',
                                        headers={'Authorization': f'Bearer {token}'})
        try:
            urllib.request.urlopen(request, timeout=60).close()
            done.append(source_id)
        except Exception as exc:  # noqa: BLE001
            failed.append(f'{source_id}: {exc}')
            print(f'❌ 删除后台记录 {source_id} 失败：{exc}', file=sys.stderr)
    return done, failed


def delete_sheet_rows(rows: list[int]) -> list[str]:
    notes = []
    failed = False
    for start, length in contiguous_ranges(rows):
        data, err = call_json(['sheet', 'delete-dimension', '--node', NODE,
                               '--sheet-id', SHEET_ID, '--dimension', 'ROWS',
                               '--position', str(start), '--length', str(length),
                               '--yes'], timeout=120)
        notes.append(f'{start}..{start + length - 1}:{"ok" if data else f"失败 {err}"}')
        failed = failed or data is None
    return notes if not failed else notes + ['⚠️ 有删除失败，请重跑巡检核对']


def restore_totals(business_ids: set[str], totals: dict[str, str],
                   columns: dict[str, int]) -> list[str]:
    """删完重读表：块里没有合计金额了，就补到该块剩下的第一行。"""
    notes = []
    rows, _, _ = read_block_rows()
    first_row: dict[str, int] = {}
    has_total: set[str] = set()
    for row in rows:
        if row['business_id'] in business_ids:
            first_row.setdefault(row['business_id'], row['row'])
            if row['total']:
                has_total.add(row['business_id'])
    letter = col_letter(columns['total'])
    for business_id, value in sorted(totals.items()):
        row = first_row.get(business_id)
        if not value or row is None or business_id in has_total:
            continue
        data, err = call_json(['sheet', 'range', 'update', '--node', NODE,
                              '--sheet-id', SHEET_ID, '--range', f'{letter}{row}',
                              '--values', json.dumps([[{'type': 'text', 'text': value}]])],
                             timeout=120)
        notes.append(f'{business_id} → {letter}{row}={value}:{"ok" if data else err}')
    return notes


def validate_structure(sheet: list[dict], duplicates: list[dict],
                       voucher_blocks: list[dict],
                       mirrors: list[dict] | None = None):
    """逐组试删：删完之后每个剩下的行还必须挂在原来的审批编号下，否则不删。

    块首行（唯一写「审批编号」的那行）被删时，同块剩下的行会向上继承**上一个
    块**的审批编号——台账不会报错，只会静默串到别人的单子里，所以宁可不动。
    """
    mirrors = mirrors or []
    baseline = attribute(sheet, set())
    accepted: set[int] = set()

    def fits(rows: list[int]) -> bool:
        after = attribute(sheet, accepted | set(rows))
        return all(owner == baseline[src] for src, owner in after.items())

    def screen(items: list[dict]) -> tuple[list[dict], list[dict]]:
        kept, rejected = [], []
        for item in sorted(items, key=lambda i: min(d['行'] for d in i['删除'])):
            rows = [d['行'] for d in item['删除']]
            if fits(rows):
                accepted.update(rows)
                kept.append(item)
            else:
                rejected.append(dict(item, 原因=UNSAFE))
        return kept, rejected

    # 镜像行先过：它们删的是表尾那份机器重写的副本，原位块留着，结构最干净。
    kept_mirrors, rejected_mirrors = screen(mirrors)
    kept, rejected = screen(duplicates)
    kept_vouchers: list[dict] = []
    for block in sorted(voucher_blocks, key=lambda b: min(b['行'])):
        if fits(block['行']):
            accepted.update(block['行'])
            kept_vouchers.append(block)
        else:
            rejected.append({'审批编号': block['审批编号'], '原因': UNSAFE,
                             '行': block['行']})
    return kept, kept_vouchers, rejected + rejected_mirrors, kept_mirrors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正执行删除')
    ap.add_argument('--include-vouchers', action='store_true',
                    help='同时删除表里残留的凭证行（后台留档，不进台账）')
    ap.add_argument('--json', help='把清理计划写成 JSON 备查')
    args = ap.parse_args()
    if args.apply and not args.json:
        # 删除是不可逆的：没有显式计划文件就自动落一份带整行内容的备份
        args.json = f'/tmp/ledger_cleanup_{time.strftime("%Y%m%d_%H%M%S")}.json'

    rows, columns, sheet = read_block_rows()
    duplicates, vouchers, manual, mirrors, ghosts = plan(rows, columns, sheet)
    voucher_blocks = [v for v in vouchers if v['删后剩余行'] > 0] if args.include_vouchers else []
    duplicates, voucher_blocks, unsafe, mirrors = validate_structure(
        sheet, duplicates, voucher_blocks, mirrors)
    manual += unsafe

    def sheet_rows_of(items: list[dict]) -> list[int]:
        return [item['行'] for plan_item in items for item in plan_item['删除']]

    backend_ids = [item['源记录ID'] for plan_item in duplicates for item in plan_item['删除']]
    backend_ids += [g['源记录ID'] for g in ghosts]  # 后台比台账多的同键记录：只动后台
    sheet_rows = sheet_rows_of(duplicates) + sheet_rows_of(mirrors)
    if args.include_vouchers:
        sheet_rows += [row for block in voucher_blocks for row in block['行']]

    print(f'表内审批镜像行 {len(rows)} 行')
    print(f'同审批同发票号重复：{len(duplicates)} 组 → 后台删 {len(duplicates)} 条记录')
    print(f'后台多出的同键重复记录：{len(ghosts)} 条 → 只删后台，台账行一行不动')
    print(f'同一记录挂两行（表尾副本）：{len(mirrors)} 组 / '
          f'{sum(len(m["删除"]) for m in mirrors)} 行 → 只删表行，后台一条不动')
    print(f'凭证行残留：{len(vouchers)} 张单 / {sum(len(v["行"]) for v in vouchers)} 行'
          + ('（本次一并删）' if args.include_vouchers else '（默认不动）'))
    if manual:
        print(f'⚠️  待人工判断 {len(manual)} 项（不自动删）')
        for item in manual[:10]:
            print('   ', json.dumps(item, ensure_ascii=False))

    if args.json:
        by_row = {line['row']: line['values'] for line in sheet}
        Path(args.json).write_text(json.dumps(
            {'duplicates': duplicates, 'mirrors': mirrors, 'ghosts': ghosts,
             'vouchers': vouchers, 'manual': manual,
             'snapshot': [{'行': row, '值': by_row.get(row, [])}
                          for row in sorted(set(sheet_rows))]},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'计划已写入 {args.json}')

    if not sheet_rows and not backend_ids:
        print('没有需要清理的重复登记。')
        return 1 if manual else 0

    for plan_item in duplicates[:4]:
        print('  ', json.dumps({k: plan_item[k] for k in ('审批编号', '发票号', '保留', '删除')},
                               ensure_ascii=False))
    for plan_item in ghosts[:4]:
        print('  ', json.dumps(plan_item, ensure_ascii=False))
    for plan_item in mirrors[:4]:
        print('  ', json.dumps({k: plan_item[k] for k in ('审批编号', '源记录ID', '保留', '删除')},
                               ensure_ascii=False))

    if not args.apply:
        print(f'\ndry-run：后台删 {len(backend_ids)} 条记录'
              f'（重复登记 {len(backend_ids) - len(ghosts)} + 后台多出的同键 {len(ghosts)}）、'
              f'表内删 {len(set(sheet_rows))} 行'
              f'（其中 {len(set(sheet_rows_of(mirrors)))} 行是表尾副本，'
              f'它们指向的记录一条不动）。加 --apply 执行。')
        return 1

    token_check = _token()
    if not token_check:
        print('❌ 后台登录失败，不动数据', file=sys.stderr)
        return 2

    done, backend_failed = delete_backend_rows(backend_ids)
    print(f'后台：删成 {len(done)} / {len(backend_ids)}')
    if backend_failed:
        # 后台没删掉的，表内行必须留着——表删了后台还在，下轮同步会把行写回来
        print(f'⚠️  {len(backend_failed)} 条删不掉（多为跨审批重复报销证据），'
              f'对应表内行不动：', file=sys.stderr)
        for note in backend_failed[:20]:
            print('   ', note, file=sys.stderr)
        succeeded = set(done)
        duplicates = [dict(item, 删除=[d for d in item['删除'] if d['源记录ID'] in succeeded])
                      for item in duplicates]
        duplicates = [item for item in duplicates if item['删除']]
        sheet_rows = sheet_rows_of(duplicates) + sheet_rows_of(mirrors)
        if args.include_vouchers:
            sheet_rows += [row for block in voucher_blocks for row in block['行']]
    totals = {}
    for plan_item in (duplicates + mirrors
                      + (voucher_blocks if args.include_vouchers else [])):
        if plan_item['块合计']:
            totals[plan_item['审批编号']] = plan_item['块合计']
    print('表：', '; '.join(delete_sheet_rows(sorted(set(sheet_rows)))))
    affected = {p['审批编号'] for p in duplicates + mirrors}
    if args.include_vouchers:
        affected |= {v['审批编号'] for v in voucher_blocks}
    restored = restore_totals(affected, totals, columns)
    if restored:
        print('合计：', '; '.join(restored))
    return 1


if __name__ == '__main__':
    sys.exit(main())
