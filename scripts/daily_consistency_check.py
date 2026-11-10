#!/usr/bin/env python3
"""《报销统计表》(axls) 一致性每日巡检 runner（挂 launchd 用）。

职责：跑 verify_reimbursement_sheet_consistency.py，落三份产物，按退出码给出结论。
    audit/consistency/<日期>.log        当次完整输出
    audit/consistency/latest.json       机器可读结果
    audit/consistency/sheet-历史.csv    一行一天的滚动趋势（表行数 / 单数 / 问题数）
有问题时额外写 audit/consistency/待处理.md，供人直接看。

退出码沿用 verify 脚本：0 通过 / 1 数据不一致 / 2 数据一致但核对没跑完。

⚠️ 2026-09-22 换目标：同步从 AI 表格 hERWDMS 换成钉钉在线表格《报销统计表》。
   旧 runner 查的是 AI 表格（`verify_dingtalk_sync_consistency.py` 七查 A–G），
   AI 表格停写后继续查只会天天误报「本地未同步」，所以这里整体切到新脚本。
   旧脚本保留不删，需要回看历史台账时仍可手工跑。

用 python 而不是 shell 写，是为了避开 shell 里「变量紧跟中文标点被吞」的老坑。
"""
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT = Path('/Users/king/Documents/Codex/2026-08-28/cai-w/work/Invoice-Manager/scripts/'
              'verify_reimbursement_sheet_consistency.py')
OUT_DIR = Path('/Volumes/D 盘/Codex/发票管理系统/audit/consistency')
TZ_LABEL = '%Y-%m-%d %H:%M:%S'
HISTORY_NAME = 'sheet-历史.csv'


def find_python():
    for candidate in (sys.executable,
                      '/usr/local/bin/python3',
                      '/opt/homebrew/bin/python3',
                      *sorted(str(p) for p in
                              Path('/Users/king/.workbuddy/binaries/python/envs/default/bin').glob('python*'))):
        if candidate and os.path.exists(candidate):
            return candidate
    return 'python3'


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    log_path = OUT_DIR / f'{today}.log'
    json_path = OUT_DIR / f'{today}.json'
    latest_path = OUT_DIR / 'latest.json'

    # ⚠️ `--check-pool` 必须传（2026-09-23 补）：
    # 巡检脚本里 E 类（「表里发票号列空但票池有票」）是 `action='store_true'`，
    # **默认关闭**。这个 runner 原先没传 → E 类**从来没有被算过**，
    # 于是「表里票号一片空着、票池明明有票」这件事天天躺在那里没人报。
    # 典型症状：晨哥问「为什么这个表里还有很多发票号是空着的」才发现。
    # 代价只是多一批本地 HTTP 请求（每张单查一次 `?approval_number=`），可以接受。
    # E 类只作提示、不影响退出码，所以开了也不会让巡检开始误报。
    command = [find_python(), str(SCRIPT), '--json', str(json_path), '--check-pool']
    proc = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    output = (proc.stdout or '') + (proc.stderr or '')
    log_path.write_text(f'# {datetime.now().strftime(TZ_LABEL)}\n{output}', encoding='utf-8')
    print(output)

    result = {}
    if json_path.exists():
        try:
            result = json.loads(json_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            result = {}
    result['checked_at'] = datetime.now().astimezone().isoformat()
    result['exit_code'] = proc.returncode
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')

    sheet = result.get('sheet') or {}
    history = OUT_DIR / HISTORY_NAME
    new_file = not history.exists()
    with history.open('a', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(['日期', '表末行', '表内单数', '钉钉已通过数',
                             '数据问题数', '核对失败数', '退出码'])
        writer.writerow([today, sheet.get('rows', ''), sheet.get('orders', ''),
                         result.get('approved_in_dingtalk', ''),
                         result.get('data_problems', ''), result.get('lookup_failed', ''),
                         proc.returncode])

    # ⚠️ 生成 `待处理.md` 的条件不能只看退出码（2026-09-23 修）：
    # E 类（可补发票号）**不影响退出码**，所以 A–D 全过的日子里原先连文件都不生成，
    # 7 条 E 类只剩 stdout 里一行「ℹ️ E 类提示」→ 没人会看见。
    # 结果就是「表里票号一片空着」躺了很久没人管。
    # 改成：**只要有任何 problems 就出文件**（E 也算），实现"装了报警器必须接线"。
    #
    # ⚠️ `sheet-历史.csv` 刻意**不加** E 列：那份趋势是历史累积的，
    # 中途加列会让老行与新表头错位。要看 E 的逐日明细请读 `<日期>.json` / `latest.json`
    # （`problems['E_可补发票号']` 全都在里面）。
    problems = result.get('problems') or {}
    any_problem = any(problems.values())
    pending = OUT_DIR / '待处理.md'
    if proc.returncode or any_problem:
        lines = [f'# 报销统计表巡检待处理 · {today}', '',
                 f'退出码 {proc.returncode}',
                 '（0=通过 1=数据不一致 2=数据一致但钉钉侧核对没跑完）', '',
                 '> 退出码为 0 但下面仍有条目 = 只有提示类问题（E 类可补发票号），不计入退出码。', '']
        for name, items in problems.items():
            if not items:
                continue
            lines.append(f'## {name}（{len(items)} 条）')
            lines.append('')
            for item in items[:50]:
                lines.append('- ' + json.dumps(item, ensure_ascii=False))
            if len(items) > 50:
                lines.append(f'- ... 另有 {len(items) - 50} 条')
            lines.append('')
        failed = result.get('inspect_failed') or []
        if failed:
            lines.append(f'## 核对失败（{len(failed)} 项，属「没跑完」不是「数据错」）')
            lines.append('')
            for item in failed[:20]:
                lines.append(f'- {item}')
            lines.append('')
        lines.append(f'完整输出见 `audit/consistency/{today}.log`')
        pending.write_text('\n'.join(lines), encoding='utf-8')
    elif pending.exists():
        pending.unlink()

    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
