import json

from app.services.llm_service import LLMService


def test_llm_response_normalization():
    content = json.dumps({
        "invoice_number": " 25952000000233223073 ",
        "invoice_code": "1234-5678-9012",
        "issue_date": "2025年11月05日",
        "buyer_name": "示例买方科技有限公司",
        "buyer_tax_id": " 123456789012345 ",
        "seller_name": "示例卖方服务有限公司",
        "seller_tax_id": "ABCD1234567890123",
        "item_name": "服务费",
        "total_with_tax": "¥8,600.00",
        "amount": "8600.00",
        "tax_amount": "0",
        "tax_rate": "免税",
    })

    service = LLMService()
    fields = service._parse_json_response(content, "test")

    assert fields["invoice_number"] == "25952000000233223073"
    assert fields["invoice_code"] == "123456789012"
    assert fields["issue_date"] == "2025-11-05"
    assert fields["buyer_tax_id"] == "123456789012345"
    assert fields["seller_tax_id"] == "ABCD1234567890123"
    assert fields["total_with_tax"] == "8600.00"
    assert fields["tax_rate"] == "免税"


def test_llm_response_invalid_fields_to_none():
    content = json.dumps({
        "invoice_number": "ABC",
        "invoice_code": "INVALID",
        "issue_date": "2025/11/05",
        "buyer_name": "示例买方科技有限公司",
        "buyer_tax_id": "123",
        "seller_name": "示例卖方服务有限公司",
        "seller_tax_id": "SELLERID",
        "item_name": "服务费",
        "total_with_tax": "not-a-number",
        "amount": "",
        "tax_amount": None,
        "tax_rate": "6 %",
    })

    service = LLMService()
    fields = service._parse_json_response(content, "test")

    assert fields["invoice_number"] is None
    assert fields["invoice_code"] is None
    assert fields["issue_date"] == "2025-11-05"
    assert fields["buyer_tax_id"] is None
    assert fields["seller_tax_id"] is None
    assert fields["total_with_tax"] is None
    assert fields["amount"] is None
    assert fields["tax_amount"] is None
    assert fields["tax_rate"] is None


def test_configured_gemini_gateway_alias_supports_vision(monkeypatch):
    from app.services import llm_service
    monkeypatch.setattr(llm_service.settings, "openai_model", "gemini-3.8-flash-high")
    assert llm_service.OpenAIProvider().supports_vision()
    monkeypatch.setattr(llm_service.settings, "openai_model", "unknown-text-model")
    assert not llm_service.OpenAIProvider().supports_vision()


def test_deepseek_vision_falls_back_to_gemini_on_error(monkeypatch):
    from app.services import llm_service
    monkeypatch.setattr(llm_service.settings, "openai_model", "mf-deepseek-v4.1-flash")
    monkeypatch.setattr(llm_service.settings, "openai_fallback_model", "gemini-3.8-flash-high")
    assert llm_service.OpenAIProvider().supports_vision()
    calls = []

    def fake_vision(self, system, prompt, image, mime, model=None):
        calls.append(model or llm_service.settings.openai_model)
        if model is None:
            raise RuntimeError("primary unavailable")
        return json.dumps({"invoice_number": "26332000006693679501", "total_with_tax": "100.00"})

    monkeypatch.setattr(llm_service.OpenAIProvider, "vision_completion", fake_vision)
    service = LLMService()
    monkeypatch.setattr(type(service), "active_provider", property(lambda self: llm_service.OpenAIProvider()))
    fields = service.parse_invoice_from_image(b"image")
    assert fields["invoice_number"] == "26332000006693679501"
    assert calls == ["mf-deepseek-v4.1-flash", "gemini-3.8-flash-high"]


def test_pdf_text_parser_uses_chat_completion(monkeypatch):
    service = LLMService()
    provider = type("Provider", (), {
        "chat_completion": lambda self, system, prompt: json.dumps({
            "invoice_number": "26332000006693679501",
            "issue_date": "2026-08-03",
            "buyer_name": "杭州气泽科技有限公司",
            "seller_name": "东冠集团有限公司",
            "total_with_tax": "100.00",
        }),
        "get_provider_name": lambda self: "test",
    })()
    monkeypatch.setattr(type(service), "is_available", property(lambda self: True))
    monkeypatch.setattr(type(service), "active_provider", property(lambda self: provider))

    fields = service.parse_invoice_from_text(
        "发票号码：26332000006693679501\n销售方名称：东冠集团有限公司"
    )

    assert fields["invoice_number"] == "26332000006693679501"
    assert fields["seller_name"] == "东冠集团有限公司"
    assert fields["total_with_tax"] == "100.00"


def test_seller_tax_id_is_not_copied_to_individual_buyer():
    content = json.dumps({
        "buyer_name": "钟燕红",
        "buyer_tax_id": "91330108MAK7FH0AXT",
        "seller_name": "杭州一王科技有限公司",
        "seller_tax_id": "91330108MAK7FH0AXT",
    })

    fields = LLMService()._parse_json_response(content, "test")

    assert fields["buyer_tax_id"] is None
    assert fields["seller_tax_id"] == "91330108MAK7FH0AXT"
