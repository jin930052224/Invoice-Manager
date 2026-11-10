import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from fastapi import HTTPException

from app.models.invoice import InvoiceStatus
from app.services import invoice_service as service


def run_with_result(monkeypatch, fields, *, fail_commit=False):
    invoice = SimpleNamespace(id=74, file_data=b'image', file_type='jpg',
        status=InvoiceStatus.CONFIRMED,
        document_type='invoice', approval_instance_id='202608031200000000001', owner='原归属人',
        approval_claim_amount=None,
        **{f: None for f in service.COMPARABLE_FIELDS})
    invoice.invoice_number = '26512000003771773401'
    invoice.buyer_name = '原购买方'
    invoice.total_with_tax = '2183.35'
    original = vars(invoice).copy()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda:invoice, scalars=lambda:SimpleNamespace(all=lambda:[]))),
        rollback=AsyncMock(), commit=AsyncMock(), add=Mock())
    if fail_commit:
        db.commit.side_effect = RuntimeError('database unavailable')
    monkeypatch.setattr(service, 'get_llm_service', lambda:SimpleNamespace(is_available=True,supports_vision=lambda:True))
    monkeypatch.setattr(service, '_run_llm_vision', lambda *args:fields)
    result = asyncio.run(service.process_invoice(74, db))
    return result,invoice,original,db


def test_empty_model_response_does_not_delete_or_clear_existing_data(monkeypatch):
    ok,invoice,original,db=run_with_result(monkeypatch,{})
    assert not ok
    assert vars(invoice)==original
    assert db.execute.await_count==1  # Only the read, never DELETE.
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


def test_partial_response_does_not_erase_existing_fields(monkeypatch):
    ok,invoice,original,db=run_with_result(monkeypatch,{'invoice_number':'26512000003771773401'})
    assert not ok and vars(invoice)==original
    assert db.execute.await_count==1
    db.commit.assert_not_awaited()


def test_valid_replacement_still_saves(monkeypatch):
    ok,invoice,original,db=run_with_result(monkeypatch,{
        'invoice_number':'26512000003771773401','buyer_name':'正确购买方','total_with_tax':'2183.35'})
    assert ok
    assert invoice.buyer_name=='正确购买方'
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_failed_commit_rolls_back_transaction(monkeypatch):
    ok,invoice,original,db=run_with_result(monkeypatch,{
        'invoice_number':'26512000003771773401','buyer_name':'正确购买方','total_with_tax':'2183.35'},fail_commit=True)
    assert not ok
    db.rollback.assert_awaited_once()


def test_background_failure_restores_prior_status(monkeypatch):
    from app.database import async_session_maker
    from app.routers.invoices import process_invoice_background
    from app import database

    invoice=SimpleNamespace(id=74,status=InvoiceStatus.REIMBURSED,invoice_number='26512000003771773401')
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:invoice)),
        commit=AsyncMock(),rollback=AsyncMock(),add=Mock())
    class Session:
        async def __aenter__(self): return db
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(database,'async_session_maker',Session)
    monkeypatch.setattr(service,'process_invoice',AsyncMock(return_value=False))
    asyncio.run(process_invoice_background(74,max_retries=0))
    assert invoice.status==InvoiceStatus.REIMBURSED
    assert invoice.invoice_number=='26512000003771773401'


def test_batch_reprocess_rejects_locked_invoice(monkeypatch):
    from fastapi import BackgroundTasks
    from starlette.requests import Request
    from app.routers.invoices import batch_reprocess_invoices
    from app.schemas.invoice import BatchDeleteRequest

    invoice=SimpleNamespace(id=74,status=InvoiceStatus.CONFIRMED,invoice_number='26512000003771773401')
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda:SimpleNamespace(all=lambda:[invoice]))),
        commit=AsyncMock(),delete=AsyncMock())
    tasks=BackgroundTasks()
    handler=batch_reprocess_invoices.__wrapped__
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler(
            request=Request({'type':'http','method':'POST','path':'/api/invoices/batch-reprocess','headers':[]}),
            background_tasks=tasks,batch_request=BatchDeleteRequest(invoice_ids=[74]),
            current_user=SimpleNamespace(is_admin=True),db=db))
    assert exc.value.status_code == 409
    assert len(tasks.tasks) == 0
    assert invoice.invoice_number=='26512000003771773401'
    assert invoice.status==InvoiceStatus.CONFIRMED
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert db.execute.await_count==1
