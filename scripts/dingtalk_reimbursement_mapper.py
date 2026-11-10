#!/usr/bin/env python3
"""钉钉「日常报销」审批单 → 《报销统计表》(axls) 18 列行块 —— **唯一**映射实现。

为什么单独抽一个模块：
  这份映射要同时被「job 生成端」和「同步 worker 端」使用。
  两处各写一份实现必然漂移（历史上就有过「子表名硬匹配」导致新模板整单漏掉的先例）。
  所以口径只在这里定义一次。

口径来源（已用 51 单逐列验证 100% 一致）：
  audit/2026-09-22/报销统计表-字段对照与同步方案.md

════════════════════════════════════════════════════════════════════════
这张表是「审批单的镜像」，不是「发票台账」
════════════════════════════════════════════════════════════════════════
18 列全部 1:1 来自审批单表单，跟 LLM 解析出的发票字段**无关**。

  列  粒度      来源
  ─────────────────────────────────────────────────────────────
  A   单·合并   人工流水号（**不从这里推导**，由写入端分配）
  B   单·合并   DDSelectField 是否有发票          原值 strip
  C   明细      TextField 发票号 / 票池           多张用「、」分隔
  D   单·合并   DDSelectField 公司名称（选「其他」时取同名 TextField）
  E   明细      子表 项目名称
  F   明细      子表 报销金额（元）
  G   明细      子表 费用发生日期                 → yyyy/m/d
  H   明细      子表 费用类型
  I   明细      子表 费用使用说明
  J   单·合并   CalculateField 合计金额（元）.value
  K   单·合并   同一字段 .extValue 的 upper      **审批单已算好，别自己转**
  L   单·合并   RecipientAccountField.extValue   按 identityType 拼
  M   单·合并   detail.businessId                = 库里 approval_instance_id
  N   单·合并   detail.createTime (epoch ms)
  O   单·合并   审批列表 title「xxx提交的日常报销」前缀
  P   单·合并   processInstanceResult            agree → 审批通过
  Q   单·合并   status                           COMPLETED → 已结束
  R   单·合并   max(operationRecords[].date) 中 type 以 EXECUTE_TASK 开头的
                                                 ⚠️ detail.finishTime 恒为 null

A/B/D/J/K/L/M/N/O/P/Q/R 共 12 列在该单所有明细行之间**纵向合并**；
C/E/F/G/H/I 逐行。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SHANGHAI = timezone(timedelta(hours=8))

#: **语义顺序**的 18 列。⚠️ 这是「值的顺序」，**不代表表内列序**——
#: 表格的列顺序可能被人手动调整（2026-09-23 就发生过：「审批编号」「创建人」
#: 被从 M、O 位拖到了 B、C 位）。任何读写都必须先过 `resolve_column_map()`，
#: **不许按固定下标取列**。
HEADER = [
    '序号', '是否有发票', '发票号', '公司名称', '项目名称', '报销金额（元）',
    '费用发生日期', '费用类型', '费用使用说明', '合计金额（元）',
    '合计金额（元）（大写）', '收款账户', '审批编号', '创建时间', '创建人',
    '审批结果', '审批状态', '完成时间',
]

#: 纵向合并的 12 个单级列（语义下标）
MERGED_COLUMNS = [0, 1, 3, 9, 10, 11, 12, 13, 14, 15, 16, 17]
#: 逐明细行的 6 列（语义下标）
PER_ROW_COLUMNS = [2, 4, 5, 6, 7, 8]

#: 纯展示列：财务 2026-11 把它们从表里删掉了（「费用类型」「合计金额（元）（大写）」）。
#: 缺列只影响这两列不写，不影响定位，因此不再阻断整条同步；其余列缺失仍是表头事故。
OPTIONAL_HEADER = frozenset({'费用类型', '合计金额（元）（大写）'})

#: 语义列名 → 语义下标（按名字取值，别记数字）
SEM_INDEX = {name: i for i, name in enumerate(HEADER)}


# ── 把表里拆开的行收敛回明细组 ──────────────────────────────────────────
#: 「报销金额（元）」的语义下标。拆行的不变式全靠它（见 collapse_detail_groups）
SEM_AMOUNT = SEM_INDEX['报销金额（元）']


def collapse_detail_groups(lines) -> list[dict]:
    """把表里「一行一张发票」拆出来的行**收敛回明细组**。

    lines 是 [(行号, 语义行), ...]（语义行 = 已按表头重排成 HEADER 顺序）。

    依据 `expand_detail_rows()` 建立的不变式：
      **`报销金额（元）` 非空 ⟺ 该行是某个明细的首行**。
    所以按「金额是否非空」切组即可 —— 续行的 `项目名称` 等会和首行**重复**，
    **不能拿文字来切**（同一明细的不同票本来就重复同一段文字）。

    首行永远起一组（即使它金额为空），这样「整块没登记明细」的单会得到 1 组、
    而不是 0 组 —— 好让调用方用「组数 != 明细行数」把它报出来。

    返回 `[{'row': 组首行行号, 'lines': [(行号, 行), ...]}, ...]`。

    ⚠️ 这是**唯一实现**：巡检 D 类（`verify_reimbursement_sheet_consistency.py`）
    和映射自检（`verify_reimbursement_sheet_mapping.py`）都调它，别各写一份。
    """
    groups: list[dict] = []
    for row_no, line in lines:
        if line[SEM_AMOUNT].strip() or not groups:
            groups.append({'row': row_no, 'lines': []})
        groups[-1]['lines'].append((row_no, line))
    return groups


# ── 列定位（表列顺序可能被人改，一律按列名找）────────────────────────────

class HeaderMismatch(RuntimeError):
    """表头与预期不符。宁可停手，也绝不按错的列往下写。"""


def normalize_header_name(name) -> str:
    """表头名归一化：去所有空白、全角括号/标点转半角，用于跨模板比较。"""
    text = '' if name is None else str(name)
    text = re.sub(r'\s+', '', text)
    for full, half in (('（', '('), ('）', ')'), ('，', ','), ('：', ':'),
                       ('、', ','), ('　', '')):
        text = text.replace(full, half)
    return text


def col_letter(index: int) -> str:
    """0-based 列下标 → Excel 列字母（支持超过 26 列）。"""
    letters = ''
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def resolve_column_map(sheet_header: list) -> dict[int, int]:
    """表头行 → ``{语义下标: 表内列下标}``。

    2026-09-23 的事故就是「按固定下标取列」造成的：表里「审批编号」从 M 列
    被拖到 B 列后，查重读到的成了「大写金额」→ 已存在的单被判成新单 →
    重复写入 + 18 列串位 + 回读校验假通过。所以这里改成按列名解析：
    表列顺序怎么变都不怕，找不到需要的列就直接报错、不写表。
    """
    if not sheet_header:
        raise HeaderMismatch('表头行为空，拒绝按固定列位读写')

    lookup: dict[str, int] = {}
    for col, raw in enumerate(sheet_header):
        key = normalize_header_name(raw)
        if key and key not in lookup:
            lookup[key] = col

    mapping: dict[int, int] = {}
    missing: list[str] = []
    for semantic, name in enumerate(HEADER):
        col = lookup.get(normalize_header_name(name))
        if col is None:
            missing.append(name)
        else:
            mapping[semantic] = col
    hard_missing = [name for name in missing if name not in OPTIONAL_HEADER]
    if hard_missing:
        raise HeaderMismatch(
            f'表里找不到这些列：{"、".join(hard_missing)}'
            f'（当前表头：{[str(x).strip() for x in sheet_header]}）')
    return mapping


def absent_optional_columns(sheet_header: list) -> list[str]:
    """表里当前缺失的可选列名，供写入时提示「这些列不写」。"""
    lookup = {normalize_header_name(raw): col
              for col, raw in enumerate(sheet_header or [])
              if normalize_header_name(raw)}
    return [name for name in HEADER
            if name in OPTIONAL_HEADER and normalize_header_name(name) not in lookup]


def header_positions(sheet_header: list, *names: str) -> list[int]:
    """按列名取表内列下标（便捷入口）。"""
    mapping = resolve_column_map(sheet_header)
    return [mapping[SEM_INDEX[name]] for name in names]


#: 子表列名 → 表列下标。旧模板把「报销金额（元）」写成「金额（元）」，两个都认。
DETAIL_COLUMN_MAP = {
    '项目名称': 4,
    '报销金额（元）': 5,
    '金额（元）': 5,
    '费用发生日期': 6,
    '费用类型': 7,
    '费用使用说明': 8,
}

#: 子表识别判据：必须有这两列，才算「报销明细子表」
REQUIRED_DETAIL_COLUMNS = {'项目名称', '报销金额（元）'}

APPROVAL_RESULT_MAP = {'agree': '审批通过'}
APPROVAL_STATUS_MAP = {'COMPLETED': '已结束'}


# ── 基础工具 ────────────────────────────────────────────────────────────

def as_text(value) -> str:
    """审批单里的值统一成去空白的字符串；None → ''。"""
    if value is None:
        return ''
    return str(value).strip()


def parse_ext(value) -> dict:
    """extValue 有时是 JSON 字符串、有时是 dict，统一成 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_amount(value) -> str:
    """金额去尾零：'90.00' → '90'，'1237.28' → '1237.28'，'' → ''。

    表里 J/F 列就是这个形态（506.3 / 2261.03 / 90 / 1674）。
    """
    text = as_text(value)
    if not text:
        return ''
    try:
        return format(Decimal(text).normalize(), 'f')
    except (InvalidOperation, ValueError):
        return text


