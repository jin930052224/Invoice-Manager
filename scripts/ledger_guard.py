#!/usr/bin/env python3
"""自动补台账前的安全闸门：拦住「疑似重复报销」。

════════════════════════════════════════════════════════════════════════
为什么需要这道闸门
════════════════════════════════════════════════════════════════════════
2026-09-23 起自动链路开始补历史漏单（`dingtalk_approval_import_worker.py`
的每日对账回填）。补台账是**往账上加一笔钱**，一旦补错，比漏补严重得多：
漏了可以再补，重复录进去没人会再核对第二次。

已知的真实案例：

    蒋文聪 `202606300952000024353`（上海酷内 / ¥6763.91）
      第 1 行明细与台账里已有的一行**完全相同**，
      而那一行属于 `202607021630000170465`（台账序号 48）
      → 疑似同一笔报销被批准了两次。自动补进去 = 台账多出 ¥6763.91。

所以自动入队之前，先拿「明细指纹」和台账已有内容比一遍。

════════════════════════════════════════════════════════════════════════
判据（刻意从严）
════════════════════════════════════════════════════════════════════════
- 指纹 = `(归一化项目名称, 归一化报销金额)`。
- 只要待写单的**任一行**在台账里已存在 → 判为疑似重复 → **不入队**，只报警。
- 方向：**宁可误拦报警（人看一眼），不可错写（钱对不上）**。
  误拦的代价是多一条人工确认；漏拦的代价是账目多一笔、且没人发现。

⚠️ 这是「疑似」，不是「判定」。闸门只负责让人来看，不做任何自动删改。

════════════════════════════════════════════════════════════════════════
失败姿态：fail closed
════════════════════════════════════════════════════════════════════════
台账读不到（`HeaderMismatch` / dws 抽风 / 网络不通）时**一律拦下**，
计数记在 `orders_blocked_no_index`。理由：核对不了就不许写钱。
代价是最坏晚一天补上（下一次对账回填会重试），这个代价可以接受。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dingtalk_sync_worker as sync_worker           # noqa: E402
from dingtalk_reimbursement_mapper import SHANGHAI, build_detail_rows  # noqa: E402

#: 进程内缓存：一次运行只读一次表（reconcile 可能一次过几十张单）
_INDEX_CACHE: dict | None = None

#: 台账范围起点（本地时区）。**早于这一天的审批单不属于台账范围，一律不补。**
#:
#: 依据：台账《报销统计表》只收 2026-06-15 起的单，更早的历史单本来就不收——
#: 补进去不是「补漏」而是污染。
#:
#: 为什么必须有这道：2026-09-28 11:53 前端「审批流程附件上传」按钮被点了两次，
#: 该接口的 job 里 `lookback_days` 是**硬编码 365**
#: （`backend/app/routers/dingtalk_import.py:26`），一次捞出 20 张 2025 年老单
#: 塞进同步队列。在此之前**全链路没有任何日期范围判据**。
SCOPE_START = datetime(2026, 6, 15, tzinfo=SHANGHAI)


def created_at_of(detail) -> datetime | None:
    """审批单创建时间（带时区）；取不到返回 None。

    与 mapper 的「创建时间」列同源：`detail.createTime`（epoch 毫秒）。
    """
    value = (detail or {}).get('createTime')
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, SHANGHAI)


def normalize_project(value) -> str:
    """项目名称归一：连内部空白一起压成单空格。

    表里是人工敲的，多一个空格、全角空格都见过；
    不归一的话「打印纸一箱（5包）」和「打印纸一箱（5包）」能判成两回事。
    """
    return ' '.join(str(value or '').split())


def normalize_amount(value) -> str:
    """金额归一：去千分位/币种符号/尾零。

    表里写 `3,446.00`，审批单映射出来是 `3446` —— 字面不同、金额相同，
    不归一就永远撞不上，闸门等于没装。
    """
    text = (str(value or '').strip()
            .replace(',', '').replace('，', '')
            .replace('¥', '').replace('￥', '').replace('元', ''))
    if not text:
        return ''
    try:
        number = float(text)
    except ValueError:
        return text
    # 3446.0 -> '3446'，91.40 -> '91.4'，6763.91 -> '6763.91'
    return f'{number:.10f}'.rstrip('0').rstrip('.') or '0'


def fingerprint(project, amount) -> tuple[str, str]:
    return normalize_project(project), normalize_amount(amount)


def load_index(force: bool = False) -> dict:
    """读一次台账，建：{(项目, 金额): [审批编号…]} + {审批编号: 序号}。

    读表逻辑复用同步 worker 的 `read_sheet_state()`（它已经按表头解析列，
    列顺序变了也不会读错）；这里只把 `line_fingerprints` 摊平成反查表。
    """
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and not force:
        return _INDEX_CACHE

    state = sync_worker.read_sheet_state()
    by_fingerprint: dict[tuple[str, str], list[str]] = {}
    for business_id, fingerprints in (state.get('line_fingerprints') or {}).items():
        for pair in fingerprints:
            # ⚠️ 台账里存的是**原始格子值**（`3,446.00`），审批单映射出来是 `3446`。
            # 建立索引这一步也必须归一化，否则两边字面不同、永远撞不上——
            # 闸门等于没装（2026-09-23 实测踩过：蒋文聪那张本该拦下却放行了）。
            by_fingerprint.setdefault(fingerprint(*pair), []).append(business_id)

    _INDEX_CACHE = {
        'by_fingerprint': by_fingerprint,
        'known_business_ids': state.get('known_business_ids') or set(),
        'last_row': state.get('last_row'),
        'max_serial': state.get('max_serial'),
    }
    return _INDEX_CACHE


#: check_order 的 kind → 含义。调用方按这张表分流，**不要**去猜 reason 字符串。
KIND_MEANING = {
    '': '可以补进台账',
    'already_present': '台账已有该审批编号，没事可做（不是问题）',
    'duplicate': '明细与台账已有记录撞车，疑似重复报销，交人工确认',
    'no_index': '台账读不到，核对不了所以不写钱（fail closed）',
    'out_of_scope': '早于台账范围起点，本来就不收，不补（不是问题）',
}


def check_order(detail) -> dict:
    """判断这张审批单能不能自动补进台账。

    返回 `{'kind': str, 'reason': str, 'collisions': [...]}`，`kind` 取值见
    `KIND_MEANING`：空串 = 可以补；其余三种都**不要**入队，把 reason 报出来给人看。

    ⚠️ 这里返回的是**结构化的 kind**，不是让调用方去 `in reason` 匹配文案——
    文案随时会改，字符串匹配会跟着静默失效（而且失败的方向是「放行」）。
    """
    business_id = str(detail.get('businessId') or '').strip()

    # ① 范围判据放最前面：范围外的单**连台账都不用读**（省一次读表），
    #    而且它是确定性判据，不该被「表读不到」影响成 no_index 报警。
    # ⚠️ 取不到创建时间时不拦（created is None）—— 不能因为一个字段解析失败
    #    就把该补的单静默丢掉；那种情况后面还有 already_present / duplicate 兜着。
    created = created_at_of(detail)
    if created is not None and created < SCOPE_START:
        return {'kind': 'out_of_scope', 'collisions': [],
                'reason': (f'审批单创建于 {created:%Y-%m-%d}，早于台账范围起点 '
                           f'{SCOPE_START:%Y-%m-%d}，不属于台账范围，不补')}

    try:
        index = load_index()
    except Exception as exc:  # noqa: BLE001
        return {'kind': 'no_index', 'collisions': [],
                'reason': f'台账索引不可用（核对不了就不写钱）: '
                          f'{type(exc).__name__}: {exc}'}

    if business_id and business_id in index['known_business_ids']:
        # 台账里已经有这张单。**必须在这里返回**，不能「交给 enqueue 的常规查重去跳过」：
        # enqueue 只认同步队列里的 job 文件，而台账里那 50+ 张单全是人工登记的、
        # 从来没进过队列 —— 放过去就会给每一张都造一个空 job。
        return {'kind': 'already_present', 'collisions': [],
                'reason': '台账已有该审批编号'}

    rows = build_detail_rows(detail)
    collisions = []
    for offset, row in enumerate(rows, start=1):
        pair = fingerprint(row.get('项目名称'), row.get('报销金额（元）'))
        if not any(pair):
            continue
        hit = [bid for bid in index['by_fingerprint'].get(pair, []) if bid != business_id]
        if hit:
            collisions.append({
                '明细行': offset,
                '项目名称': row.get('项目名称'),
                '报销金额（元）': row.get('报销金额（元）'),
                '撞上台账里的单': sorted(set(hit)),
            })

    if collisions:
        return {
            'kind': 'duplicate',
            'collisions': collisions,
            'reason': (f'{len(collisions)} 行明细与台账已有记录完全相同，'
                       f'疑似同一笔报销批了两次 —— 交人工确认，不自动补'),
        }
    return {'kind': '', 'collisions': [], 'reason': ''}


def main() -> int:
    """自检：把闸门跑在指定的审批编号上，只读、不写任何东西。"""
    import argparse
    import json
    import subprocess

    parser = argparse.ArgumentParser(
        description='疑似重复报销闸门自检（只读，不写表、不入队）')
    parser.add_argument('business_ids', nargs='+', help='审批编号（21 位）')
    parser.add_argument('--detail-script',
                        default=str(Path(__file__).parent / 'approval_detail_by_number.py'))
    args = parser.parse_args()

    index = load_index(force=True)
    print(f"台账索引：{len(index['known_business_ids'])} 张单 / "
          f"{len(index['by_fingerprint'])} 条明细指纹，末行 {index['last_row']}\n")

    exit_code = 0
    for business_id in args.business_ids:
        raw = subprocess.check_output(
            ['/usr/local/bin/python3', args.detail_script, business_id, '--raw'], text=True)
        verdict = check_order(json.loads(raw))
        mark = {'': '✅ 放行', 'already_present': '⏭ 台账已有',
                'duplicate': '⛔ 拦下', 'no_index': '⚠️ 读不到表',
                'out_of_scope': '⏭ 范围外(不补)'}[verdict['kind']]
        print(f'{mark}  {business_id}  {verdict["reason"]}')
        for item in verdict['collisions']:
            print('     ' + json.dumps(item, ensure_ascii=False))
        if verdict['kind'] in ('duplicate', 'no_index'):
            exit_code = 1
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
