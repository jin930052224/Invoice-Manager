"""钉钉同步「只收报销数据」的守卫测试（依赖 app.*，在容器内跑）。

业务规则：发票库是全量池子，钉钉表 hERWDMS 只收报销数据；
判据 = invoice.approval_instance_id 非空（来自钉钉「日常报销」审批单）。
导入侧的「审批未通过」过滤见 test_dingtalk_import_scope.py。
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import dingtalk_sync_service as sync


def make_settings(queue_dir, enabled=True):
    return SimpleNamespace(
        dingtalk_sync_enabled=enabled,
        dingtalk_base_id="LeBq413JA24E9bjoHzYPeEZg8DOnGvpb",
        dingtalk_table_id="hERWDMS",
        dingtalk_sync_queue_dir=str(queue_dir),
    )


def make_invoice(**overrides):
    base = dict(
        id=1,
        invoice_number="26332000006493606201",
        buyer_name="杭州酷内科技有限公司",
        buyer_tax_id="91330108MADRYT4211",
        seller_name="测试销售方",
        seller_tax_id="91330000TEST000001",
        item_name="测试商品",
        owner="洪岭",
        approval_instance_id="202609151508000023389",
        issue_date=date(2026, 7, 28),
        created_at=datetime(2026, 9, 17, 14, 39, 55),
        amount=Decimal("69.65"),
        tax_amount=Decimal("4.18"),
        total_with_tax=Decimal("73.83"),
        tax_rate="6%",
        status=SimpleNamespace(value="已确认"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_reimbursement_invoice_is_queued_with_owner_and_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(make_invoice()) is True

    import json

    job = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    assert job["key_value"] == "26332000006493606201"
    assert job["key_field_id"] == sync.FIELD_IDS["sync_key"]
    assert job["cells"][sync.FIELD_IDS["approval_instance_id"]] == "202609151508000023389"
    assert job["cells"][sync.FIELD_IDS["owner"]] == "洪岭"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_non_reimbursement_invoice_is_not_queued(tmp_path, monkeypatch, blank):
    """没有审批单号 = 手动上传进发票池的发票，同步上去只会让归属人/审批单号留空。"""
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(make_invoice(approval_instance_id=blank)) is False
    assert list(tmp_path.glob("*.json")) == []


def test_sync_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path, enabled=False))

    assert sync._sync_invoice_blocking(make_invoice()) is False
    assert list(tmp_path.glob("*.json")) == []


def make_voucher(**overrides):
    """凭证（行程单 / 付款截图等）：没有发票号码，只有审批单号。"""
    base = dict(
        document_type="voucher",
        invoice_number=None,
        issue_date=None,
        tax_rate=None,
        amount=None,
        tax_amount=None,
        total_with_tax=Decimal("36.54"),
        status=None,
    )
    base.update(overrides)
    return make_invoice(**base)


def test_voucher_is_not_sent_to_legacy_aitable_queue(tmp_path, monkeypatch):
    """旧 AI 表格链路保持关闭；凭证由现役 axls 原始镜像 worker 统一读取。"""
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(make_voucher(), sequence=2) is False
    assert list(tmp_path.glob("*.json")) == []


def test_voucher_without_sequence_is_not_queued(tmp_path, monkeypatch):
    """序号取不到（库里只有这一张时不应发生，属防御）时宁可不同步，也不写脏键。"""
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(make_voucher(), sequence=None) is False
    assert list(tmp_path.glob("*.json")) == []


def test_voucher_without_approval_is_not_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(
        make_voucher(approval_instance_id=None), sequence=1
    ) is False
    assert list(tmp_path.glob("*.json")) == []


def test_invoice_number_wins_over_sequence(tmp_path, monkeypatch):
    """有票号就永远用票号，序号只在凭证兜底时生效。"""
    monkeypatch.setattr(sync, "get_settings", lambda: make_settings(tmp_path))

    assert sync._sync_invoice_blocking(make_invoice(), sequence=7) is True

    import json

    job = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    assert job["key_value"] == "26332000006493606201"


# --- 凭证「是不是配套单据」的判据 ---

def test_companion_voucher_with_matching_invoice_amount_is_not_standalone():
    """高德打车：行程单 11.49 与同审批单的发票 11.49 配对 → 配套单据，不进表。"""
    assert sync.is_standalone_voucher(Decimal("11.49"), [Decimal("11.49")]) is False


def test_standalone_voucher_without_matching_invoice_is_standalone():
    """拼多多保证金 500 元，同审批单下没有发票 → 独立支出，该进表。"""
    assert sync.is_standalone_voucher(Decimal("500.00"), []) is True
    assert sync.is_standalone_voucher(Decimal("500.00"), [Decimal("36.54")]) is True


def test_blank_amount_voucher_is_not_standalone():
    """金额为空就无从判断是不是配套单据，一律不进表——宁可漏一行，不可多一行。"""
    assert sync.is_standalone_voucher(None, []) is False
    assert sync.is_standalone_voucher("", []) is False


def test_amount_match_survives_string_decimal_mix():
    """库里 numeric 取出来可能是 Decimal，也可能是字符串，比较不能因类型漏判。"""
    assert sync.is_standalone_voucher("11.49", ["11.49"]) is False
    assert sync.is_standalone_voucher(Decimal("11.49"), ["11.490"]) is False
    assert sync.is_standalone_voucher(Decimal("11.49"), ["11.50"]) is True


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    """按调用顺序依次吐出两批结果：同审批单的发票金额、同类型凭证兄弟行。"""

    def __init__(self, amount_rows, sibling_rows):
        self._queue = [amount_rows, sibling_rows]

    async def execute(self, _stmt):
        return _FakeResult(self._queue.pop(0))


def test_resolve_sync_sequence_skips_companion_voucher():
    """行程单与同审批单发票同额 → 返回 None → 上层跳过同步。"""
    voucher = make_voucher(id=122, total_with_tax=Decimal("11.49"))
    db = _FakeDb([Decimal("11.49")], [voucher])

    assert asyncio.run(sync.resolve_sync_sequence(db, voucher)) is None


def test_resolve_sync_sequence_numbers_only_standalone_vouchers():
    """序号只数会同步的凭证：配套单据不占号，删掉它不会让后面的键前移。"""
    companion = make_voucher(id=103, total_with_tax=Decimal("11.49"))
    standalone = make_voucher(id=104, total_with_tax=Decimal("500.00"))
    db = _FakeDb([Decimal("11.49")], [companion, standalone])

    assert asyncio.run(sync.resolve_sync_sequence(db, standalone)) == 1


def test_resolve_sync_sequence_ignores_invoices():
    """发票走票号，不需要序号。"""
    invoice = make_invoice()
    db = _FakeDb([], [])

    assert asyncio.run(sync.resolve_sync_sequence(db, invoice)) is None


# --- 同额凭证之间也要去重（2026-09-21 一致性检查 F 类暴露的坑） ---

def test_same_amount_sibling_voucher_is_not_standalone():
    """同审批单下两张同额凭证：后者是前者的重复支出，不能各自进一行。

    真实案例：审批单 202609031102000273646 里「CCB245BB….png」和
    「LD260814FTG20T+82.4_账单.jpg」都是 82.40，同审批单下**没有**发票，
    只跟发票比的话两张都会被判成独立支出 → 台账里出现两行 82.40。
    """
    assert sync.is_standalone_voucher(Decimal("82.40"), [], [Decimal("82.40")]) is False
    assert sync.is_standalone_voucher(Decimal("82.40"), [], [Decimal("36.54")]) is True


def test_resolve_sync_sequence_dedupes_same_amount_vouchers():
    """两张同额凭证：靠前的那张拿 1，靠后的返回 None（跳过），不产生 -2 键。"""
    first = make_voucher(id=354, total_with_tax=Decimal("82.40"))
    second = make_voucher(id=355, total_with_tax=Decimal("82.40"))

    assert asyncio.run(sync.resolve_sync_sequence(_FakeDb([], [first, second]), first)) == 1
    assert asyncio.run(sync.resolve_sync_sequence(_FakeDb([], [first, second]), second)) is None


def test_same_amount_voucher_after_different_amount_is_still_numbered():
    """去重只看「金额是否已被占用」，不能顺手把后面所有凭证都吞掉。"""
    different = make_voucher(id=400, total_with_tax=Decimal("50.00"))
    same = make_voucher(id=401, total_with_tax=Decimal("82.40"))
    again = make_voucher(id=402, total_with_tax=Decimal("82.40"))

    db_rows = [different, same, again]
    assert asyncio.run(sync.resolve_sync_sequence(_FakeDb([], db_rows), different)) == 1
    assert asyncio.run(sync.resolve_sync_sequence(_FakeDb([], db_rows), same)) == 2
    assert asyncio.run(sync.resolve_sync_sequence(_FakeDb([], db_rows), again)) is None