def fmt_date(value) -> str:
    """日期 → 'yyyy/m/d'（表里 G 列的写法，月/日不补零）。

    接受：'2026-06-25' / '2026/6/25' / epoch ms / datetime。
    """
    if value is None or value == '':
        return ''
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        dt = datetime.fromtimestamp(int(value) / 1000, SHANGHAI)
    else:
        text = as_text(value).replace('/', '-')
        dt = None
        for pattern in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
            try:
                dt = datetime.strptime(text[:len(pattern) + 2], pattern)
                break
            except ValueError:
                continue
        if dt is None:
            return as_text(value)
    return f'{dt.year}/{dt.month}/{dt.day}'


def fmt_datetime(value) -> str:
    """epoch ms / ISO / datetime → 'yyyy/m/d H:MM:SS'（表里 N/R 列的写法）。"""
    if value is None or value == '':
        return ''
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        dt = datetime.fromtimestamp(int(value) / 1000, SHANGHAI)
    else:
        text = as_text(value)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return text
        if dt.tzinfo is not None:
            dt = dt.astimezone(SHANGHAI)
    return f'{dt.year}/{dt.month}/{dt.day} {dt.hour}:{dt.minute:02d}:{dt.second:02d}'


def field_map(detail: dict) -> dict:
    """name → field。⚠️ 同名时**保留第一个非空**的，不要直接 dict 覆盖。

    审批单里「公司名称」有**两个同名字段**（DDSelectField + TextField），
    直接 `{f['name']: f}` 会让后出现的空 TextField 把有值的 SelectField 顶掉，
    公司名就整列变空。
    """
    mapping: dict[str, dict] = {}
    for field in detail.get('formValueVOS') or []:
        name = field.get('name')
        if name is None:
            continue
        if name not in mapping or not as_text(mapping[name].get('value')):
            if name not in mapping or as_text(field.get('value')):
                mapping[name] = field
    return mapping


