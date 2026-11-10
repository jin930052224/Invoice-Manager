"""Invoice processing service."""

import asyncio
import logging
import re
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List, Tuple, Set
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.invoice import Invoice, OcrResult, LlmResult, ParsingDiff, InvoiceStatus
from app.services.ocr_service import get_ocr_service, get_field_extractor
from app.services.llm_service import get_llm_service
from app.services.approval_match_service import match_imported_approval, propagate_approval_to_reviewing_siblings
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Separate thread pools for OCR and LLM.  PaddleOCR's native runtime aborts
# the interpreter when multiple predictor calls overlap, so OCR is purposely
# single-worker.  The LLM pool remains concurrent because it is network I/O.
_ocr_executor = ThreadPoolExecutor(max_workers=1)
_llm_executor = ThreadPoolExecutor(max_workers=settings.llm_max_workers)
logger.info(f"Initialized thread pools: OCR=1 (Paddle-safe), LLM={settings.llm_max_workers}")

# Fields to compare between OCR and LLM
COMPARABLE_FIELDS = [
    'invoice_number',
    'issue_date',
    'buyer_name',
    'buyer_tax_id',
    'seller_name',
    'seller_tax_id',
    'item_name',
    'specification',
    'unit',
    'quantity',
    'unit_price',
    'total_with_tax',
    'amount',
    'tax_amount',
    'tax_rate',
]


def _reset_extracted_fields(invoice: Invoice) -> None:
    """Reset extracted fields to avoid stale values on reprocess."""
    for field_name in COMPARABLE_FIELDS:
        setattr(invoice, field_name, None)


def _run_ocr(file_data: bytes, file_type: str) -> Tuple[str, float, Dict[str, Any]]:
    """Run OCR processing in a separate thread.

    Args:
        file_data: Raw file bytes
        file_type: File type (pdf, jpg, png, etc.)

    Returns:
        Tuple of (raw_text, confidence, extracted_fields)
    """
    ocr_service = get_ocr_service()
    extractor = get_field_extractor()

    if file_type == 'pdf':
        raw_text, confidence, ocr_lines = ocr_service.process_pdf(file_data)
    else:
        raw_text, confidence, ocr_lines = ocr_service.process_image(file_data)

    ocr_fields = extractor.extract_fields(raw_text, ocr_lines)
    return raw_text, confidence, ocr_fields


