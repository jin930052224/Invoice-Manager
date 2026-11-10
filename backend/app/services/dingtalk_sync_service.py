"""【已停用】把发票同步到钉钉 AI 表格 hERWDMS。

⚠️ 2026-09-22 起同步目标已换成钉钉在线表格《报销统计表》(axls)，
旧的「每张发票 → AI 表格」链路**整体弃用**（`DINGTALK_SYNC_ENABLED=false`）。

为什么不能平移：两套目标的模型不同——
  · AI 表格：1 张发票 = 1 行，票号做唯一键，可 upsert；
  · 报销统计表：**1 张审批单 = N 行明细**，没有 key 列，且 E–I/K/L 等列
    来自**审批单表单**（本服务读的 invoices 表里根本没有）。

新链路在宿主机脚本侧（后端不再参与）：
  · `scripts/dingtalk_reimbursement_mapper.py`  —— 审批单 → 18 列行块（唯一口径）
  · `scripts/dingtalk_approval_import_worker.py` —— 审批通过后按单入队
  · `scripts/dingtalk_sync_worker.py`            —— 消费队列、块写表格 + 合并 + 回读校验

本文件保留的原因：发票池本身（去重、查重弹窗、跨审批单重复检测）仍在用，
`is_standalone_voucher` 等判据还作为**镜像实现**被
`scripts/verify_dingtalk_sync_consistency.py` 引用。
改这些判据时务必同步改巡检脚本里的镜像实现。
"""

import asyncio
import itertools
import json
import logging
import os
import re
from pathlib import Path
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from app.config import get_settings
from app.models.invoice import Invoice

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Field IDs from the user-selected table hERWDMS. 【已停用，仅作历史参照】
FIELD_IDS = {
    "seller_tax_id": "91GfllP",
    "buyer_name": "IOdLhv7",
    "created_at": "4taTroe",
    "tax_amount": "HGMf7d9",
    "invoice_number": "01ZM8y7",
    "tax_rate": "ZDGROCV",
    "sync_key": "CLWOBCJ",
    "item_name": "liLIBMY",
    "buyer_tax_id": "5wYhR7R",
    "seller_name": "VXCLRD5",
    "total_with_tax": "zv0Jvma",
    "issue_date": "vjVQqiY",
    "owner": "B8nE78p",
    "status": "U4L1ZUm",
    "amount": "zusab2V",
    "approval_instance_id": "kZE7D5A",
}

SHANGHAI = timezone(timedelta(hours=8))