#: 「公司名称」选到这一项时，真实公司名在同名 TextField 里
OTHER_OPTION = '其他'


def resolve_company(detail: dict) -> str:
    """D 列公司名称。

    审批单里有两个同名字段：
      · DDSelectField —— 正常选公司；选「其他」时它的值就是「其他」
      · TextField     —— 仅在选「其他」时才填真实公司名

    所以**不能简单地「取非空那个」**（那样选「其他」的单会写成「其他」）。
    正确顺序：SelectField 有值且不是「其他」→ 用它；否则用 TextField。
    """
    select_value = ''
    text_value = ''
    for field in detail.get('formValueVOS') or []:
        if field.get('name') != '公司名称':
            continue
        value = as_text(field.get('value'))
        if field.get('componentType') == 'DDSelectField' and value:
            select_value = select_value or value
        elif field.get('componentType') == 'TextField' and value:
            text_value = text_value or value
        elif value and not select_value and not text_value:
            select_value = value
    if select_value and select_value != OTHER_OPTION:
        return select_value
    return text_value or select_value


# ── L 收款账户 ──────────────────────────────────────────────────────────

def build_recipient(field: dict) -> str:
    """按 identityType 拼收款账户。

    PERSONAL_BANK_CARD → "{name} ({instName} {cardNo} {instBranchName})"
    ALIPAY_ACCOUNT     → "{name} (支付宝账号 {logonId})"

    拼不出来时退回 field.value（审批单里就是姓名），别写空。
    """
    if not field:
        return ''
    fallback = as_text(field.get('value'))
    ext = parse_ext(field.get('extValue'))
    if not ext:
        return fallback
    name = as_text(ext.get('name')) or fallback
    identity = as_text(ext.get('identityType')).upper()
    if identity == 'PERSONAL_BANK_CARD':
        parts = [as_text(ext.get(k)) for k in ('instName', 'cardNo', 'instBranchName')]
        inner = ' '.join(p for p in parts if p)
        return f'{name} ({inner})' if inner else fallback
    if identity == 'ALIPAY_ACCOUNT':
        logon = as_text(ext.get('logonId'))
        return f'{name} (支付宝账号 {logon})' if logon else fallback
    return fallback


# ── 明细子表 ────────────────────────────────────────────────────────────

