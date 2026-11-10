import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.approval_match_service import match_imported_approval, propagate_approval_to_reviewing_siblings
from app.services import approval_match_service
from app.models.invoice import InvoiceStatus


def _db(rows):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: rows))))


def test_exact_number_and_amount_with_unique_approval_matches():
    invoice = SimpleNamespace(id=10, document_type='invoice', invoice_number='26332000007709234926',
                              total_with_tax=Decimal('5.86'), approval_instance_id=None, owner=None)
    source = SimpleNamespace(approval_instance_id='202610281706000420279', owner='张三')
    assert asyncio.run(match_imported_approval(_db([source]), invoice))
    assert invoice.approval_instance_id == '202610281706000420279'
    assert invoice.owner == '张三'


def test_same_invoice_across_approvals_is_ambiguous():
    invoice = SimpleNamespace(id=10, document_type='invoice', invoice_number='26332000007709234926',
                              total_with_tax=Decimal('5.86'), approval_instance_id=None, owner=None)
    sources = [SimpleNamespace(approval_instance_id='approval-a', owner='张三'),
               SimpleNamespace(approval_instance_id='approval-b', owner='李四')]
    assert not asyncio.run(match_imported_approval(_db(sources), invoice))
    assert invoice.approval_instance_id is None


def test_unmatched_review_invoice_queues_sheet_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_match_service, 'QUEUE', tmp_path)
    invoice = SimpleNamespace(id=10, document_type='invoice', status=InvoiceStatus.REVIEWING,
                              invoice_number='26332000007709234926', total_with_tax=Decimal('5.86'),
                              approval_instance_id=None, owner=None)
    approval_match_service.queue_dingtalk_lookup(invoice)
    assert (tmp_path / 'match-10.json').exists()
    invoice.status = InvoiceStatus.CONFIRMED
    (tmp_path / 'match-10.json').unlink()
    approval_match_service.queue_dingtalk_lookup(invoice)
    assert not (tmp_path / 'match-10.json').exists()


def test_later_dingtalk_attachment_links_only_reviewing_copy():
    source = SimpleNamespace(id=10, document_type='invoice', invoice_number='26332000007709234926',
                             total_with_tax=Decimal('5.86'), approval_instance_id='approval-a', owner='张三')
    reviewing = SimpleNamespace(id=11, status=InvoiceStatus.REVIEWING, approval_instance_id=None, owner=None)
    locked = SimpleNamespace(id=12, status=InvoiceStatus.CONFIRMED, approval_instance_id=None, owner=None)
    changed = asyncio.run(propagate_approval_to_reviewing_siblings(_db([reviewing, locked]), source))
    assert changed == 1
    assert reviewing.approval_instance_id == 'approval-a' and reviewing.owner == '张三'
    assert locked.approval_instance_id is None
