#!/usr/bin/env python3
"""审批单 → 台账 / 票池 的对账报告。**只读**：不写表、不入队、不改 imported.json。

════════════════════════════════════════════════════════════════════════
为什么要有这个脚本
════════════════════════════════════════════════════════════════════════
「现在都同步了吗？还差什么？」这个问题以前只能靠巡检的 B 类回答，而 B 类**只
看台账一侧**：票池有没有那张单的票，完全没数。2026-09-23 排查「为啥自动同步没
同步到」时才被迫两边一起对，发现缺的是两种东西，得分开看。

每张「已通过」的审批单判一个结论：

    ✅ 齐          台账有 + 附件都处理过
    🟡 缺台账       台账里没有（巡检 B 类报的就是这批）
    🟡 缺票池       附件没进/只进了一部分
    🔴 两者都缺
    ⛔ 疑似重复报销  台账没有、但明细与台账已有记录完全相同
                    → 自动补会被 ledger_guard 拦下，必须人工确认

退出码：0 全齐 / 1 有缺口 / 2 钉钉侧没拉全（没跑完，不下结论）。
产物：audit/reconcile/<日期>.json + <日期>.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dingtalk_approval_import_worker as import_worker   # noqa: E402
import ledger_guard                                        # noqa: E402
import verify_reimbursement_sheet_consistency as verify     # noqa: E402

OUT_DIR = Path('/Volumes/D 盘/Codex/发票管理系统/audit/reconcile')


def attachment_state(detail, imported: set[str]) -> tuple[int, int]:
    """返回 (已处理附件数, 附件总数)。

    ⚠️ 「已处理」不等于「进了票池」：付款截图本来就不是发票，worker 处理完也
    只会停在「已上传」。所以这里说的是「worker 看过了」，不是「发票入库了」。
    压缩包展开出来的子项在 imported.json 里是 `父key:内部文件名`，也一起算数。
    """
    process_instance_id = str(detail.get('processInstanceId') or '')
    attachments = import_worker.attachments_from_detail(detail)
    done = 0
    for attachment in attachments:
        key = f"{process_instance_id}:{attachment.get('fileId')}"
        if key in imported or any(k.startswith(key + ':') for k in imported):
            done += 1
    return done, len(attachments)


def build_report(days: int) -> dict:
    now = datetime.now(verify.SHANGHAI)
    since = now - timedelta(days=days)

    print('读取台账现状…')
    index = ledger_guard.load_index(force=True)
    print(f"  台账 {len(index['known_business_ids'])} 张单 / "
          f"{len(index['by_fingerprint'])} 条明细指纹，末行 {index['last_row']}")

    # 台账的起点 = 台账里最早那张单的日期。早于它的单台账本来就不收，
    # 不能算成「漏同步」——否则报告天天喊「漏了 38 张」，人就再也不看了。
    scope_dates = sorted(bid[:8] for bid in index['known_business_ids']
                         if len(bid) >= 8 and bid[:8].isdigit())
    scope_start = scope_dates[0] if scope_dates else '00000000'
    print(f'  台账范围起点 {scope_start[:4]}-{scope_start[4:6]}-{scope_start[6:8]}')

    imported: set[str] = set()
    if import_worker.STATE.exists():
        imported = set(json.loads(import_worker.STATE.read_text(encoding='utf-8')))
    print(f'  imported.json {len(imported)} 条附件记账')

    print(f'拉钉钉已通过审批单 {since:%Y-%m-%d} ~ {now:%Y-%m-%d} …')
    approved, inspect_failed = verify.list_approved(since, now)
    print(f'  已通过 {len(approved)} 张（核对失败 {len(inspect_failed)} 项）')

    rows = []
    for business_id, entry in sorted(approved.items()):
        detail = entry['detail']
        in_ledger = business_id in index['known_business_ids']
        done, total = attachment_state(detail, imported)
        # ⚠️ 判据用 `kind`（结构化），不要用 reason 文案做字符串匹配，也不要用
        #    已废弃的 `blocked` 键 —— ledger_guard.check_order 的契约是
        #    `{kind, reason, collisions}`，kind 取值见 ledger_guard.KIND_MEANING。
        verdict = ledger_guard.check_order(detail)
        if business_id[:8] < scope_start:
            # 早于台账第一张单 —— 台账本来就不收这段。**不是缺口**，
            # 单独列出来只是为了别让人误以为「漏了 38 张」。
            status = 'out_of_scope'
        elif not in_ledger and verdict['kind'] == 'duplicate':
            status = 'suspect_duplicate'
        elif not in_ledger and done < total:
            status = 'missing_both'
        elif not in_ledger:
            status = 'missing_ledger'
        elif done < total:
            status = 'missing_pool'
        else:
            status = 'ok'
        rows.append({
            '审批编号': business_id,
            '日期': f'{business_id[:4]}-{business_id[4:6]}-{business_id[6:8]}',
            '创建人': str(entry['list_item'].get('title', '')).split('提交的')[0],
            '结论': status,
            '台账已有': in_ledger,
            '附件已处理': f'{done}/{total}',
            '疑似重复撞车': verdict['collisions'][:5] if status == 'suspect_duplicate' else [],
        })

    counts: dict[str, int] = {}
    for row in rows:
        counts[row['结论']] = counts.get(row['结论'], 0) + 1

    return {
        'checked_at': now.isoformat(),
        'window_days': days,
        'since': since.isoformat(),
        'ledger_orders': len(index['known_business_ids']),
        'ledger_scope_start': scope_start,
        'approved_in_dingtalk': len(approved),
        'inspect_failed': inspect_failed,
        'counts': counts,
        'rows': rows,
    }


LABELS = {
    'ok': '✅ 齐',
    'missing_ledger': '🟡 缺台账',
    'missing_pool': '🟡 缺票池',
    'missing_both': '🔴 两者都缺',
    'suspect_duplicate': '⛔ 疑似重复报销',
    'out_of_scope': '⚪ 台账范围外',
}
#: 只有这几类是「要处理的」——out_of_scope 和 ok 都不算
PROBLEM = ['suspect_duplicate', 'missing_both', 'missing_ledger', 'missing_pool']
ORDER = PROBLEM + ['ok', 'out_of_scope']


def render_markdown(report: dict) -> str:
    counts = report['counts']
    lines = [
        f"# 审批单 ↔ 台账/票池 对账 · {report['checked_at'][:10]}",
        '',
        f"窗口：近 {report['window_days']} 天"
        f"（{report['since'][:10]} 起）　"
        f"钉钉已通过 **{report['approved_in_dingtalk']}** 张　"
        f"台账 **{report['ledger_orders']}** 张单",
        f"台账范围起点：**{report['ledger_scope_start'][:4]}-"
        f"{report['ledger_scope_start'][4:6]}-{report['ledger_scope_start'][6:8]}**"
        f"（早于它的单台账本来就不收，不算漏）",
        '',
        '| 结论 | 张数 |',
        '|---|---|',
    ]
    for key in ORDER:
        if counts.get(key):
            lines.append(f"| {LABELS[key]} | {counts[key]} |")
    lines.append('')

    problem = [r for r in report['rows'] if r['结论'] in PROBLEM]
    if problem:
        lines += ['## 需要处理的', '',
                  '| 结论 | 审批编号 | 日期 | 创建人 | 台账已有 | 附件已处理 |',
                  '|---|---|---|---|---|---|']
        for row in sorted(problem, key=lambda r: (ORDER.index(r['结论']), r['审批编号'])):
            lines.append(
                f"| {LABELS[row['结论']]} | `{row['审批编号']}` | {row['日期']} | "
                f"{row['创建人']} | {'有' if row['台账已有'] else '**没有**'} | "
                f"{row['附件已处理']} |")
        lines.append('')

        dupes = [r for r in report['rows'] if r['结论'] == 'suspect_duplicate']
        if dupes:
            lines += ['### ⛔ 疑似重复报销（自动补台账会被闸门拦下）', '']
            for row in dupes:
                lines.append(f"- `{row['审批编号']}` {row['创建人']} {row['日期']}")
                for item in row['疑似重复撞车']:
                    lines.append(
                        f"  - 第 {item['明细行']} 行「{item['项目名称']}」"
                        f"¥{item['报销金额（元）']} 与台账 "
                        f"{'、'.join(item['撞上台账里的单'])} 完全相同")
            lines.append('')
    else:
        lines += ['✅ 窗口内没有需要处理的：范围内每张已通过的单都在台账里、附件也都处理过。', '']

    outside = counts.get('out_of_scope', 0)
    if outside:
        dates = sorted(r['日期'] for r in report['rows'] if r['结论'] == 'out_of_scope')
        lines += [f"⚪ 另有 {outside} 张已通过的单早于台账起点（{dates[0]} ~ {dates[-1]}），"
                  f"台账本来就不收这段，已排除在缺口之外。", '']

    if report['inspect_failed']:
        lines += [f"⚠️ 钉钉侧有 {len(report['inspect_failed'])} 项没拉成（这轮结论不完整）：", '']
        lines += [f'- {item}' for item in report['inspect_failed'][:20]]
        lines.append('')

    lines += ['---', '',
              '本报告**只读**：没写表、没入队、没改 imported.json。',
              '生成命令：`python3 scripts/reconcile_report.py`']
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='审批单 ↔ 台账/票池 对账报告（只读）')
    parser.add_argument('--days', type=int, default=180, help='回看天数（默认 180）')
    parser.add_argument('--json', help='结果 JSON 路径')
    parser.add_argument('--md', help='报告 Markdown 路径')
    args = parser.parse_args()

    report = build_report(args.days)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = report['checked_at'][:10]
    json_path = Path(args.json) if args.json else OUT_DIR / f'{today}.json'
    md_path = Path(args.md) if args.md else OUT_DIR / f'{today}.md'
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    md_path.write_text(render_markdown(report), encoding='utf-8')

    counts = report['counts']
    print()
    for key in ORDER:
        if counts.get(key):
            print(f'  {LABELS[key]}  {counts[key]}')
    print(f'\n已写入 {json_path}')
    print(f'已写入 {md_path}')

    if report['inspect_failed']:
        return 2
    return 1 if any(r['结论'] in PROBLEM for r in report['rows']) else 0


if __name__ == '__main__':
    sys.exit(main())