def _date_value(value: date | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        else:
            value = value.astimezone(SHANGHAI)
        return value.replace(second=0, microsecond=0).isoformat()
    return datetime(value.year, value.month, value.day, tzinfo=SHANGHAI).isoformat()


def _number_value(value: Any) -> str:
    return format(Decimal(str(value)), "f")


def build_sync_key(invoice: Invoice, sequence: Optional[int] = None) -> Optional[str]:
    """钉钉表的唯一键。

    - 发票：直接用发票号码；
    - 凭证（行程单、付款截图等，没有发票号码）：用「审批单号-序号」，
      否则这类单据永远进不了表（表唯一键就是票号）。
    """
    number = (invoice.invoice_number or "").strip()
    if number:
        return number
    approval = (invoice.approval_instance_id or "").strip()
    if approval and sequence is not None:
        return f"{approval}-{sequence}"
    return None


def is_standalone_voucher(amount, invoice_amounts, sibling_amounts=()) -> bool:
    """这张凭证是「独立支出」还是「某张单据的配套单据」。

    高德打车那种报销会同时传「行程单」和「电子发票」，两者金额一模一样；
    付款截图也常常是某张发票的付款凭证。这类配套单据如果也进钉钉表，
    同一笔报销就会在台账里出现两行、金额看起来翻倍。

    金额只跟发票比是不够的——同一个审批单下**两张凭证**也可能同额
    （例如一张付款截图 + 一张账单截图，都是 82.40），那种情况同样只能进一行。
    所以要比对的「已占用金额」= 该审批单下的发票金额 ∪ 更早取得序号的独立凭证金额。

    判据：
        金额命中任一已占用金额 → 配套单据，不进表；
        金额为空               → 无从判断，也不进表（宁可漏，不可多）。
    """
    if amount is None:
        return False
    try:
        target = Decimal(str(amount))
    except Exception:
        return False
    for candidate in itertools.chain(invoice_amounts, sibling_amounts):
        if candidate is None:
            continue
        try:
            if Decimal(str(candidate)) == target:
                return False
        except Exception:
            continue
    return True


async def resolve_sync_sequence(db, invoice: Invoice) -> Optional[int]:
    """返回凭证类单据的序号；返回 None 表示「这张单据不该进钉钉表」。

    对发票来说 None 无所谓（build_sync_key 会退回用发票号码）；
    对凭证来说 None 就等于跳过。

    序号只数「确定会同步」的凭证（独立支出），配套单据不占号。因此删掉一张
    配套单据不会让后面几张的键前移；反过来，一旦某张独立凭证被删掉，后面几张
    的序号会前移、键就变了，钉钉表里会多出残留行——这是「审批单号-序号」方案的
    固有代价，只能靠一致性检查的 B 类（表里有、本地库没有）兜出来再清理。
    """
    if (invoice.invoice_number or "").strip():
        return None
    approval = (invoice.approval_instance_id or "").strip()
    if not approval:
        return None

    invoice_amounts = (await db.execute(
        select(Invoice.total_with_tax).where(
            Invoice.approval_instance_id == approval,
            Invoice.document_type == "invoice",
        )
    )).scalars().all()

    siblings = (await db.execute(
        select(Invoice).where(
            Invoice.approval_instance_id == approval,
            Invoice.document_type == invoice.document_type,
            Invoice.id <= invoice.id,
        ).order_by(Invoice.id)
    )).scalars().all()
    # 逐张推进，把已经拿到序号的独立凭证金额记为「已占用」，这样同额的
    # 后续凭证会被判成配套单据而不是又占一个序号。当前这张也走同一套判定
    # （siblings 含自己），否则「表里已有的行」和「本地认为该有的键」会用
    # 两套规则，一致性检查就会一直报 B/F 类差异。
    claimed: list = []
    mine: Optional[int] = None
    for sibling in siblings:
        if is_standalone_voucher(sibling.total_with_tax, invoice_amounts, claimed):
            claimed.append(sibling.total_with_tax)
            if sibling.id == invoice.id:
                mine = len(claimed)
    if mine is None:
        logger.info(
            "Invoice %s is a companion voucher (金额为空 / 同审批单下已有同额发票或同额凭证)；"
            "DingTalk sync skipped", invoice.id,
        )
    return mine


def build_invoice_cells(invoice: Invoice, sequence: Optional[int] = None) -> Dict[str, Any]:
    """Build writable cells, omitting blanks that DingTalk normalizes to null."""
    sync_key = build_sync_key(invoice, sequence)
    values: Dict[str, Any] = {
        "invoice_number": invoice.invoice_number,
        "buyer_name": invoice.buyer_name,
        "buyer_tax_id": invoice.buyer_tax_id,
        "seller_name": invoice.seller_name,
        "seller_tax_id": invoice.seller_tax_id,
        "item_name": invoice.item_name,
        "owner": invoice.owner,
        "sync_key": sync_key,
        "approval_instance_id": invoice.approval_instance_id,
    }
    if invoice.issue_date:
        values["issue_date"] = _date_value(invoice.issue_date)
    if invoice.created_at:
        values["created_at"] = _date_value(invoice.created_at)
    for name in ("amount", "tax_amount", "total_with_tax"):
        value = getattr(invoice, name, None)
        if value is not None:
            values[name] = _number_value(value)
    if invoice.tax_rate:
        match = re.search(r"-?\d+(?:\.\d+)?", str(invoice.tax_rate))
        if match:
            # DingTalk's PERCENT formatter expects 0.13 to display 13%, while
            # invoices store tax rates as strings such as "13%".
            values["tax_rate"] = format(Decimal(match.group(0)) / Decimal("100"), "f")
    if invoice.status:
        values["status"] = getattr(invoice.status, "value", str(invoice.status))
    return {
        FIELD_IDS[name]: value
        for name, value in values.items()
        if value is not None and str(value).strip() != ""
    }


def _sync_invoice_blocking(invoice: Invoice, sequence: Optional[int] = None) -> bool:
    # 旧 AI 表格链路已经停用。保留此守卫只为防止误开旧开关；现役 axls 原始镜像
    # 由宿主机 dingtalk_sync_worker.py 读取 /api/invoices/sync-records 完成。
    if getattr(invoice, "document_type", "invoice") == "voucher":
        logger.info("Invoice %s is a voucher archive item; DingTalk sync skipped", invoice.id)
        return False
    settings = get_settings()
    if not settings.dingtalk_sync_enabled:
        return False
    if not settings.dingtalk_base_id or not settings.dingtalk_table_id:
        logger.error("DingTalk sync enabled without base/table ID")
        return False
    # 钉钉表 hERWDMS 只收报销数据。没有审批单号 = 手动上传进发票池的发票
    # （或来自其他非报销渠道），同步过去只会让「归属人」「审批单号」两列留空，
    # 反而污染这张表，因此一律不入队。
    if not (invoice.approval_instance_id or "").strip():
        logger.info(
            "Invoice %s has no approval instance; DingTalk sync skipped (非报销数据)",
            invoice.id,
        )
        return False
    sync_key = build_sync_key(invoice, sequence)
    if not sync_key:
        # 走到这里只有一种情况：凭证类且没拿到序号。resolve_sync_sequence 对
        # 「配套单据 / 金额为空」的凭证就是故意返回 None 的，属正常跳过，不是故障。
        logger.info(
            "Invoice %s has no sync key (凭证无票号且未获序号，按规则跳过同步)",
            invoice.id,
        )
        return False

    cells = build_invoice_cells(invoice, sequence)
    try:
        queue_dir = Path(settings.dingtalk_sync_queue_dir)
        queue_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "invoice_id": invoice.id,
            "base_id": settings.dingtalk_base_id,
            "table_id": settings.dingtalk_table_id,
            "key_field_id": FIELD_IDS["sync_key"],
            "key_value": sync_key,
            "cells": cells,
        }
        temporary = queue_dir / f".{invoice.id}.json.tmp"
        destination = queue_dir / f"{invoice.id}.json"
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, destination)
        logger.info("Invoice %s queued for DingTalk sync", invoice.id)
        return True
    except Exception as exc:
        logger.error("DingTalk sync failed for invoice %s: %s", invoice.id, exc)
        return False


async def sync_invoice_to_dingtalk(invoice: Invoice, sequence: Optional[int] = None) -> bool:
    """Queue DingTalk sync off the event loop; local invoice saving is unaffected.

    sequence 仅凭证类（无发票号码）需要，用 resolve_sync_sequence() 取。
    """
    return await asyncio.to_thread(_sync_invoice_blocking, invoice, sequence)