def _run_llm_vision(
    file_data: bytes,
    file_type: str,
    document_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Run LLM vision parsing in a separate thread.

    Args:
        file_data: Raw file bytes
        file_type: File type (pdf, jpg, png, etc.)

    Returns:
        Dictionary of extracted fields
    """
    llm_service = get_llm_service()

    if not llm_service.is_available or not llm_service.supports_vision():
        return {}

    # Determine MIME type
    mime_map = {
        'pdf': 'application/pdf',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
    }
    mime_type = mime_map.get(file_type, 'image/png')

    # For PDF, we need to convert first page to image
    if file_type == 'pdf':
        try:
            from pdf2image import convert_from_bytes
            from io import BytesIO

            # Use higher DPI (300) for better text recognition in PDFs
            # Some PDFs have text rendered as graphics which need higher resolution
            images = convert_from_bytes(file_data, dpi=300, first_page=1, last_page=1)
            if images:
                # Convert PIL Image to bytes with high quality
                buffer = BytesIO()
                images[0].save(buffer, format='PNG', optimize=False)
                file_data = buffer.getvalue()
                mime_type = 'image/png'
                logger.info(f"PDF converted to image: {images[0].size[0]}x{images[0].size[1]} pixels, {len(file_data)} bytes")
            else:
                logger.warning("Failed to convert PDF to image for LLM vision")
                return {}
        except Exception as e:
            logger.error(f"PDF to image conversion failed: {e}")
            return {}

    return llm_service.parse_invoice_from_image(file_data, mime_type, document_kind)


def _is_railway_ticket_text(raw_text: str) -> bool:
    """Detect the non-tabular layout used by China Railway e-tickets."""
    compact = re.sub(r"\s+", "", raw_text or "")
    return (
        "铁路电子客票" in compact
        or ("电子客票号" in compact and ("12306" in compact or "票价" in compact))
    )


def _extract_railway_ticket_fields(raw_text: str) -> Dict[str, str]:
    """Map railway e-ticket text into the existing invoice fields.

    The ticket has no VAT item table.  Keep its travel information together in
    ``specification`` so it remains visible, searchable, and exportable.
    """
    if not _is_railway_ticket_text(raw_text):
        return {}

    def capture(pattern: str) -> Optional[str]:
        match = re.search(pattern, raw_text, re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else None

    def compact(value: Optional[str]) -> Optional[str]:
        return re.sub(r"\s+", "", value) if value else None

    compact_text = re.sub(r"\s+", "", raw_text)
    item_name = (
        "铁路退票费" if "退票费" in compact_text else
        "铁路改签费" if "改签费" in compact_text else
        "铁路客运服务"
    )
    fields: Dict[str, str] = {
        "item_name": item_name,
        "unit": "张",
        "quantity": "1",
    }
    invoice_number = capture(r"发票号码\s*[:：]\s*(\d{8,20})")
    if invoice_number:
        fields["invoice_number"] = invoice_number

    issue = capture(r"开票日期\s*[:：]\s*((?:\d{4})年\s*(?:\d{1,2})月\s*(?:\d{1,2})日)")
    if issue:
        date_match = re.match(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", issue)
        if date_match:
            year, month, day = date_match.groups()
            fields["issue_date"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    buyer_name = capture(r"购买方名称\s*[:：]\s*(.+?)(?=\s*统一社会信用代码\s*[:：]|$)")
    if buyer_name:
        fields["buyer_name"] = compact(buyer_name) or buyer_name
    buyer_tax_id = capture(r"统一社会信用代码\s*[:：]\s*([A-Z0-9]{15,20})")
    if buyer_tax_id:
        fields["buyer_tax_id"] = buyer_tax_id.upper()
    if "中国铁路" in re.sub(r"\s+", "", raw_text):
        fields["seller_name"] = "中国铁路"

    price = capture(r"[￥¥]\s*(\d+(?:\.\d{1,2})?)")
    if price:
        fields["total_with_tax"] = price
        fields["unit_price"] = price

    route_match = re.search(
        r"([\u4e00-\u9fff]{2,}(?:东|南|西|北|站)?)\s+([GDCZTK]\d{1,5})\s+([\u4e00-\u9fff]{2,}(?:东|南|西|北|站)?)",
        raw_text,
        re.IGNORECASE,
    )
    details: List[str] = []
    if route_match:
        departure, train_number, arrival = route_match.groups()
        details.extend([train_number.upper(), f"{departure}→{arrival}"])
    travel_match = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}:\d{2})", raw_text)
    if travel_match:
        year, month, day, time = travel_match.groups()
        details.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d} {time}")
    seat = capture(r"(\d{1,2}车\s*\d{1,3}[A-Z]?号)")
    if seat:
        details.append(compact(seat) or seat)
    seat_class = capture(r"(商务座|特等座|一等座|二等座|无座|[一二三]等座)")
    if seat_class:
        details.append(seat_class)
    passenger = capture(r"\d{6,}\*+\d{4}\s*([\u4e00-\u9fff]{2,4})")
    if passenger:
        details.append(f"乘车人{passenger}")
    electronic_number = capture(r"电子客票号\s*[:：]\s*(\d{10,})")
    if electronic_number:
        details.append(f"电子客票号{electronic_number}")
    if details:
        fields["specification"] = "；".join(details)[:255]

    return fields


def _parse_tax_qr_payload(payload: str) -> Dict[str, str]:
    """Parse authoritative fields from the standard Chinese tax-invoice QR payload."""
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    parts = [part.strip() for part in payload.split(',')]
    if len(parts) < 6 or not re.fullmatch(r'\d{20}', parts[3]):
        return {}

    fields: Dict[str, str] = {'invoice_number': parts[3]}
    try:
        fields['total_with_tax'] = format(Decimal(parts[4]), 'f')
    except (InvalidOperation, ValueError):
        pass
    try:
        fields['issue_date'] = datetime.strptime(parts[5], '%Y%m%d').strftime('%Y-%m-%d')
    except ValueError:
        pass
    return fields


def _extract_invoice_fields_from_qr(file_data: bytes, file_type: str = 'png') -> Dict[str, str]:
    """Read authoritative invoice number, total and date from the invoice QR code."""
    try:
        import cv2
        import numpy as np

        image_data = file_data
        if file_type.lower() == 'pdf':
            from pdf2image import convert_from_bytes
            from io import BytesIO

            pages = convert_from_bytes(file_data, dpi=300, first_page=1, last_page=1)
            if not pages:
                return {}
            buffer = BytesIO()
            pages[0].save(buffer, format='PNG', optimize=False)
            image_data = buffer.getvalue()

        image = cv2.imdecode(np.frombuffer(image_data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {}
        detector = cv2.QRCodeDetector()
        candidates = [image]
        for scale in (2, 3, 4):
            candidates.append(cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
        for candidate in candidates:
            payload, _, _ = detector.detectAndDecode(candidate)
            if not payload:
                continue
            fields = _parse_tax_qr_payload(payload)
            if fields:
                return fields
    except Exception as exc:
        logger.warning(f"QR invoice-field extraction failed: {exc}")
    return {}


def _extract_invoice_number_from_qr(image_data: bytes) -> Optional[str]:
    """Backward-compatible invoice-number-only QR helper."""
    return _extract_invoice_fields_from_qr(image_data).get('invoice_number')


def _discard_duplicated_party_tax_id(fields: Dict[str, Any]) -> None:
    """Discard a seller tax ID accidentally copied into a different buyer."""
    buyer_tax_id = _normalize_value(fields.get('buyer_tax_id'))
    seller_tax_id = _normalize_value(fields.get('seller_tax_id'))
    buyer_name = _normalize_value(fields.get('buyer_name'))
    seller_name = _normalize_value(fields.get('seller_name'))
    if buyer_tax_id and buyer_tax_id == seller_tax_id and buyer_name != seller_name:
        fields['buyer_tax_id'] = None


def _run_llm_text(
    raw_text: str,
    document_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Use an invoice's embedded PDF text instead of a lossy rendered image."""
    llm_service = get_llm_service()
    if not llm_service.is_available or not raw_text.strip():
        return {}
    return llm_service.parse_invoice_from_text(raw_text, document_kind)


def _has_meaningful_fields(fields: Dict[str, Any]) -> bool:
    """Check if parsed fields contain any meaningful values."""
    if not fields:
        return False
    for value in fields.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


async def process_invoice(invoice_id: int, db: AsyncSession, processing_mode: str = "llm_only") -> bool:
    """Process an invoice: run OCR and/or LLM vision, then compare results.

    Args:
        invoice_id: ID of the invoice to process
        db: Database session
        processing_mode: One of 'ocr_only', 'llm_only', 'ocr_and_llm'

    Returns:
        True if processing succeeded, False otherwise
    """
    try:
        # Get invoice
        query = select(Invoice).where(Invoice.id == invoice_id)
        result = await db.execute(query)
        invoice = result.scalar_one_or_none()

        if not invoice:
            logger.error(f"Invoice {invoice_id} not found")
            return False

        # Run OCR and/or LLM based on processing_mode
        loop = asyncio.get_running_loop()
        qr_fields = await loop.run_in_executor(
            _ocr_executor, _extract_invoice_fields_from_qr, invoice.file_data, invoice.file_type
        )
        if qr_fields:
            logger.info(f"Invoice {invoice_id}: authoritative QR fields found: {sorted(qr_fields)}")

        # If llm_only is requested but the current LLM doesn't support vision,
        # automatically fall back to ocr_and_llm to avoid empty results
        effective_mode = processing_mode
        if processing_mode == "llm_only":
            llm_svc = get_llm_service()
            if not llm_svc.is_available or not llm_svc.supports_vision():
                effective_mode = "ocr_and_llm"
                logger.warning(
                    f"Invoice {invoice_id}: 'llm_only' requested but LLM does not support vision "
                    f"— falling back to 'ocr_and_llm'"
                )

        use_ocr = effective_mode != "llm_only"
        use_llm = effective_mode != "ocr_only"
        document_kind: Optional[str] = None
        logger.info(f"Processing invoice {invoice_id} with mode={effective_mode} (requested={processing_mode})")

        if use_ocr and use_llm:
            ocr_result_data = await loop.run_in_executor(
                _ocr_executor, _run_ocr, invoice.file_data, invoice.file_type
            )
            if _is_railway_ticket_text(ocr_result_data[0]):
                document_kind = "railway_ticket"
                logger.info("Invoice %s detected as railway e-ticket", invoice_id)
            if invoice.file_type == 'pdf' and ocr_result_data[0].strip():
                llm_fields = await loop.run_in_executor(
                    _llm_executor, _run_llm_text, ocr_result_data[0], document_kind
                )
            else:
                llm_fields = await loop.run_in_executor(
                    _llm_executor, _run_llm_vision, invoice.file_data, invoice.file_type, document_kind
                )
        elif use_ocr:
            # OCR-only mode: run OCR first, then auto-fallback to LLM if critical fields are missing
            ocr_result_data = await loop.run_in_executor(_ocr_executor, _run_ocr, invoice.file_data, invoice.file_type)
            if _is_railway_ticket_text(ocr_result_data[0]):
                document_kind = "railway_ticket"
                logger.info("Invoice %s detected as railway e-ticket", invoice_id)
            _, _, _ocr_fields_peek = ocr_result_data
            _critical = ['invoice_number', 'issue_date', 'total_with_tax']
            _missing = [f for f in _critical if not _ocr_fields_peek.get(f)]
            if _missing:
                _llm_svc = get_llm_service()
                if _llm_svc.is_available and _llm_svc.supports_vision():
                    logger.info(
                        f"Invoice {invoice_id}: OCR missing {_missing}, running LLM fallback"
                    )
                    llm_fields = await loop.run_in_executor(
                        _llm_executor, _run_llm_vision, invoice.file_data, invoice.file_type, document_kind
                    )
                else:
                    llm_fields = {}
            else:
                llm_fields = {}
        else:  # llm_only
            if invoice.file_type == 'pdf':
                # Preserve the PDF text layer. Some invoices use fonts that disappear
                # when Poppler renders the page for a vision model.
                ocr_result_data = await loop.run_in_executor(
                    _ocr_executor, _run_ocr, invoice.file_data, invoice.file_type
                )
                if _is_railway_ticket_text(ocr_result_data[0]):
                    document_kind = "railway_ticket"
                    logger.info("Invoice %s detected as railway e-ticket", invoice_id)
                llm_fields = await loop.run_in_executor(
                    _llm_executor, _run_llm_text, ocr_result_data[0], document_kind
                )
            else:
                ocr_result_data = ("", 0.0, {})
                llm_fields = await loop.run_in_executor(
                    _llm_executor, _run_llm_vision, invoice.file_data, invoice.file_type
                )

        # Unpack OCR results
        raw_text, confidence, ocr_fields = ocr_result_data
        railway_fields = _extract_railway_ticket_fields(raw_text)
        if railway_fields:
            ocr_fields.update(railway_fields)
            document_kind = "railway_ticket"
        _discard_duplicated_party_tax_id(ocr_fields)
        _discard_duplicated_party_tax_id(llm_fields)
        # Put QR values on the OCR/comparison side so the original vision result
        # remains visible in diffs while QR wins every conflict.
        ocr_fields.update(qr_fields)
        has_llm = _has_meaningful_fields(llm_fields)

        logger.info(f"OCR completed: {len(ocr_fields)} fields extracted")
        logger.info(f"LLM vision completed: {len(llm_fields)} fields extracted (has_llm={has_llm})")

        # Validate the replacement before touching stored results. An unavailable
        # model currently returns empty fields, which is a failure, not success.
        final_fields, diffs = _compare_and_resolve(
            ocr_fields, llm_fields, has_llm, authoritative_fields=set(qr_fields)
        )
        if not _has_meaningful_fields(final_fields):
            logger.error(f"Invoice {invoice_id}: no valid recognition result; preserving existing data")
            await db.rollback()
            return False
        stale_duplicate_buyer_tax_id = (
            _normalize_value(invoice.buyer_tax_id)
            and _normalize_value(invoice.buyer_tax_id) == _normalize_value(invoice.seller_tax_id)
            and _normalize_value(invoice.buyer_name) != _normalize_value(invoice.seller_name)
        )
        missing_existing = [
            field for field in COMPARABLE_FIELDS
            if _normalize_value(getattr(invoice, field, None)) is not None
            and _normalize_value(final_fields.get(field)) is None
            and not (field == 'buyer_tax_id' and stale_duplicate_buyer_tax_id)
        ]
        if missing_existing:
            logger.error(f"Invoice {invoice_id}: incomplete replacement ({missing_existing}); preserving existing data")
            await db.rollback()
            return False

        # Delete existing OCR, LLM, and diff results for reprocessing
        await db.execute(delete(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id))
        await db.execute(delete(LlmResult).where(LlmResult.invoice_id == invoice_id))
        await db.execute(delete(OcrResult).where(OcrResult.invoice_id == invoice_id))
        logger.info(f"Cleared existing processing results for invoice {invoice_id}")

        # Save OCR result
        ocr_result = OcrResult(
            invoice_id=invoice_id,
            raw_text=raw_text,
            invoice_number=ocr_fields.get('invoice_number'),
            issue_date=ocr_fields.get('issue_date'),
            buyer_name=ocr_fields.get('buyer_name'),
            buyer_tax_id=ocr_fields.get('buyer_tax_id'),
            seller_name=ocr_fields.get('seller_name'),
            seller_tax_id=ocr_fields.get('seller_tax_id'),
            item_name=ocr_fields.get('item_name'),
            specification=ocr_fields.get('specification'),
            unit=ocr_fields.get('unit'),
            quantity=ocr_fields.get('quantity'),
            unit_price=ocr_fields.get('unit_price'),
            total_with_tax=ocr_fields.get('total_with_tax'),
            amount=ocr_fields.get('amount'),
            tax_amount=ocr_fields.get('tax_amount'),
            tax_rate=ocr_fields.get('tax_rate'),
        )
        db.add(ocr_result)

        # Save LLM result if available
        if has_llm:
            llm_result = LlmResult(
                invoice_id=invoice_id,
                invoice_number=llm_fields.get('invoice_number'),
                issue_date=llm_fields.get('issue_date'),
                buyer_name=llm_fields.get('buyer_name'),
                buyer_tax_id=llm_fields.get('buyer_tax_id'),
                seller_name=llm_fields.get('seller_name'),
                seller_tax_id=llm_fields.get('seller_tax_id'),
                item_name=llm_fields.get('item_name'),
                specification=llm_fields.get('specification'),
                unit=llm_fields.get('unit'),
                quantity=llm_fields.get('quantity'),
                unit_price=llm_fields.get('unit_price'),
                total_with_tax=llm_fields.get('total_with_tax'),
                amount=llm_fields.get('amount'),
                tax_amount=llm_fields.get('tax_amount'),
                tax_rate=llm_fields.get('tax_rate'),
            )
            db.add(llm_result)
        else:
            logger.info(f"LLM vision not available - invoice {invoice_id} using OCR-only flow")

        # Clear extracted fields so missing values don't keep stale data
        _reset_extracted_fields(invoice)

        # Save parsing diffs
        for diff in diffs:
            parsing_diff = ParsingDiff(
                invoice_id=invoice_id,
                field_name=diff['field_name'],
                ocr_value=diff['ocr_value'],
                llm_value=diff['llm_value'],
                final_value=diff['final_value'],
                source=diff['source'],
                resolved=0 if diff['needs_review'] else 1,
            )
            db.add(parsing_diff)

        # Update invoice with final data
        _update_invoice_from_fields(invoice, final_fields)
        await match_imported_approval(db, invoice)
        await propagate_approval_to_reviewing_siblings(db, invoice)

        # Set status based on whether review is needed
        # Check for conflicts in diffs
        has_conflicts = any(d['needs_review'] for d in diffs)

        # Check for missing critical fields (these require review)
        critical_fields = [
            'invoice_number',
            'issue_date',
            'total_with_tax',
            'buyer_name',
            'buyer_tax_id',
            'seller_name',
            'seller_tax_id',
            'item_name',
        ]
        missing_fields = [f for f in critical_fields if not final_fields.get(f)]
        missing_critical = bool(missing_fields)
        if not final_fields.get('invoice_number'):
            invoice.document_type = "voucher"
        if missing_critical:
            logger.warning(f"Invoice {invoice_id} missing critical fields: {missing_fields}")

        # Keep unmatched invoices editable until the host-side DingTalk lookup
        # has had a chance to attach the approval number and originator.
        needs_review = has_conflicts or missing_critical or not invoice.approval_instance_id or not invoice.owner
        if invoice.approval_claim_amount is not None and invoice.total_with_tax is not None:
            if abs(invoice.approval_claim_amount - invoice.total_with_tax) > Decimal('0.01'):
                needs_review = True
                logger.warning('Invoice %s amount differs from approval claim: %s vs %s', invoice_id, invoice.total_with_tax, invoice.approval_claim_amount)
        if needs_review:
            invoice.status = InvoiceStatus.REVIEWING
        else:
            invoice.status = InvoiceStatus.CONFIRMED

        await db.commit()
        logger.info(f"Invoice {invoice_id} processed successfully (needs_review={needs_review})")
        return True

    except Exception as e:
        logger.error(f"Failed to process invoice {invoice_id}: {e}")
        await db.rollback()
        return False


def _compare_and_resolve(
    ocr_fields: Dict[str, Any],
    llm_fields: Dict[str, Any],
    has_llm: bool,
    authoritative_fields: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Compare OCR and LLM results, resolve differences.

    Args:
        ocr_fields: Fields extracted by OCR
        llm_fields: Fields extracted by LLM
        has_llm: Whether LLM results are available

    Returns:
        Tuple of (final_fields, diffs_list)
    """
    final_fields = {}
    diffs = []

    for field_name in COMPARABLE_FIELDS:
        ocr_value = _normalize_value(ocr_fields.get(field_name))
        llm_value = _normalize_value(llm_fields.get(field_name)) if has_llm else None

        # A detail-table header is an OCR artifact, not a real field value.
        if field_name == 'specification' and ocr_value:
            header_words = ('项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额')
            if any(word in str(ocr_value) for word in header_words):
                ocr_value = None

        if field_name == 'invoice_number':
            ocr_value = _sanitize_invoice_number(ocr_value)
            llm_value = _sanitize_invoice_number(llm_value)

        # QR fields are authoritative. Vision/OCR values remain in the diff so
        # users can inspect any disagreement without corrupting the stored data.
        if authoritative_fields and field_name in authoritative_fields and ocr_value:
            final_value = ocr_value
            source = 'qr'
            needs_review = False
        elif not has_llm:
            # No LLM available, use OCR value directly
            final_value = ocr_value
            source = 'ocr'
            needs_review = False
        elif _values_are_equal(field_name, ocr_value, llm_value):
            # Values match (including numeric equivalence like 330.00 == 330)
            final_value = ocr_value or llm_value
            source = 'matched'
            needs_review = False
        elif ocr_value and llm_value:
            # Both have values but differ — prefer LLM, no manual review needed
            final_value = llm_value
            source = 'llm'
            needs_review = False
        elif llm_value and not ocr_value:
            # LLM found something OCR missed - prefer LLM
            final_value = llm_value
            source = 'llm'
            needs_review = False
        else:
            # OCR found something LLM missed - prefer OCR
            final_value = ocr_value
            source = 'ocr'
            needs_review = False

        if field_name == 'invoice_number' and final_value:
            # Keep suspicious results visible, but require verification.
            if len(final_value) not in (8, 10, 20):
                needs_review = True

        final_fields[field_name] = final_value

        # Record diff if there's a discrepancy or both have values
        if (has_llm and (ocr_value or llm_value)) or needs_review:
            diffs.append({
                'field_name': field_name,
                'ocr_value': ocr_value,
                'llm_value': llm_value,
                'final_value': final_value,
                'source': source,
                'needs_review': needs_review,
            })

    # Post-process: sanitize invoice_number
    if final_fields.get('invoice_number'):
        final_fields['invoice_number'] = _sanitize_invoice_number(final_fields['invoice_number'])

    return final_fields, diffs


def _sanitize_invoice_number(raw: Optional[str]) -> Optional[str]:
    """Remove formatting noise without truncating recognized digits.

    An unexpected length is evidence for review, not permission to extract a
    shorter prefix. Preserve leading zeros and normalize full-width digits.
    """
    if not raw:
        return None
    import unicodedata
    normalized = unicodedata.normalize('NFKC', str(raw))
    digits_only = re.sub(r'[^0-9]', '', normalized)
    return digits_only if len(digits_only) >= 6 else None


def _normalize_value(value: Any) -> Optional[str]:
    """Normalize a value to string for comparison."""
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str:
        return None
    return value_str


# Fields that should be compared as numbers
NUMERIC_FIELDS = ['total_with_tax', 'amount', 'tax_amount']


def _values_are_equal(field_name: str, value1: Optional[str], value2: Optional[str]) -> bool:
    """Compare two values, using numeric comparison for numeric fields.

    Args:
        field_name: Name of the field being compared
        value1: First value (normalized string)
        value2: Second value (normalized string)

    Returns:
        True if values are considered equal
    """
    # If both are None or empty, they're equal
    if not value1 and not value2:
        return True

    # If only one is None/empty, they're not equal
    if not value1 or not value2:
        return False

    # For numeric fields, compare as numbers
    if field_name in NUMERIC_FIELDS:
        try:
            from decimal import Decimal, InvalidOperation
            # Remove any currency symbols and whitespace
            clean1 = value1.replace('¥', '').replace('￥', '').replace(',', '').strip()
            clean2 = value2.replace('¥', '').replace('￥', '').replace(',', '').strip()
            num1 = Decimal(clean1)
            num2 = Decimal(clean2)
            return num1 == num2
        except (InvalidOperation, ValueError):
            # Fall back to string comparison if not valid numbers
            pass

    # Default to string comparison
    return value1 == value2


def _update_invoice_from_fields(invoice: Invoice, fields: dict) -> None:
    """Update invoice fields from extracted data."""
    from datetime import datetime
    from decimal import Decimal

    if fields.get('invoice_number'):
        invoice.invoice_number = fields['invoice_number']

    if fields.get('issue_date'):
        try:
            invoice.issue_date = datetime.strptime(fields['issue_date'], '%Y-%m-%d').date()
        except ValueError:
            pass

    if fields.get('buyer_name'):
        invoice.buyer_name = fields['buyer_name']

    if fields.get('buyer_tax_id'):
        invoice.buyer_tax_id = fields['buyer_tax_id']

    if fields.get('seller_name'):
        invoice.seller_name = fields['seller_name']

    if fields.get('seller_tax_id'):
        invoice.seller_tax_id = fields['seller_tax_id']

    if fields.get('item_name'):
        invoice.item_name = fields['item_name']

    if fields.get('specification'):
        invoice.specification = fields['specification']
    if fields.get('unit'):
        invoice.unit = fields['unit']
    for numeric_field in ('quantity', 'unit_price'):
        if fields.get(numeric_field):
            try:
                setattr(invoice, numeric_field, Decimal(str(fields[numeric_field]).replace(',', '')))
            except (ValueError, TypeError):
                pass

    if fields.get('total_with_tax'):
        try:
            invoice.total_with_tax = Decimal(fields['total_with_tax'])
        except (ValueError, TypeError):
            pass

    if fields.get('amount'):
        try:
            invoice.amount = Decimal(fields['amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_amount'):
        try:
            invoice.tax_amount = Decimal(fields['tax_amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_rate'):
        invoice.tax_rate = fields['tax_rate']


def check_llm_available() -> bool:
    """Check if LLM service is available."""
    return get_llm_service().is_available
