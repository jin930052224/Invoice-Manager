"""Conservative approval metadata matching for newly parsed invoices."""

import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus

logger = logging.getLogger(__name__)
QUEUE = Path(os.environ.get("DINGTALK_IMPORT_QUEUE_DIR", "/dingtalk-import-queue"))


async def match_imported_approval(db, invoice: Invoice) -> bool:
    """Use exact number and amount from previously imported DingTalk attachments.

    A shared number across approvals is ambiguous even when amounts match, so
    never guess which reimbursement owns this invoice.
    """
    if (invoice.document_type != "invoice" or not invoice.invoice_number
            or invoice.total_with_tax is None or (invoice.approval_instance_id and invoice.owner)):
        return False
    rows = (await db.execute(select(Invoice).where(
        Invoice.id != invoice.id,
        Invoice.document_type == "invoice",
        Invoice.invoice_number == invoice.invoice_number,
        Invoice.total_with_tax == invoice.total_with_tax,
        Invoice.approval_instance_id.is_not(None),
    ))).scalars().all()
    approvals = {row.approval_instance_id.strip() for row in rows if row.approval_instance_id and row.approval_instance_id.strip()}
    if invoice.approval_instance_id:
        approvals &= {invoice.approval_instance_id.strip()}
    if len(approvals) != 1:
        return False
    approval = approvals.pop()
    owners = {row.owner.strip() for row in rows if row.approval_instance_id == approval and row.owner and row.owner.strip()}
    if len(owners) > 1:
        return False
    if not invoice.approval_instance_id:
        invoice.approval_instance_id = approval
    if not invoice.owner and owners:
        invoice.owner = owners.pop()
    logger.info("Invoice %s matched imported approval %s by exact number and amount", invoice.id, approval)
    return True


async def propagate_approval_to_reviewing_siblings(db, invoice: Invoice) -> int:
    """When a DingTalk attachment arrives later, link earlier manual uploads."""
    if (invoice.document_type != "invoice" or not invoice.invoice_number
            or invoice.total_with_tax is None or not invoice.approval_instance_id or not invoice.owner):
        return 0
    rows = (await db.execute(select(Invoice).where(
        Invoice.id != invoice.id,
        Invoice.document_type == "invoice",
        Invoice.invoice_number == invoice.invoice_number,
        Invoice.total_with_tax == invoice.total_with_tax,
    ))).scalars().all()
    approvals = {item.approval_instance_id.strip() for item in rows
                 if item.approval_instance_id and item.approval_instance_id.strip()}
    if approvals - {invoice.approval_instance_id.strip()}:
        return 0
    changed = 0
    for item in rows:
        if item.status == InvoiceStatus.CONFIRMED or item.approval_instance_id:
            continue
        item.approval_instance_id = invoice.approval_instance_id
        if not item.owner:
            item.owner = invoice.owner
        changed += 1
    return changed


def queue_dingtalk_lookup(invoice: Invoice) -> None:
    """Ask the host worker to search the DingTalk reimbursement sheet."""
    if (invoice.document_type != "invoice" or invoice.status == InvoiceStatus.CONFIRMED
            or not invoice.invoice_number or invoice.total_with_tax is None
            or (invoice.approval_instance_id and invoice.owner)):
        return
    try:
        QUEUE.mkdir(parents=True, exist_ok=True)
        target = QUEUE / f"match-{invoice.id}.json"
        payload = {"invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
                   "amount": str(Decimal(invoice.total_with_tax))}
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(target)
    except OSError:
        logger.exception("Could not queue DingTalk approval lookup for invoice %s", invoice.id)