def iter_detail_tables(detail: dict) -> list[tuple[str, list[dict]]]:
    """取出所有「报销明细子表」。

    ⚠️ 两个坑：
      1. 子表名随模板版本变（报销详情 / 有发票 / 无发票有收据 / 无发票无收据），
         **按名字硬匹配会整单漏掉**。判据必须是「componentType==TableField
         且列里含 项目名称 + 报销金额（元）」。
      2. 行数据藏在 field['value'] 这个 **JSON 字符串**里，
         不是 field['details']（后者常常是空壳 [{}]）。
    """
    tables = []
    for field in detail.get('formValueVOS') or []:
        if field.get('componentType') != 'TableField':
            continue
        raw = field.get('value')
        if isinstance(raw, str):
            if not raw.strip():
                continue
            try:
                rows = json.loads(raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(raw, list):
            rows = raw
        else:
            continue
        if not isinstance(rows, list) or not rows:
            continue
        labels = {
            c.get('label')
            for row in rows if isinstance(row, dict)
            for c in (row.get('rowValue') or [])
        }
        if REQUIRED_DETAIL_COLUMNS <= labels:
            tables.append((as_text(field.get('name')), rows))
    return tables


def build_detail_rows(detail: dict) -> list[dict]:
    """子表 → 逐行明细。返回 [{'项目名称':.., '报销金额（元）':.., ...}, ...]

    每行的 key 直接用表头名（E–I），未知列（如「其他费用类型」）也一并带上，
    由写入端决定要不要用。
    """
    rows: list[dict] = []
    for _name, table_rows in iter_detail_tables(detail):
        for row in table_rows:
            if not isinstance(row, dict):
                continue
            parsed: dict[str, str] = {}
            for cell in row.get('rowValue') or []:
                label = as_text(cell.get('label'))
                if not label:
                    continue
                value = cell.get('value')
                if isinstance(value, list):
                    continue  # 附件列，表里不用
                parsed[label] = as_text(value)
            if not parsed:
                continue
            amount = normalize_amount(parsed.get('报销金额（元）') or parsed.get('金额（元）'))
            rows.append({
                '项目名称': parsed.get('项目名称', ''),
                '报销金额（元）': amount,
                '费用发生日期': fmt_date(parsed.get('费用发生日期')),
                '费用类型': parsed.get('费用类型', ''),
                '费用使用说明': parsed.get('费用使用说明', ''),
                # 表里没有、但审批单有的列，供将来加列 / 排查用
                '其他费用类型': parsed.get('其他费用类型', ''),
            })
    return rows


# ── C 发票号 ────────────────────────────────────────────────────────────

def extract_form_invoice_numbers(detail: dict) -> list[str]:
    """从审批单表单里抽「发票号」。

    ⚠️ 只有**部分模板版本**有「发票号」字段（实测 51 单里 7 单有，
    其余模板只挂发票 PDF 附件、表单里根本没有票号字段）。
    抽不到就返回 []，由调用方改用本地票池补。
    """
    numbers: list[str] = []
    for field in detail.get('formValueVOS') or []:
        name = as_text(field.get('name'))
        if name not in {'发票号', '发票号码'}:
            continue
        raw = as_text(field.get('value'))
        if not raw:
            continue
        for part in raw.replace('，', ',').replace('、', ',').replace(';', ',').split(','):
            part = part.strip()
            if part:
                numbers.append(part)
    return numbers


# ── 按金额把整单票号精确划分到明细行 ────────────────────────────────────

def _to_cents(value) -> int | None:
    """金额 → 整数分。用整数分比较，避免浮点 `0.1+0.2 != 0.3` 这类假不匹配。"""
    text = as_text(value)
    if not text:
        return None
    try:
        return int((Decimal(text) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def _order_tickets(numbers: list[str], prices: dict[str, int]) -> list[str]:
    """同一明细内的多张票按**金额升序**排列 —— 对齐表里人工登记的形态。

    证据（2026-09-23 实测，表内「同一明细挂多张票」的组）：
      · 序号 13 `202609021057000520969`：表内 100.80 在最前，其后 4 张 141.00
      · 序号 16 `202609011042000593104`：打车组 29.63 / 32.72 / 33.94、餐饮组同
      · 序号 62 `202606220927000540218`：141.00 / 141.00 / 40.10

    ⚠️ **金额相同的票之间没有可推断的规则，别去凑**。
      实测残留差异全部落在这里：序号 13 那 4 张同为 141.00 的票、
      序号 16 高铁组 2 张同额票 —— 谁在前谁在后都对，人工自己的排法也不一致。
      所以任何「表 ↔ 计算」的逐行比对，**票号只能比集合，不能比序列**。

    ⚠️ 为什么要显式排序，而不是照抄票池返回的顺序：
      票池 API 的返回顺序是**未定义的**（同一单实测返回
      `[42.60, 324.00, 104.70, 109.60, …]`，既非升序也非降序）。
      照抄会让「同一份数据」在表里出现两种排布，后人核对时容易误判成不一致。

    ⚠️ 金额取不到的票（表单手填、池里没有该票号）**排在前面且保持原有相对顺序**
      （`sorted` 是稳定排序）—— 表单里填的是报销人自己写的，优先展示。
    """
    def key(number: str):
        cents = prices.get(number)
        return (0 if cents is None else 1, cents or 0)

    return sorted(numbers, key=key)


def _find_subset(target: int, prices: list[int], max_size: int,
                 node_budget: int) -> list[int] | None:
    """在 prices（下标）里找和为 target 的子集，最多 max_size 个。返回下标列表。

    prices 传的是「可用票的单价数组」，已按金额降序，便于早剪枝。
    用 node_budget 兜住搜索规模，避免一张单几十张票时把 worker 卡死。
    """
    found: list[int] = []
    nodes = 0

    def search(start: int, remaining: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > node_budget:
            return False
        if remaining == 0:
            return True
        if len(found) >= max_size or start >= len(prices):
            return False
        for index in range(start, len(prices)):
            price = prices[index]
            if price > remaining:
                continue
            found.append(index)
            if search(index + 1, remaining - price):
                return True
            found.pop()
            if nodes > node_budget:
                return False
        return False

    if search(0, target):
        return list(found)
    return None


def _assign_invoice_groups(row_amounts, invoices, max_per_row: int = 8,
                           node_budget: int = 200_000) -> list[list[str]]:
    """把**整单**的票按金额划分到各明细行，返回**逐行的票号列表**。

    ⚠️ 这是划分算法的唯一实现。`assign_invoice_numbers()`（拼接成串）和
    `plan_invoice_number_groups()`（交给调用方拆行）都调它，别各写一份。

    参数
      row_amounts : 逐行「报销金额（元）」，顺序与表里明细行一致
      invoices    : [{'invoice_number': .., 'total_with_tax': ..}, ...]（整单所有票）

    返回：与 row_amounts 等长的 list[list[str]]，匹配不上为空列表。

    ⚠️ 为什么不能用贪心逐行匹配：
      贪心是顺序相关的，先处理的行会「抢走」本该属于后面行的票。
      实测序号 27 那单，贪心 5 行匹配不上、整单划分只 2 行匹配不上。

    ⚠️ 单行最多 max_per_row 张：这是**划分算法的约束**（子集搜索的搜索空间上限，
      防止几十张票时 DFS 爆炸），不是「表里一行能塞几张」。
      2026-09-23 起，划分结果**不再往一个格子里拼串**，而是由
      `expand_detail_rows()` **拆成多行**（晨哥口径：一行一张发票）。
    """
    amounts = [_to_cents(a) for a in row_amounts]
    usable = []
    for item in invoices:
        number = as_text(item.get('invoice_number') if isinstance(item, dict) else item[0])
        cents = _to_cents(item.get('total_with_tax') if isinstance(item, dict) else item[1])
        if number and cents is not None:
            usable.append((number, cents))

    # 已出现过的票号去重（同一票号在池子里可能有多条记录）
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for number, cents in usable:
        if number in seen:
            continue
        seen.add(number)
        unique.append((number, cents))

    # 已按金额降序排好的下标池（大额优先，剪枝更早生效）
    order = sorted(range(len(unique)), key=lambda i: -unique[i][1])
    available: list[int] = list(order)

    assignment: list[list[str]] = [[] for _ in amounts]

    # ── 第 1 轮：先满足「该行金额恰好等于某一张票」的行 ────────────────
    # 为什么必须先做这一轮：如果按表格行序直接跑 DFS，一张「由多张票拼成」的行
    # 会优先把它能用的票（含某张小票）吃掉，导致后面那张**本来唯一对应一张票**的
    # 行反而匹配不上。实测序号 27 那单就是这样丢掉 74.2 / 39.41 / 27.59 三行。
    for row_index, cents in enumerate(amounts):
        if cents is None or cents <= 0:
            continue
        exact = [i for i in available if unique[i][1] == cents]
        if not exact:
            continue
        pick = exact[0]
        assignment[row_index] = [unique[pick][0]]
        available.remove(pick)

    # ── 第 2 轮：剩下的行，按「可选票数」从少到多处理（约束紧的先定） ──
    pending = [i for i, cents in enumerate(amounts)
               if not assignment[i] and cents is not None and cents > 0]
    pending.sort(key=lambda i: sum(1 for j in available if unique[j][1] <= amounts[i]))

    for row_index in pending:
        cents = amounts[row_index]
        if not available:
            break
        prices = [unique[i][1] for i in available]
        picked = _find_subset(cents, prices, max_per_row, node_budget)
        if picked is None:
            continue
        chosen = [available[p] for p in picked]
        assignment[row_index] = [unique[i][0] for i in chosen]
        for i in sorted(chosen, reverse=True):
            available.remove(i)
    return assignment


def assign_invoice_numbers(row_amounts, invoices, max_per_row: int = 8,
                           node_budget: int = 200_000) -> list[str]:
    """按金额把整单的票划分到各明细行，**每行拼成一个字符串**（多张用「、」分隔）。

    ⚠️ 这个"拼串"形态只保留给**兼容旧 job 文件**用。
    新代码请用 `plan_invoice_number_groups()` + `expand_detail_rows()` ——
    2026-09-23 晨哥定了口径：**一行一张发票，多张票就拆多行**，不再往一个格子里挤。
    """
    return ['、'.join(group) for group in
            _assign_invoice_groups(row_amounts, invoices, max_per_row, node_budget)]


#: 单行明细的单，超过这么多张票就不自动填（一格塞不下，也没可读性）
SINGLE_ROW_MAX_INVOICES = 10


#: 票池金额与明细金额允许的差额（元）。超过就记一条提示，交人工看。
#: 为什么需要一个而非严格的 0：发票金额与报销金额天然会有小差
#: （实测序号 65：明细 4846.49 / 池 4846.59，差 0.10，属正常）。
INVOICE_AMOUNT_TOLERANCE = 1.00


def invoice_amount_gap(block: dict, pool_invoices) -> dict:
    """单明细行的单：比一比「票池金额合计」与「明细报销金额」。

    ⚠️ 为什么必须有这个：
      `plan_invoice_number_groups()` 对**单明细行**的单是把池里所有票号照单全收的
      （没有金额校验）。如果池里的票根本不属于这一笔，我们就会在台账上宣称
      「这一行报销 = 这 N 张发票」——**这是假的关联**。
      实测 2026-09-23（数字用本函数现场算出来的，别手抄）：
        序号 39 `202607161657000346141` 明细 ¥3523.62，池 72 张合计 **¥4370.06**，
          **差 ¥846.44** → suspect，不填；
        序号 15 `202609011638000525339` 明细 ¥1237.28，池 12 张合计 ¥1237.28，
          差 0.00 → 通过；
        序号 65 `202609211009000095307` 明细 ¥4846.49，池 2 张 ¥4846.59，
          差 0.10 → 在容差内，通过。

    返回 `{'invoices': 张数, 'pool_total': 池金额, 'claim_total': 明细金额,
           'gap': 差额, 'suspect': bool}`。多明细行的单返回 `suspect=False`
    （那边逐行做子集划分，对不齐的行本来就会留空，不需要在这里再判一次）。
    """
    rows = block.get('rows') or []
    claim_total = 0.0
    for row in rows:
        cents = _to_cents(row.get('报销金额（元）'))
        if cents:
            claim_total += cents / 100.0

    pool_total = 0.0
    count = 0
    seen: set[str] = set()
    for item in pool_invoices or []:
        number = as_text(item.get('invoice_number') if isinstance(item, dict) else item[0])
        cents = _to_cents(item.get('total_with_tax') if isinstance(item, dict) else item[1])
        if not number or cents is None or number in seen:
            continue
        seen.add(number)
        pool_total += cents / 100.0
        count += 1

    gap = round(pool_total - claim_total, 2)
    suspect = bool(len(rows) <= 1 and count and abs(gap) > INVOICE_AMOUNT_TOLERANCE)
    return {'invoices': count, 'pool_total': round(pool_total, 2),
            'claim_total': round(claim_total, 2), 'gap': gap, 'suspect': suspect}


def plan_invoice_number_groups(block: dict, pool_invoices,
                               trace: dict | None = None) -> list[list[str]]:
    """给一个行块算出「每个明细行归到哪些票号」，返回 list[list[str]]。

    票号来源优先级（与表里既有 51 单的口径一致）：
      ① 审批单表单的「发票号」字段（报销人自己填的，权威）
      ② 本地票池里同一 approval_instance_id 下 document_type='invoice' 且有票号的记录

    ⚠️ **这里不设"几张票就留空"的上限**（2026-09-23 晨哥定了口径）。
    旧代码有个 `single_row_cap = 10`：单明细行的单如果票多于 10 张就整格留空
    （所以表里序号 39 的 72 张、序号 15 的 12 张一直是空的）。
    现在的做法是**把这一行拆成 N 行、一行一张票号** ——
    既落地了票号，又和表里人工登记出来的形态一致（序号 27 那单 20 行 = 20 个票号）。
    拆行由调用方调用 `expand_detail_rows()` 完成。

    ⚠️ **金额闸门放在这里，不放调用方**：`invoice_amount_gap()` 判 suspect 时
    直接返回全空的 groups（fail closed）。理由 —— 这是唯一的规划入口，
    闸门放调用方的话，任何新的调用点都能绕过它、在台账上造出假关联。
    调用方（`dingtalk_sync_worker.process`）**仍然会再判一次**，那是第二道保险。

    trace 传一个 dict 进来可以拿到「本次是怎么定的」，键：
      · `mode`            : 'blocked-by-amount' | 'single-row' | 'order-total-fallback'
                            | 'per-row' | 'empty'
      · `amount_check`    : `invoice_amount_gap()` 的原样结果
      · `unassigned`      : 池里有票、但没被划分到任何明细行的票号（多明细行时可能出现）
    只用于报告/排查，不影响返回值。

    返回：与 `block['rows']` 等长的列表；匹配不上给空列表（**不猜**：宁可空着可补，
    不可串号污染台账）。
    """
    rows = block.get('rows') or []
    amount_check = invoice_amount_gap(block, pool_invoices)
    if trace is not None:
        trace['amount_check'] = amount_check
        trace['mode'] = 'empty'
        trace['unassigned'] = []

    if not rows:
        return []

    if amount_check['suspect']:
        # 单明细行 + 金额对不上 → 一律不填（序号 39 就是这种：差 ¥846.44）
        if trace is not None:
            trace['mode'] = 'blocked-by-amount'
        return [[] for _ in rows]

    form_numbers = [n for n in (block.get('form_invoice_numbers') or []) if n]
    pool_numbers: list[str] = []
    prices: dict[str, int] = {}
    seen: set[str] = set()
    for item in pool_invoices or []:
        number = as_text(item.get('invoice_number') if isinstance(item, dict) else item[0])
        if not number:
            continue
        if number not in prices:
            cents = _to_cents(item.get('total_with_tax') if isinstance(item, dict)
                              else item[1])
            if cents is not None:
                prices[number] = cents
        if number not in seen:
            seen.add(number)
            pool_numbers.append(number)

    def finalize(result: list[list[str]]) -> list[list[str]]:
        """统一出口：每个明细行内的票号按金额升序（见 `_order_tickets`）。"""
        return [_order_tickets(group, prices) for group in result]

    if len(rows) <= 1:
        # 单明细行：表单票号 + 池内票号全归这一行，几个就拆几行。
        numbers: list[str] = []
        for number in list(form_numbers) + pool_numbers:
            if number not in numbers:
                numbers.append(number)
        if trace is not None:
            trace['mode'] = 'single-row'
        return finalize([numbers])

    # 多明细行：按金额做整单精确划分（见 _assign_invoice_groups）
    groups = _assign_invoice_groups(
        [row.get('报销金额（元）', '') for row in rows], pool_invoices)

    if trace is not None:
        trace['mode'] = 'per-row'
        assigned = {n for group in groups for n in group}
        trace['unassigned'] = [n for n in pool_numbers if n not in assigned]

    if not any(groups) and pool_numbers:
        # ── 兜底：逐行划分全失败，但**整单总额可能仍然对得上** ──────────────
        # 实测序号 24（`202608081550000152912`）：明细 3 行 = 24.64/20.93/43.83，
        # 池里 6 张 = 12.00/10.61/10.30/21.26/12.15/23.08；**两边合计都是 89.40**，
        # 但票是按「每一次行程」开的，报账时被人**合并成了 3 行** ——
        # 逐行的票号归属**在数据上不可推断**。
        #
        # 这时候留空是错的：票是真的、总额也对。做法是**把票号全部挂到首行**，
        # 由 expand_detail_rows 铺成多行 —— 不丢票，也**不硬猜**哪两张票属于哪一行。
        # 只有当**总额**也对不上时才真留空（那是「票根本不属于这笔」的信号）。
        #
        # ⚠️ 已知的精度代价（**如实记下来，别当成没这回事**）：
        #    这样挂完，首行看起来是「报销 24.64 却挂着 6 张票合计 89.40」，
        #    明细归属其实是猜不出来的。所以 trace['mode'] 会标成
        #    'order-total-fallback'，调用方要把这个标记写进报告、交人工看。
        claim_cents = sum(_to_cents(row.get('报销金额（元）')) or 0 for row in rows)
        # 按唯一票号算池金额（池里可能有重复票号，重复算会虚高）
        pool_cents = 0
        seen_numbers: set[str] = set()
        for item in pool_invoices or []:
            number = as_text(item.get('invoice_number') if isinstance(item, dict)
                             else item[0])
            cents = _to_cents(item.get('total_with_tax') if isinstance(item, dict)
                              else item[1])
            if not number or cents is None or number in seen_numbers:
                continue
            seen_numbers.add(number)
            pool_cents += cents
        if claim_cents and abs(pool_cents - claim_cents) <= int(INVOICE_AMOUNT_TOLERANCE * 100):
            groups[0] = list(pool_numbers)
            if trace is not None:
                trace['mode'] = 'order-total-fallback'
                trace['unassigned'] = []
    return finalize(groups)


def plan_invoice_numbers(block: dict, pool_invoices,
                         max_per_row: int = 8,
                         single_row_cap: int = SINGLE_ROW_MAX_INVOICES) -> list[str]:
    """兼容旧 job 的「拼接串」形态（每行一个字符串，多张用「、」）。

    ⚠️ **新代码别再用它**。它保留了旧的「单明细行超过 `single_row_cap` 张就整格留空」
    行为——那正是表里序号 39/15 票号空着的原因。
    新链路用的是 `plan_invoice_number_groups()` + `expand_detail_rows()`（拆多行）。
    """
    groups = plan_invoice_number_groups(block, pool_invoices)
    rows = block.get('rows') or []
    if len(rows) <= 1:
        numbers = groups[0] if groups else []
        if not numbers or len(numbers) > single_row_cap:
            return [''] * len(rows)
        return ['、'.join(numbers)]
    return ['、'.join(group) for group in groups]


#: 拆行产生的「续行」要**跟着重复填**的列。
#: ⚠️ `报销金额（元）` 刻意不在其中 —— 见 `expand_detail_rows()` 的说明。
CONTINUATION_CARRY_COLUMNS = ('项目名称', '费用发生日期', '费用类型', '费用使用说明',
                              '其他费用类型')


def expand_detail_rows(rows: list[dict], groups: list[list[str]]) -> list[dict]:
    """按「一行一张发票」把明细行**拆开**。

    口径（2026-09-23 晨哥定）：一个明细行挂 N 张票 → 拆成 N 行。

      · **首行**保留整行明细内容 + 第 1 张票号；
      · **后续行**（续行）重复 `项目名称` / `费用发生日期` / `费用类型` /
        `费用使用说明`，只填 `发票号`，**`报销金额（元）` 留空**。

    ⚠️ 形态是**照抄表里人工登记的样子**定的，不是拍脑袋：
      2026-09-23 用 `sheet range read --format json` 读了行 43–46 的**原始 cell**，
      续行（行 44/45）里 A–F 列是 `{"value":""}` **且无 cellStyles**（=纵向合并），
      而 G/H/I/J/K 五列**带 cellStyles**，其中
        · G 项目名称 / I 费用发生日期 / J 费用类型 / K 费用使用说明 → 有真实值
        · H 报销金额（元） → 有样式但 value 为空
      即人工的约定是「明细文字重复、**金额只写在明细首行**」。
      该形态在 9 月的登记里最普遍（序号 13 `202609021057000520969` 5 行、
      序号 16 `202609011042000593104` 10 行 都是这样）。

    ⚠️ 为什么金额**必须**留空（这不是美观问题）：
      重复填金额会让「按行求和 `报销金额（元）`」把同一笔钱算 N 次 ——
      台账的和就会虚高。整单级的 12 列之所以纵向合并，本来就是这个目的。
      由此得到一条可自检的不变式：
        **`报销金额（元）` 非空 ⟺ 该行是某个明细的首行**。

    ⚠️ 拆分**不改变任何金额**：新增行不带金额，整单「合计金额（元）」也不动。
    真正变的只有行数和发票号。
    """
    expanded: list[dict] = []
    for index, row in enumerate(rows):
        numbers = groups[index] if index < len(groups) else []
        if not numbers:
            expanded.append(dict(row))
            continue
        first = dict(row)
        first['发票号'] = numbers[0]
        expanded.append(first)
        for number in numbers[1:]:
            extra = {key: '' for key in row}
            for key in CONTINUATION_CARRY_COLUMNS:
                if key in extra:
                    extra[key] = row[key]
            extra['发票号'] = number
            expanded.append(extra)
    return expanded


# ── 完成时间 R ──────────────────────────────────────────────────────────

def resolve_finished_at(detail: dict) -> str:
    """R 列：`operationRecords` 里 type 以 `EXECUTE_TASK` 开头的**最后一条**的 date。

    ⚠️ 两个坑：
      1. `detail.finishTime` **恒为 null**，别拿它当完成时间。
      2. 必须排除 `ADD_REMARK`（备注）和 `PROCESS_CC`（抄送），
         否则会差十几个小时——抄送时间通常晚于审批完成时间。
    """
    latest = None
    for record in detail.get('operationRecords') or []:
        if not isinstance(record, dict):
            continue
        if not as_text(record.get('type')).startswith('EXECUTE_TASK'):
            continue
        date = record.get('date')
        if date is None:
            continue
        if latest is None or date > latest:
            latest = date
    return fmt_datetime(latest)


# ── 主入口 ──────────────────────────────────────────────────────────────

def build_order_block(detail: dict, list_item: dict | None = None) -> dict:
    """审批单 detail → 一张单的完整行块。

    list_item 是 list-by-admin 返回的那条（用来取 title 拿创建人）。
    直接调 detail 时 `processInstanceTitle` 常常退化成 processInstanceId，
    所以**创建人优先从 list_item['title'] 取**。

    返回：
      {
        'business_id', 'company', 'has_invoice', 'total', 'total_upper',
        'recipient', 'created_at', 'creator', 'result', 'status', 'finished_at',
        'form_invoice_numbers': [...],          # 审批单表单里的票号（可能为空）
        'rows': [ {列名: 值}, ... ],            # E–I，逐明细行
        'row_count': int,
      }
    """
    fields = field_map(detail)

    company = resolve_company(detail)

    total_field = fields.get('合计金额（元）')
    total_upper = as_text(parse_ext(total_field.get('extValue')).get('upper')) if total_field else ''

    title = as_text((list_item or {}).get('title')) or as_text(detail.get('processInstanceTitle'))
    creator = title.split('提交的')[0].strip() if '提交的' in title else ''

    raw_result = as_text(detail.get('processInstanceResult')).lower()
    raw_status = as_text(detail.get('status'))

    rows = build_detail_rows(detail)
    return {
        'business_id': as_text(detail.get('businessId')),
        'process_instance_id': as_text(detail.get('processInstanceId')),
        'company': company,
        'has_invoice': as_text(fields['是否有发票'].get('value')) if '是否有发票' in fields else '',
        'total': normalize_amount(total_field.get('value') if total_field else ''),
        'total_upper': total_upper,
        'recipient': build_recipient(fields.get('收款账户')),
        'created_at': fmt_datetime(detail.get('createTime')),
        'creator': creator,
        'result': APPROVAL_RESULT_MAP.get(raw_result, raw_result),
        'status': APPROVAL_STATUS_MAP.get(raw_status, raw_status),
        'finished_at': resolve_finished_at(detail),
        'form_invoice_numbers': extract_form_invoice_numbers(detail),
        'rows': rows,
        'row_count': len(rows),
    }


def block_to_sheet_rows(block: dict, serial=None, serials: list | None = None) -> list[list[str]]:
    """行块 → 写入用的二维数组（每行 18 格，A–R）。

    serial   : 该单的序号（写在 A 列）。单级列只写**第一行**，
               其余行留空——因为写入端随后会做纵向合并，留空才是对的
               （合并前把同一值写满每一行，合并后值也是相同的，但留空更干净）。
    serials  : 逐行序号覆盖（一般不用）。
    """
    out: list[list[str]] = []
    rows = block['rows'] or [{}]
    for index, row in enumerate(rows):
        is_first = index == 0
        line = [''] * 18
        line[0] = '' if serial is None else str(serial)
        line[1] = block['has_invoice'] if is_first else ''
        line[2] = row.get('发票号', '')
        line[3] = block['company'] if is_first else ''
        line[4] = row.get('项目名称', '')
        line[5] = row.get('报销金额（元）', '')
        line[6] = row.get('费用发生日期', '')
        line[7] = row.get('费用类型', '')
        line[8] = row.get('费用使用说明', '')
        line[9] = block['total'] if is_first else ''
        line[10] = block['total_upper'] if is_first else ''
        line[11] = block['recipient'] if is_first else ''
        line[12] = block['business_id'] if is_first else ''
        line[13] = block['created_at'] if is_first else ''
        line[14] = block['creator'] if is_first else ''
        line[15] = block['result'] if is_first else ''
        line[16] = block['status'] if is_first else ''
        line[17] = block['finished_at'] if is_first else ''
        out.append(line)
    return out


# ── CLI ─────────────────────────────────────────────────────────────────

def _cli() -> int:
    ap = argparse.ArgumentParser(description='审批单 → 报销统计表行块（映射自检用）')
    ap.add_argument('--approval-number', help='审批编号（21 位）')
    ap.add_argument('--raw-file', help='已保存的审批单详情 JSON 文件')
    ap.add_argument('--serial', type=int, default=None, help='给这个序号试排一下 A 列')
    args = ap.parse_args()

    if args.raw_file:
        detail = json.loads(open(args.raw_file, encoding='utf-8').read())
    elif args.approval_number:
        sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from approval_detail_by_number import fetch_detail
        detail, err, _pid = fetch_detail(args.approval_number)
        if detail is None:
            print(json.dumps({'ok': False, 'error': err}, ensure_ascii=False), file=sys.stderr)
            return 2
    else:
        ap.error('必须给 --approval-number 或 --raw-file')

    block = build_order_block(detail)
    print(json.dumps(block, ensure_ascii=False, indent=2))
    if args.serial is not None:
        print('\n--- 写入行块（A 列序号 %s）---' % args.serial)
        for line in block_to_sheet_rows(block, serial=args.serial):
            print(json.dumps(line, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
