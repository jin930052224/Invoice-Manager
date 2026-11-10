from types import SimpleNamespace

from sqlalchemy import select

from app.models.invoice import Invoice
from app.routers.invoices import _apply_user_filter, _apply_write_filter


def test_regular_user_reads_every_upload_but_writes_only_their_own():
    user = SimpleNamespace(id=2, is_admin=False)
    read_sql = str(_apply_user_filter(select(Invoice), user).compile())
    write_sql = str(_apply_write_filter(select(Invoice), user).compile())
    assert "uploaded_by_id =" not in read_sql
    assert "uploaded_by_id =" in write_sql
