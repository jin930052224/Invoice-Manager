import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from app.models.invoice import InvoiceStatus
from app.routers import invoices as router
from app.schemas.invoice import BatchDeleteRequest


def test_batch_unlock_only_changes_confirmed(monkeypatch):
    locked = SimpleNamespace(id=1, status=InvoiceStatus.CONFIRMED)
    reviewing = SimpleNamespace(id=2, status=InvoiceStatus.REVIEWING)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [locked, reviewing])
        )),
        commit=AsyncMock(),
    )
    audit = AsyncMock()
    monkeypatch.setattr(router, "log_audit_no_commit", audit)
    monkeypatch.setattr(router, "get_client_info", lambda request: {})
    result = asyncio.run(router.batch_unlock_invoices(
        request=Request({"type": "http", "method": "POST", "path": "/batch-unlock", "headers": []}),
        batch_request=BatchDeleteRequest(invoice_ids=[1, 2]),
        current_user=SimpleNamespace(is_admin=True), db=db,
    ))
    assert result["unlocked"] == 1 and result["skipped"] == 1
    assert locked.status == InvoiceStatus.REVIEWING
    assert reviewing.status == InvoiceStatus.REVIEWING
    audit.assert_awaited_once()
    db.commit.assert_awaited_once()
