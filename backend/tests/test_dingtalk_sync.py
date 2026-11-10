from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services.dingtalk_sync_service import FIELD_IDS, build_invoice_cells


def test_build_invoice_cells_uses_table_types_and_omits_blanks():
    invoice = SimpleNamespace(
        invoice_number="26332000006493606201",
        buyer_name="杭州酷内科技有限公司",
        buyer_tax_id="91330108MADRYT4211",
        seller_name="测试销售方",
        seller_tax_id="91330000TEST000001",
        item_name="测试商品",
        owner=None,
        file_name="invoice.pdf",
        issue_date=date(2026, 7, 28),
        created_at=datetime(2026, 9, 17, 14, 39, 55),
        amount=Decimal("69.65"),
        tax_amount=Decimal("4.18"),
        total_with_tax=Decimal("73.83"),
        tax_rate="6%",
        status=SimpleNamespace(value="已确认"),
        approval_instance_id=None,
    )

    cells = build_invoice_cells(invoice)

    assert cells[FIELD_IDS["invoice_number"]] == invoice.invoice_number
    assert cells[FIELD_IDS["sync_key"]] == invoice.invoice_number
    assert cells[FIELD_IDS["total_with_tax"]] == "73.83"
    assert cells[FIELD_IDS["tax_rate"]] == "0.06"
    assert cells[FIELD_IDS["issue_date"]] == "2026-07-28T00:00:00+08:00"
    assert cells[FIELD_IDS["created_at"]] == "2026-09-17T14:39:00+08:00"
    assert FIELD_IDS["owner"] not in cells
