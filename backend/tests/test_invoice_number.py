import pytest

from app.services.invoice_service import _sanitize_invoice_number, _compare_and_resolve
from app.services.invoice_service import (
    _extract_invoice_number_from_qr,
    _extract_invoice_fields_from_qr,
    _parse_tax_qr_payload,
)


@pytest.mark.parametrize("raw,expected", [
    ("2633200006527202031", "2633200006527202031"),
    ("26332000006527202031", "26332000006527202031"),
    ("00123456", "00123456"),
    ("0012345678", "0012345678"),
    ("２６３３２０００００６５２７２０２０３１", "26332000006527202031"),
    ("26332000 006527202031", "26332000006527202031"),
    ("123456789012345678901", "123456789012345678901"),
    (None, None),
    ("ABC", None),
])
def test_sanitize_preserves_all_digits(raw, expected):
    assert _sanitize_invoice_number(raw) == expected


@pytest.mark.parametrize("has_llm", [False, True])
def test_incomplete_number_requires_review(has_llm):
    fields = {"invoice_number": "2633200006527202031"}
    final, diffs = _compare_and_resolve({} if has_llm else fields, fields if has_llm else {}, has_llm)
    assert final["invoice_number"] == fields["invoice_number"]
    diff = next(d for d in diffs if d["field_name"] == "invoice_number")
    assert diff["needs_review"] is True
    assert diff["final_value"] == final["invoice_number"]


def test_formatted_number_matches_and_diff_agrees():
    final, diffs = _compare_and_resolve(
        {"invoice_number": "26332000 006527202031"},
        {"invoice_number": "26332000006527202031"}, True,
    )
    diff = next(d for d in diffs if d["field_name"] == "invoice_number")
    assert diff["source"] == "matched"
    assert diff["needs_review"] is False
    assert diff["final_value"] == final["invoice_number"]


def test_qr_invoice_number_from_real_invoice():
    import cv2

    payload = '01,32,,26332000006493606201,73.83,20260728,,1A39'
    qr_image = cv2.QRCodeEncoder_create().encode(payload)
    ok, encoded = cv2.imencode('.png', qr_image)

    assert ok
    assert _extract_invoice_number_from_qr(encoded.tobytes()) == '26332000006493606201'


def test_tax_qr_fields_are_parsed():
    assert _parse_tax_qr_payload(
        '01,32,,26332000006493606201,73.83,20260728,,1A39'
    ) == {
        'invoice_number': '26332000006493606201',
        'total_with_tax': '73.83',
        'issue_date': '2026-07-28',
    }


def test_qr_fields_override_vision_but_keep_comparison():
    qr = {
        'invoice_number': '26332000006493606201',
        'total_with_tax': '73.83',
        'issue_date': '2026-07-28',
    }
    final, diffs = _compare_and_resolve(
        qr,
        {
            'invoice_number': '26332000006493606291',
            'total_with_tax': '78.83',
            'issue_date': '2026-07-23',
        },
        True,
        authoritative_fields=set(qr),
    )
    assert final == {**{field: None for field in final}, **qr}
    for field in qr:
        diff = next(item for item in diffs if item['field_name'] == field)
        assert diff['source'] == 'qr'
        assert diff['final_value'] == qr[field]
        assert diff['needs_review'] is False
