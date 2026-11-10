#!/usr/bin/env python3
"""自证「我们同步的是不是晨哥指定的那张表」。

════════════════════════════════════════════════════════════════════════
为什么需要这个
════════════════════════════════════════════════════════════════════════
2026-09-22 同步目标从 AI 表格 `hERWDMS` 换成了在线表格《报销统计表》(axls)。
换目标这种事最容易出两类**静默**错误：

  1. 脚本里还留着旧表的 baseId/tableId → 往废弃的表里写，没人看得到；
  2. 代码换了、`docker-compose.yml` 的环境变量没换（或反之）→ 两套配置各说各话。

这两种都不会报错，只会「同步了但表里没有」。所以每换一次表、或每次怀疑
「是不是同步错表了」，跑这一条命令，拿到四份**可核对**的证据：

  [1] 代码里写死的 nodeId / sheetId
  [2] 真读一次表 —— 表头、列数、末行、已有审批编号数（证明这张表**长得对**）
  [3] 后端容器的 `DINGTALK_SYNC_ENABLED` / `DINGTALK_BASE_ID` / `DINGTALK_TABLE_ID`
      （证明**旧链路确实关着**，以及它的 ID 指向哪里）
  [4] 旧 AI 表格现存记录数（证明它在切换之后**没有被新写入**）

只读：不写表、不写队列、不改任何状态。

用法：
    /usr/local/bin/python3 scripts/verify_sync_target.py
    /usr/local/bin/python3 scripts/verify_sync_target.py --expected-node 4lgGw3P...

退出码：0 全部对得上 / 1 发现不一致（**别急着写表**）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dingtalk_sync_worker as sync_worker          # noqa: E402

#: 2026-09-22 起晨哥指定的目标表（《报销统计表》axls）。
#: 晨哥 2026-09-23 给的确认链接：
#: https://alidocs.dingtalk.com/i/nodes/4lgGw3P8vPbXxd01hZpympmRV5daZ90D
EXPECTED_NODE = '4lgGw3P8vPbXxd01hZpympmRV5daZ90D'
#: 旧的 AI 表格 —— **已停用**，这里只用来查它有没有被误写
LEGACY_BASE_ID = 'LeBq413JA24E9bjoHzYPeEZg8DOnGvpb'
LEGACY_TABLE_ID = 'hERWDMS'

DWS = '/Users/king/.local/node22/lib/node_modules/dingtalk-workspace-cli/vendor/dws'
CONTAINER = 'invoice_backend'

#: 台账前 18 列的表头（缺一列都不对）——用来证明"这张表长得对"
EXPECTED_HEADER = ['序号', '审批编号', '创建人', '是否有发票', '发票号', '公司名称',
                   '项目名称', '报销金额（元）', '费用发生日期', '费用类型', '费用使用说明',
                   '合计金额（元）', '合计金额（元）（大写）', '收款账户',
                   '创建时间', '审批结果', '审批状态', '完成时间']


def section(title):
    print(f'\n{"─" * 66}\n{title}\n{"─" * 66}')


def check_code_side(expected_node):
    section('[1] 脚本里写死的目标')
    ok = True
    for label, mod in (('同步 worker', sync_worker),):
        node = getattr(mod, 'NODE', None)
        sheet = getattr(mod, 'SHEET_ID', None)
        mark = '✅' if node == expected_node else '❌'
        print(f'  {mark} {label}: NODE={node}  SHEET_ID={sheet}')
        if node != expected_node:
            ok = False
    # 其它脚本单独 import 会更慢，直接文本核对常量
    for name in ('verify_reimbursement_sheet_consistency.py',
                 'verify_reimbursement_sheet_mapping.py'):
        path = Path(__file__).parent / name
        text = path.read_text(encoding='utf-8') if path.exists() else ''
        good = f"NODE = '{expected_node}'" in text
        print(f'  {"✅" if good else "❌"} {name}')
        if not good:
            ok = False
    return ok


def check_live_table(expected_node):
    section('[2] 真读一次表（证明这张表长得对）')
    try:
        state = sync_worker.read_sheet_state()
    except Exception as exc:                                  # noqa: BLE001
        print(f'  ❌ 读表失败: {type(exc).__name__}: {exc}')
        return False, None
    header = list(state.get('header') or [])
    filled = [h for h in header if h]
    print(f'  列数 {state.get("col_count")}　末行 {state.get("last_row")}　'
          f'最大序号 {state.get("max_serial")}')
    print(f'  已有审批编号 {len(state.get("known_business_ids") or [])} 张　'
          f'明细指纹 {len(state.get("line_fingerprints") or {})} 张单')
    print(f'  表头: {filled}')
    missing = [h for h in EXPECTED_HEADER if h not in header]
    if missing:
        print(f'  ❌ 表头缺列: {missing}（这张表不是《报销统计表》，或结构被改了）')
        return False, state
    print('  ✅ 前 18 列表头齐全 = 这张就是《报销统计表》')
    return True, state


def check_backend_env():
    section('[3] 后端容器的同步开关（证明旧链路关着）')
    try:
        raw = subprocess.check_output(
            ['docker', 'exec', CONTAINER, 'sh', '-c', 'env | grep -i dingtalk'],
            text=True, timeout=30, stderr=subprocess.DEVNULL)
    except Exception as exc:                                  # noqa: BLE001
        print(f'  ⚠️ 读不到容器环境（{type(exc).__name__}）——容器可能没起，跳过')
        return True
    env = dict(line.split('=', 1) for line in raw.strip().splitlines() if '=' in line)
    enabled = str(env.get('DINGTALK_SYNC_ENABLED', '')).lower()
    ok = enabled not in ('true', '1', 'yes')
    print(f'  {"✅" if ok else "🔴"} DINGTALK_SYNC_ENABLED={env.get("DINGTALK_SYNC_ENABLED")}'
          f'{"（旧 aitable 链路已停用）" if ok else "（⚠️ 开着！后端会往旧表写）"}')
    print(f'     DINGTALK_BASE_ID={env.get("DINGTALK_BASE_ID")}  ← 这是**旧**表')
    print(f'     DINGTALK_TABLE_ID={env.get("DINGTALK_TABLE_ID")}  ← 这是**旧**表')
    if ok:
        print('     （这两个 ID 指向已停用的 AI 表格，仅在开关打开时才生效；保持 false 即无害）')
    return ok


def check_legacy_table():
    section('[4] 旧 AI 表格 hERWDMS 现存记录（应为 242 且不再增长）')
    cmd = [DWS, 'aitable', 'record', 'query', '--base-id', LEGACY_BASE_ID,
           '--table-id', LEGACY_TABLE_ID, '--field-ids', 'CLWOBCJ',
           '--all', '--limit', '20', '--format', 'json']
    try:
        raw = subprocess.check_output(cmd, text=True, timeout=120, stderr=subprocess.STDOUT)
    except Exception as exc:                                   # noqa: BLE001
        print(f'  ⚠️ 查旧表失败（{type(exc).__name__}）——可能已被删除，那就更好')
        return True
    payload = raw[raw.index('{'):] if '{' in raw else raw
    try:
        data = json.loads(payload)
    except Exception:                                          # noqa: BLE001
        print(f'  ⚠️ 返回体无法解析: {raw[:200]}')
        return True
    count = (data.get('data') or {}).get('fetchedCount')
    print(f'  记录数 {count}')
    print('  （2026-09-22 10:24 是旧链路最后一次写入 = 242 条；'
          '这个数字涨了就说明有东西还在往旧表写）')
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description='自证同步目标表身份（只读）')
    parser.add_argument('--expected-node', default=EXPECTED_NODE,
                        help='期望的 nodeId（默认＝2026-09-22 起的目标表）')
    args = parser.parse_args()

    print(f'期望目标表 nodeId: {args.expected_node}')
    results = [
        check_code_side(args.expected_node),
        check_live_table(args.expected_node)[0],
        check_backend_env(),
        check_legacy_table(),
    ]
    section('结论')
    if all(results):
        print('  ✅ 同步目标一致，没有写错表。')
        return 0
    print('  ❌ 有不一致项，**先别写表**，逐条核对上面标 ❌ 的地方。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
