#!/usr/bin/env python3
"""补《报销统计表》里「有发票但发票号空」且票池已有票的行。

只做一件事：把票池算出来的票号写进「发票号」列对应行。
不碰技术列（源记录ID 等），不碰其他行。

用法：
  python3 backfill_invoice_numbers.py 202609211009000095307            # dry-run
  python3 backfill_invoice_numbers.py 202609211009000095307 --apply    # 真写
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/Users/king/Documents/Codex/2026-08-28/cai-w/work/Invoice-Manager/scripts')

from approval_detail_by_number import fetch_detail  # noqa: E402
from dingtalk_reimbursement_mapper import (  # noqa: E402
    build_order_block, plan_invoice_number_groups, resolve_column_map,
)
from dingtalk_sync_worker import fetch_pool_invoices  # noqa: E402

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
NODE = '4lgGw3P8vPbXxd01hZpympmRV5daZ90D'
SHEET_ID = 'kgqie6hm'


def read_sheet() -> list[list[str]]:
    out = subprocess.check_output(
        [DWS, 'sheet', 'range', 'read', '--node', NODE,
         '--sheet-id', SHEET_ID, '--range', 'A1:V400'],
        text=True, timeout=90)
    data = json.loads(out)
    if data.get('error'):
        raise RuntimeError(f'读表失败: {json.dumps(data["error"], ensure_ascii=False)}')
    cells = data['cells']

    def val(c):
        if c is None:
            return ''
        if isinstance(c, dict):
            v = c.get('value')
            if v is None:
                v = c.get('text', '')
            return '' if v is None else str(v)
        return str(c)

    return [[val(c) for c in row] for row in cells]


def cell_text(text: str) -> dict:
    return {'type': 'text', 'text': text}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('business_id')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    bid = args.business_id

    rows = read_sheet()
    header = rows[0]
    cmap = resolve_column_map(header)
    print('列映射（语义下标 → 表内列下标）:', cmap)

    def col_of(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            raise SystemExit(f'❌ 表头里找不到「{name}」，拒绝按固定列位写表。表头：{header}')

    col_no = col_of('发票号')
    col_bid = col_of('审批编号')
    col_amt = col_of('报销金额（元）')
    print(f'  「发票号」= 第 {col_no} 列（{chr(65 + col_no)} 列）')

    hits = [i for i, r in enumerate(rows) if i and r[col_bid].strip() == bid]
    if not hits:
        print(f'❌ 表里没有审批编号 {bid}')
        return 1
    print(f'命中行（1-based）: {[h + 1 for h in hits]}')
    for h in hits:
        cur = rows[h][col_no]
        print(f'  行{h + 1}: 发票号当前值 = {cur!r} | 报销金额 = {rows[h][col_amt]!r}')
        if cur.strip():
            print('  该行已有发票号，跳过')
            return 0

    detail, err, pid = fetch_detail(bid)
    if detail is None:
        print(f'❌ 取审批单详情失败: {err}')
        return 2
    pool = fetch_pool_invoices(bid)
    block = build_order_block(detail)
    trace = {}
    groups = plan_invoice_number_groups(block, pool, trace=trace)
    print(f'明细行数 {len(block.get("rows") or [])} | trace.mode = {trace.get("mode")}')
    print('金额闸门:', json.dumps(trace.get('amount_check'), ensure_ascii=False))
    print('划分结果:', json.dumps(groups, ensure_ascii=False))

    if len(hits) != 1 or len(groups) != 1:
        print('❌ 只支持「表里 1 行 + 明细 1 行」的情形，本单不满足，停手')
        return 3
    numbers = groups[0]
    if not numbers:
        print('❌ 算不出票号（闸门拦下或池里没票），停手')
        return 3

    text = '、'.join(numbers)
    target_row = hits[0] + 1
    rng = f'{chr(65 + col_no)}{target_row}'
    print(f'\n→ 计划写入 {rng} = {text!r}')

    if not args.apply:
        print('（dry-run，未写表。加 --apply 真写）')
        return 0

    payload = json.dumps([[cell_text(text)]], ensure_ascii=False)
    out = subprocess.check_output(
        [DWS, 'sheet', 'range', 'update', '--node', NODE, '--sheet-id', SHEET_ID,
         '--range', rng, '--values', payload],
        text=True, timeout=90)
    print('写入返回:', out.strip()[:400])

    after = read_sheet()
    got = after[target_row - 1][col_no]
    ok = got.strip() == text
    print(f'回读 {rng} = {got!r}  → {"✅ 一致" if ok else "❌ 不一致"}')
    return 0 if ok else 4


if __name__ == '__main__':
    sys.exit(main())
