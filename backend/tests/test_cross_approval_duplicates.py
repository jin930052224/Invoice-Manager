from types import SimpleNamespace

from app.routers.invoices import _cross_approval_duplicate_candidates, _inactive_approval_numbers


def invoice(invoice_id, number, approval):
    return SimpleNamespace(id=invoice_id, invoice_number=number, approval_instance_id=approval)


def test_same_approval_reuploads_are_not_cross_approval_duplicates():
    groups = _cross_approval_duplicate_candidates([
        invoice(1, "26332000008883116866", "202610161507000144830"),
        invoice(2, "26332000008883116866", "202610161507000144830"),
        invoice(3, "26332000008883116866", None),
    ])

    assert groups == {}


def test_same_number_under_two_approvals_keeps_one_representative_each():
    groups = _cross_approval_duplicate_candidates([
        invoice(1, "26952000004374439216", "202604010000000000001"),
        invoice(2, "26952000004374439216", "202604010000000000001"),
        invoice(3, "26952000004374439216", "202604020000000000002"),
        invoice(4, "-", "202604030000000000003"),
    ])

    assert list(groups) == ["26952000004374439216"]
    assert [item.id for item in groups["26952000004374439216"]] == [1, 3]


def test_terminated_approval_does_not_count_as_duplicate():
    invoices = [
        invoice(984, "26952000004374439216", "202610201840000032326"),
        invoice(986, "26952000004374439216", "202610210946000180023"),
    ]
    assert _cross_approval_duplicate_candidates(invoices)  # Before status filtering.
    assert _cross_approval_duplicate_candidates(
        invoices, {"202610201840000032326"}
    ) == {}


def test_inactive_approval_index_excludes_terminated_and_refused(tmp_path, monkeypatch):
    import json
    from app.routers import invoices as router

    index = tmp_path / "approvals.json"
    index.write_text(json.dumps({
        "one": {"status": "TERMINATED"},
        "two": {"status": "COMPLETED", "result": "refuse"},
        "three": {"status": "COMPLETED", "result": "agree"},
        "four": {"status": "RUNNING"},
    }))
    monkeypatch.setattr(router, "APPROVAL_INDEX", index)
    assert _inactive_approval_numbers() == {"one", "two"}
