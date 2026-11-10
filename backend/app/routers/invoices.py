from typing import Optional, List
import json
from io import BytesIO
from datetime import date
import asyncio
import hashlib
import os
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query, BackgroundTasks, Request, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, update, text
from decimal import Decimal

from app.services.company_service import company_key_expression, choose_canonical_company_names
from app.database import get_db
from app.models.invoice import Invoice, OcrResult, LlmResult, ParsingDiff, InvoiceStatus
from app.models.user import User as UserModel
from app.schemas.invoice import (
    InvoiceResponse, InvoiceListResponse, InvoiceDetailResponse,
    InvoiceUpdate, BatchUpdateRequest, BatchDeleteRequest, StatisticsResponse, UploadResponse,
    ResolveDiffRequest, DuplicateGroup, DuplicateCheckResponse
)
from app.config import get_settings
from app.services.audit_service import log_audit_no_commit, get_client_info
from app.rate_limit import limiter
from app.auth import get_current_user
from app.models.user import User

settings = get_settings()
router = APIRouter()

# 后台解析的并发上限。每个后台任务全程持有一条数据库连接，而 LLM 单次最长 60s、
# 重试 4 次就是几分钟；批量导入时若放任几十个任务并行，连接池（默认 15 条）会被占满，
# 之后连 /api/health 都会挂住 —— 整个服务「活着但没反应」。2026-09-21 批量回补时真实踩到。
# 可以用环境变量 BACKGROUND_PROCESSING_CONCURRENCY 调整（1~16）。
BACKGROUND_PROCESSING_CONCURRENCY = max(
    1, min(16, int(os.environ.get("BACKGROUND_PROCESSING_CONCURRENCY", "4")))
)
_BACKGROUND_PROCESSING_SLOTS = asyncio.Semaphore(BACKGROUND_PROCESSING_CONCURRENCY)
APPROVAL_INDEX = Path(os.environ.get('DINGTALK_IMPORT_QUEUE_DIR', '/dingtalk-import-queue')) / 'approvals.json'


def _inactive_approval_numbers() -> set[str]:
    """Use the import worker's refreshed DingTalk approval index for duplicate checks."""
    try:
        records = json.loads(APPROVAL_INDEX.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    inactive = set()
    for number, record in records.items():
        if not isinstance(record, dict):
            continue
        status = str(record.get('status') or '').upper()
        result = str(record.get('result') or '').lower()
        if status in {'TERMINATED', 'CANCELLED', 'CANCELED', 'REVOKED'} or (
            status == 'COMPLETED' and result in {'refuse', 'reject', 'rejected', 'disagree'}
        ):
            inactive.add(str(number).strip())
    return inactive


async def _cross_approval_duplicate_evidence(
    db: AsyncSession,
    invoice: Invoice,
) -> list[str]:
    """Return distinct approval numbers when this invoice is duplicate-report evidence.

    The same invoice number appearing under different approval numbers is evidence of a
    possible duplicate reimbursement. Neither copy may be deleted from any UI or API.
    """
    invoice_number = (invoice.invoice_number or "").strip()
    approval_number = (invoice.approval_instance_id or "").strip()
    if not invoice_number or not approval_number:
        return []

    result = await db.execute(
        select(Invoice.approval_instance_id).where(
            Invoice.document_type == "invoice",
            func.trim(Invoice.invoice_number) == invoice_number,
            Invoice.approval_instance_id.is_not(None),
            func.trim(Invoice.approval_instance_id) != "",
        )
    )
    approval_numbers = {
        str(value).strip()
        for value in result.scalars().all()
        if value and str(value).strip()
    }
    approval_numbers -= _inactive_approval_numbers()
    return sorted(approval_numbers) if len(approval_numbers) >= 2 else []


async def _reject_evidence_deletion(db: AsyncSession, invoice: Invoice) -> None:
    approval_numbers = await _cross_approval_duplicate_evidence(db, invoice)
    if approval_numbers:
        raise HTTPException(
            status_code=409,
            detail=(
                f"发票号 {invoice.invoice_number} 同时出现在不同审批单 "
                f"{', '.join(approval_numbers)}，属于疑似重复报销证据，禁止删除任何一条记录"
            ),
        )


async def resume_interrupted_processing() -> None:
    """Resume uploads left behind by a process restart.

    FastAPI BackgroundTasks live only in the web process.  A container restart
    used to leave their rows permanently in ``已上传`` or ``解析中``.  The rows
    themselves are durable, so rebuild the in-memory work queue from them at
    startup.  The shared semaphore still limits actual parsing concurrency.
    """
    from app.database import async_session_maker

    async with async_session_maker() as db:
        # No task can survive a process restart.  Reclassify any inherited
        # in-flight rows before rebuilding the queue so the UI only shows the
        # small number of jobs actually running in this server process.
        await db.execute(
            update(Invoice)
            .where(
                Invoice.document_type == "invoice",
                Invoice.status == InvoiceStatus.PROCESSING,
            )
            .values(status=InvoiceStatus.UPLOADED)
        )
        await db.commit()

        result = await db.execute(
            select(Invoice.id).where(
                Invoice.document_type == "invoice",
                Invoice.status == InvoiceStatus.UPLOADED,
            ).order_by(Invoice.created_at)
        )
        invoice_ids = list(result.scalars())

    for invoice_id in invoice_ids:
        # Recovery must be resilient to a vision gateway that returns an empty
        # response for some image formats.  OCR keeps the queue moving and the
        # LLM still validates/extracts fields when it can.
        # Historical uploads have already spent time in the failed queue.  One
        # complete OCR + vision pass is sufficient for recovery; files with no
        # invoice fields are retained in the voucher archive instead of being
        # retried indefinitely.
        asyncio.create_task(
            process_invoice_background(invoice_id, "ocr_and_llm", max_retries=0)
        )
        # Let the event loop start queued tasks without blocking application startup.
        await asyncio.sleep(0)


def _parse_invoice_ids(invoice_ids: Optional[str]) -> Optional[List[int]]:
    if not invoice_ids:
        return None
    ids: List[int] = []
    for raw_id in invoice_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            ids.append(int(raw_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="发票ID必须为整数") from exc
    if not ids:
        raise HTTPException(status_code=400, detail="发票ID不能为空")
    return ids


def _parse_date_param(value: Optional[str], field_name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} 日期格式无效，应为 YYYY-MM-DD") from exc


def _apply_user_filter(query, current_user: User):
    """All signed-in users share the same invoice read scope."""
    return query


def _apply_write_filter(query, current_user: User):
    """Keep mutations limited to the uploader or an administrator."""
    if not current_user.is_admin:
        query = query.where(Invoice.uploaded_by_id == current_user.id)
    return query


def _check_invoice_access(invoice: Invoice, current_user: User):
    """非管理员只能操作自己上传的发票"""
    if not current_user.is_admin and invoice.uploaded_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问此发票")


@router.post("/upload", response_model=List[UploadResponse])
@limiter.limit("10/minute")
async def upload_invoices(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    processing_mode: str = Form("llm_only"),
    owner: Optional[str] = Form(None),
    approval_instance_id: Optional[str] = Form(None),
    approval_claim_amount: Optional[Decimal] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """上传发票文件 (支持多文件)，上传后异步触发OCR解析"""
    results = []
    invoice_ids_to_process = []
    client_info = get_client_info(request)

    for file in files:
        # Validate file type
        ext = file.filename.split(".")[-1].lower() if file.filename else ""
        if ext not in settings.allowed_extensions:
            results.append(UploadResponse(
                id=0,
                file_name=file.filename or "unknown",
                status="error",
                message=f"不支持的文件类型: {ext}"
            ))
            continue

        # Read file content
        content = await file.read()

        # Validate file size
        if len(content) > settings.max_file_size:
            results.append(UploadResponse(
                id=0,
                file_name=file.filename or "unknown",
                status="error",
                message=f"文件过大，最大支持 {settings.max_file_size // 1024 // 1024}MB"
            ))
            continue

        # The same approval attachment can arrive through manual import and
        # scheduled scans. Serialize this approval/file pair across requests,
        # then compare the original bytes before creating another invoice row.
        if approval_instance_id:
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"{approval_instance_id}:{hashlib.sha256(content).hexdigest()}"},
                )
            existing = await db.execute(
                select(Invoice.id).where(
                    Invoice.approval_instance_id == approval_instance_id,
                    Invoice.file_data == content,
                ).limit(1)
            )
            existing_id = existing.scalar_one_or_none()
            if existing_id is not None:
                results.append(UploadResponse(
                    id=existing_id,
                    file_name=file.filename or "unknown",
                    status="success",
                    message="附件已存在，已跳过重复上传",
                ))
                continue

        # Create invoice record with UPLOADED status (not yet processed)
        invoice = Invoice(
            file_name=file.filename or "unknown",
            file_type=ext,
            file_data=content,
            status=InvoiceStatus.UPLOADED,
            uploaded_by_id=current_user.id,
            # Preserve trusted source metadata at creation time.  This is needed
            # for DingTalk attachment imports because processing may confirm the
            # invoice before a follow-up edit can run.
            owner=owner,
            approval_instance_id=approval_instance_id,
            approval_claim_amount=approval_claim_amount,
            # 所有上传先进入发票识别流程；未识别到发票号码的单据会由
            # invoice_service 自动迁入凭证归档，避免人为误放进发票池。
            document_type="invoice",
        )
        db.add(invoice)
        await db.flush()

        invoice_ids_to_process.append(invoice.id)

        # Audit log for upload
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="upload",
            new_value={"file_name": invoice.file_name, "file_type": ext, "file_size": len(content)},
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )

        results.append(UploadResponse(
            id=invoice.id,
            file_name=invoice.file_name,
            status="success",
            message="上传成功，等待解析"
        ))

    await db.commit()

    # Schedule background processing for each uploaded invoice
    for invoice_id in invoice_ids_to_process:
        background_tasks.add_task(process_invoice_background, invoice_id, processing_mode)

    return results


async def process_invoice_background(invoice_id: int, processing_mode: str = "llm_only", max_retries: int = 3):
    """Background task to process an invoice with OCR/LLM.

    Implements exponential backoff retry logic for transient failures.
    Total attempts = 1 (initial) + max_retries, with delays of 2, 4, 8 seconds
    between retries.

    Args:
        invoice_id: ID of the invoice to process
        processing_mode: One of 'ocr_only', 'llm_only', 'ocr_and_llm'
        max_retries: Maximum number of retries after the initial attempt (default: 3,
            resulting in up to 4 total attempts)

    ⚠️ 并发闸门：这个任务从头到尾持有一条数据库连接，而 LLM 单次最长 60s、
    重试 4 次就是几分钟。批量导入时上传每 7 秒来一个，几十个后台任务同时压在
    连接池上（默认 pool_size=5 + max_overflow=10 = 15 条）→ 池子被占满，
    之后**所有**请求（包括 /api/health）都会无限期挂住，整个服务看着是「活着但没反应」。
    2026-09-21 批量回补时真实踩到，所以用信号量把并发的后台解析限死在几条以内。
    """
    from app.services.invoice_service import process_invoice as do_process
    from app.database import async_session_maker
    from app.services.audit_service import log_audit
    import logging
    import asyncio
    logger = logging.getLogger(__name__)

    async with _BACKGROUND_PROCESSING_SLOTS:
        retry_count = 0
        last_error = None
        original_status = None

        while retry_count <= max_retries:
            async with async_session_maker() as db:
                try:
                    # Update status to PROCESSING
                    query = select(Invoice).where(Invoice.id == invoice_id)
                    result = await db.execute(query)
                    invoice = result.scalar_one_or_none()

                    if not invoice:
                        logger.error(f"Invoice {invoice_id} not found")
                        return

                    if invoice.status == InvoiceStatus.CONFIRMED:
                        logger.info("Skip processing locked invoice %s", invoice_id)
                        return

                    if original_status is None:
                        # PROCESSING is an in-flight state, not a recoverable
                        # business state.  It can be left behind by a process
                        # restart, so exhausted retries must return it to the
                        # visible, retryable uploaded state instead of keeping
                        # a perpetual "解析中" badge.
                        original_status = (
                            InvoiceStatus.UPLOADED
                            if invoice.status == InvoiceStatus.PROCESSING
                            else invoice.status
                        )
                    invoice.status = InvoiceStatus.PROCESSING
                    await db.commit()

                    logger.info(f"Background processing invoice {invoice_id} mode={processing_mode} (attempt {retry_count + 1}/{max_retries + 1})")
                    success = await do_process(invoice_id, db, processing_mode)

                    if success:
                        logger.info(f"Invoice {invoice_id} processing completed successfully")
                        # Log successful processing
                        await log_audit(
                            db=db,
                            entity_type="invoice",
                            entity_id=invoice_id,
                            action="process_complete",
                            new_value={"status": "success", "attempts": retry_count + 1}
                        )
                        await db.refresh(invoice)
                        from app.services.approval_match_service import queue_dingtalk_lookup
                        queue_dingtalk_lookup(invoice)
                        from app.services.dingtalk_sync_service import (
                            resolve_sync_sequence,
                            sync_invoice_to_dingtalk,
                        )
                        synced = await sync_invoice_to_dingtalk(
                            invoice, await resolve_sync_sequence(db, invoice)
                        )
                        logger.info("Invoice %s DingTalk sync result: %s", invoice_id, synced)
                        return
                    else:
                        last_error = "Processing returned false"
                        logger.warning(f"Invoice {invoice_id} processing returned false")
                        # A configured vision model can still return an empty
                        # result for a particular image.  Retry through OCR so
                        # text-bearing invoices are not retried identically.
                        if processing_mode == "llm_only":
                            processing_mode = "ocr_and_llm"
                            logger.info(
                                "Invoice %s retry will use OCR + LLM fallback",
                                invoice_id,
                            )

                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Failed to process invoice {invoice_id} (attempt {retry_count + 1}): {e}")
                    await db.rollback()

            retry_count += 1

            if retry_count <= max_retries:
                # Exponential backoff: 2^retry_count seconds (2, 4, 8 seconds)
                delay = 2 ** retry_count
                logger.info(f"Retrying invoice {invoice_id} in {delay} seconds...")
                await asyncio.sleep(delay)

        # All retries exhausted - mark as failed
        async with async_session_maker() as db:
            try:
                query = select(Invoice).where(Invoice.id == invoice_id)
                result = await db.execute(query)
                invoice = result.scalar_one_or_none()

                if invoice:
                    # New uploads with no usable result after all OCR + vision
                    # attempts are not invoices that can remain in the invoice
                    # list.  Preserve their original file in the voucher archive
                    # for review, without sending it to DingTalk or retrying it
                    # forever.  Existing reviewed/confirmed invoices keep their
                    # prior business state when a later reparse fails.
                    archive_unrecognized = (
                        original_status == InvoiceStatus.UPLOADED
                        and last_error == "Processing returned false"
                    )
                    if archive_unrecognized:
                        invoice.document_type = "voucher"
                        invoice.status = InvoiceStatus.REVIEWING
                    else:
                        invoice.status = original_status or InvoiceStatus.UPLOADED
                    await db.commit()

                    # Keep a durable trail for either a real processing failure
                    # or automatic archive classification.
                    await log_audit(
                        db=db,
                        entity_type="invoice",
                        entity_id=invoice_id,
                        action=("archive_unrecognized" if archive_unrecognized else "process_failed"),
                        new_value={
                            "error": last_error,
                            "attempts": max_retries + 1,
                            "document_type": "voucher" if archive_unrecognized else "invoice",
                        }
                    )

                if invoice and archive_unrecognized:
                    logger.info(
                        "Invoice %s had no valid invoice fields after %s attempts; archived as voucher",
                        invoice_id,
                        max_retries + 1,
                    )
                else:
                    logger.error(f"Invoice {invoice_id} processing failed after {max_retries + 1} attempts: {last_error}")
            except Exception as e:
                logger.error(f"Failed to update invoice {invoice_id} status after retry exhaustion: {e}")


@router.get("", response_model=InvoiceListResponse)
async def list_invoices(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    invoice_number: Optional[str] = Query(None, description="发票号码（模糊）"),
    keyword: Optional[str] = Query(None, description="关键词（发票号/购买方/销售方）"),
    company_key: Optional[str] = Query(None, description="购买方公司分类"),
    party_type: Optional[str] = Query(None, pattern="^(company|person)$", description="购买方类型"),
    buyer_keyword: Optional[str] = Query(None, description="购买方名称关键词"),
    approval_number: Optional[str] = Query(None, description="审批编号"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取发票列表"""
    query = select(Invoice)
    query = _apply_user_filter(query, current_user)
    query = query.where(Invoice.document_type == "invoice")

    if company_key is not None:
        query = query.where(company_key_expression() == company_key)
    buyer_tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    buyer_name = func.nullif(func.trim(Invoice.buyer_name), '')
    if party_type == "company":
        query = query.where(buyer_tax_id.is_not(None))
    elif party_type == "person":
        query = query.where(buyer_tax_id.is_(None), buyer_name.is_not(None))
    if buyer_keyword:
        query = query.where(Invoice.buyer_name.ilike(f"%{buyer_keyword}%"))
    if approval_number:
        query = query.where(Invoice.approval_instance_id == approval_number)

    # Apply filters
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    if start_date:
        try:
            start_date_obj = date.fromisoformat(start_date)
            query = query.where(Invoice.issue_date >= start_date_obj)
        except ValueError:
            pass  # Invalid date format, skip filter
    if end_date:
        try:
            end_date_obj = date.fromisoformat(end_date)
            query = query.where(Invoice.issue_date <= end_date_obj)
        except ValueError:
            pass  # Invalid date format, skip filter
    if invoice_number:
        query = query.where(Invoice.invoice_number.ilike(f"%{invoice_number}%"))
    if keyword:
        from sqlalchemy import or_
        query = query.where(or_(
            Invoice.invoice_number.ilike(f"%{keyword}%"),
            Invoice.approval_instance_id.ilike(f"%{keyword}%"),
            Invoice.buyer_name.ilike(f"%{keyword}%"),
            Invoice.seller_name.ilike(f"%{keyword}%"),
        ))

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0

    # Apply pagination
    query = query.order_by(Invoice.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    invoices = result.scalars().all()

    # Batch-load uploaders to avoid N+1
    uploader_ids = {inv.uploaded_by_id for inv in invoices if inv.uploaded_by_id}
    uploaders: dict[int, UserModel] = {}
    if uploader_ids:
        u_result = await db.execute(select(UserModel).where(UserModel.id.in_(uploader_ids)))
        for u in u_result.scalars().all():
            uploaders[u.id] = u

    def _enrich(inv: Invoice) -> InvoiceResponse:
        resp = InvoiceResponse.model_validate(inv)
        if inv.uploaded_by_id and inv.uploaded_by_id in uploaders:
            u = uploaders[inv.uploaded_by_id]
            resp.uploaded_by_username = u.username
            resp.uploaded_by_display_name = u.display_name
        return resp

    return InvoiceListResponse(
        items=[_enrich(inv) for inv in invoices],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/sync-records", response_model=InvoiceListResponse)
async def list_sync_records(
    approval_number: str = Query(..., min_length=1, description="审批编号"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return every backend source record for one approval, without business filtering.

    This endpoint is intentionally different from the invoice-list endpoint: DingTalk
    ledger synchronization mirrors the source records exactly, so vouchers, empty
    invoice numbers, and duplicate invoice numbers are all included. ``Invoice.id`` is
    the stable technical identity; invoice number is display data only.
    """
    query = _apply_user_filter(
        select(Invoice).where(Invoice.approval_instance_id == approval_number),
        current_user,
    )
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0
    result = await db.execute(
        query.order_by(Invoice.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    records = result.scalars().all()
    return InvoiceListResponse(
        items=[InvoiceResponse.model_validate(record) for record in records],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/processing-status")
async def get_processing_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return parsing progress independently of the current list page."""
    invoice_query = select(Invoice.status, func.count()).where(
        Invoice.document_type == "invoice"
    )
    invoice_query = _apply_user_filter(invoice_query, current_user)
    invoice_rows = (await db.execute(invoice_query.group_by(Invoice.status))).all()
    counts = {
        (status.value if isinstance(status, InvoiceStatus) else str(status)): count
        for status, count in invoice_rows
    }

    voucher_query = select(func.count()).select_from(Invoice).where(
        Invoice.document_type == "voucher"
    )
    voucher_query = _apply_user_filter(voucher_query, current_user)
    archived_vouchers = await db.scalar(voucher_query) or 0

    queued = counts.get(InvoiceStatus.UPLOADED.value, 0)
    processing = counts.get(InvoiceStatus.PROCESSING.value, 0)
    return {
        "queued": queued,
        "processing": processing,
        "pending": queued + processing,
        "reviewing": counts.get(InvoiceStatus.REVIEWING.value, 0),
        "confirmed": counts.get(InvoiceStatus.CONFIRMED.value, 0),
        "archived_vouchers": archived_vouchers,
    }


@router.get("/vouchers", response_model=InvoiceListResponse)
async def list_vouchers(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = _apply_user_filter(select(Invoice).where(Invoice.document_type == "voucher"), current_user).order_by(Invoice.created_at.desc())
    rows = (await db.execute(query)).scalars().all()
    return InvoiceListResponse(items=[InvoiceResponse.model_validate(row) for row in rows], total=len(rows), page=1, page_size=len(rows) or 20)


@router.get("/companies")
async def list_companies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate only accessible invoices by buyer tax ID, falling back to name."""
    key = company_key_expression()
    company_tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    query = select(
        key.label("key"),
        func.max(func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')).label("tax_id"),
        func.count(Invoice.id).label("invoice_count"),
        func.coalesce(func.sum(Invoice.total_with_tax), 0).label("total_amount"),
        func.sum(case((Invoice.total_with_tax.is_(None), 1), else_=0)).label("missing_amount_count"),
        func.sum(case((Invoice.status == InvoiceStatus.REVIEWING, 1), else_=0)).label("review_count"),
    ).where(company_tax_id.is_not(None)).group_by(key).order_by(func.count(Invoice.id).desc(), key)
    rows = (await db.execute(_apply_user_filter(query, current_user))).mappings().all()

    normalized_name = func.nullif(func.trim(Invoice.buyer_name), '')
    name_query = select(
        key.label("key"),
        normalized_name.label("name"),
        func.count(Invoice.id).label("occurrences"),
        func.max(Invoice.issue_date).label("latest_issue_date"),
        func.max(Invoice.id).label("latest_id"),
    ).where(
        company_tax_id.is_not(None),
        normalized_name.is_not(None),
    ).group_by(key, normalized_name)
    name_rows = (
        await db.execute(_apply_user_filter(name_query, current_user))
    ).mappings().all()
    canonical_names = choose_canonical_company_names(name_rows)

    return {
        "items": [
            dict(
                row,
                name=canonical_names.get(row["key"]),
                total_amount=str(row["total_amount"]),
            )
            for row in rows
        ]
    }


@router.get("/persons")
async def list_persons(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate accessible invoices whose buyer has a name but no tax ID."""
    key = company_key_expression()
    name = func.nullif(func.trim(Invoice.buyer_name), '')
    tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    query = select(
        key.label("key"),
        name.label("name"),
        func.count(Invoice.id).label("invoice_count"),
        func.coalesce(func.sum(Invoice.total_with_tax), 0).label("total_amount"),
        func.sum(case((Invoice.total_with_tax.is_(None), 1), else_=0)).label("missing_amount_count"),
        func.sum(case((Invoice.status == InvoiceStatus.REVIEWING, 1), else_=0)).label("review_count"),
    ).where(
        tax_id.is_(None),
        name.is_not(None),
    ).group_by(key, name).order_by(func.count(Invoice.id).desc(), name)
    rows = (await db.execute(_apply_user_filter(query, current_user))).mappings().all()
    return {
        "items": [dict(row, total_amount=str(row["total_amount"])) for row in rows]
    }


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics(
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取发票统计数据"""
    query = select(Invoice).where(Invoice.document_type == "invoice")
    query = _apply_user_filter(query, current_user)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)

    result = await db.execute(query)
    invoices = result.scalars().all()

    count = len(invoices)
    total_amount = sum((inv.amount or Decimal(0)) for inv in invoices)
    total_tax = sum((inv.tax_amount or Decimal(0)) for inv in invoices)
    total_with_tax = sum((inv.total_with_tax or Decimal(0)) for inv in invoices)

    return StatisticsResponse(
        count=count,
        total_amount=total_amount,
        total_tax=total_tax,
        total_with_tax=total_with_tax
    )


def _cross_approval_duplicate_candidates(
    all_invoices: list[Invoice], inactive_approvals: Optional[set[str]] = None
) -> dict[str, list[Invoice]]:
    """Return one representative per approval only when a number spans approvals."""
    from collections import defaultdict
    by_number: dict[str, dict[str, Invoice]] = defaultdict(dict)
    inactive_approvals = inactive_approvals or set()
    for invoice in all_invoices:
        invoice_number = (invoice.invoice_number or "").strip()
        approval_number = (invoice.approval_instance_id or "").strip()
        # 空值、占位符或短识别片段不属于发票号码，绝不能互相判重。
        if invoice_number.isdigit() and len(invoice_number) >= 6 and approval_number and approval_number not in inactive_approvals:
            # 同审批号的多次上传只保留一个代表；它们属于导入去重，
            # 不应挤占“跨审批重复报销”的查重页面。
            by_number[invoice_number].setdefault(approval_number, invoice)

    return {
        number: list(approvals.values())
        for number, approvals in by_number.items()
        if len(approvals) >= 2
    }


@router.get("/duplicates", response_model=DuplicateCheckResponse)
async def check_duplicates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Only the same valid invoice number across different approval numbers counts."""
    result = await db.execute(
        _apply_user_filter(select(Invoice), current_user)
        .where(Invoice.document_type == "invoice")
        .order_by(Invoice.id)
    )
    by_number = _cross_approval_duplicate_candidates(result.scalars().all(), _inactive_approval_numbers())
    groups = [
        DuplicateGroup(
            reason="同发票号出现在不同审批编号",
            duplicate_key=invoice_number,
            invoices=[InvoiceResponse.model_validate(invoice) for invoice in invoices],
        )
        for invoice_number, invoices in by_number.items()
    ]
    total_duplicates = sum(len(group.invoices) for group in groups)
    return DuplicateCheckResponse(
        total_groups=len(groups),
        total_duplicates=total_duplicates,
        groups=groups,
    )


@router.get("/{invoice_id}/adjacent")
async def get_adjacent_invoices(
    invoice_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取相邻发票 ID（按 created_at 排序）"""
    prev_query = (
        _apply_user_filter(select(Invoice.id), current_user)
        .where(Invoice.id < invoice_id)
        .order_by(Invoice.id.desc())
        .limit(1)
    )
    next_query = (
        _apply_user_filter(select(Invoice.id), current_user)
        .where(Invoice.id > invoice_id)
        .order_by(Invoice.id.asc())
        .limit(1)
    )
    prev_result = await db.execute(prev_query)
    next_result = await db.execute(next_query)
    prev_id = prev_result.scalar_one_or_none()
    next_id = next_result.scalar_one_or_none()
    return {"prev_id": prev_id, "next_id": next_id}


@router.get("/{invoice_id}", response_model=InvoiceDetailResponse)
async def get_invoice(
    invoice_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取发票详情"""
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Load related data
    ocr_query = select(OcrResult).where(OcrResult.invoice_id == invoice_id)
    ocr_result = await db.execute(ocr_query)
    ocr = ocr_result.scalar_one_or_none()

    llm_query = select(LlmResult).where(LlmResult.invoice_id == invoice_id)
    llm_result = await db.execute(llm_query)
    llm = llm_result.scalar_one_or_none()

    diff_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    diff_result = await db.execute(diff_query)
    diffs = diff_result.scalars().all()

    # Build response dict from invoice to avoid lazy loading issues
    invoice_dict = {
        "id": invoice.id,
        "file_name": invoice.file_name,
        "file_type": invoice.file_type,
        "status": invoice.status,
        "owner": invoice.owner,
        "approval_instance_id": invoice.approval_instance_id,
        "uploaded_by_id": invoice.uploaded_by_id,
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date,
        "buyer_name": invoice.buyer_name,
        "buyer_tax_id": invoice.buyer_tax_id,
        "seller_name": invoice.seller_name,
        "seller_tax_id": invoice.seller_tax_id,
        "item_name": invoice.item_name,
        "total_with_tax": invoice.total_with_tax,
        "specification": invoice.specification,
        "unit": invoice.unit,
        "quantity": invoice.quantity,
        "unit_price": invoice.unit_price,
        "amount": invoice.amount,
        "tax_rate": invoice.tax_rate,
        "tax_amount": invoice.tax_amount,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
        "ocr_result": ocr,
        "llm_result": llm,
        "parsing_diffs": list(diffs),
    }

    # Fill uploader info
    if invoice.uploaded_by_id:
        u_result = await db.execute(select(UserModel).where(UserModel.id == invoice.uploaded_by_id))
        uploader = u_result.scalar_one_or_none()
        if uploader:
            invoice_dict["uploaded_by_username"] = uploader.username
            invoice_dict["uploaded_by_display_name"] = uploader.display_name

    return InvoiceDetailResponse.model_validate(invoice_dict)


@router.get("/{invoice_id}/file")
async def get_invoice_file(
    invoice_id: int,
    download: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取发票原始文件，download=true 时触发下载，否则内联显示"""
    from fastapi.responses import Response
    from urllib.parse import quote

    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    content_type_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png"
    }

    # URL-encode filename for Content-Disposition header (RFC 5987)
    encoded_filename = quote(invoice.file_name)
    disposition = "attachment" if download else "inline"

    return Response(
        content=invoice.file_data,
        media_type=content_type_map.get(invoice.file_type, "application/octet-stream"),
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}"}
    )


@router.put("/{invoice_id}", response_model=InvoiceResponse)
async def update_invoice(
    invoice_id: int,
    update_data: InvoiceUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新发票信息"""
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    _check_invoice_access(invoice, current_user)
    if invoice.status == InvoiceStatus.CONFIRMED:
        raise HTTPException(status_code=409, detail="发票已确认并锁定，请先取消锁定后再编辑。")

    # Capture old values for audit
    update_dict = update_data.model_dump(exclude_unset=True)
    old_values = {key: getattr(invoice, key) for key in update_dict.keys()}
    # Convert non-serializable types
    for key, value in old_values.items():
        if hasattr(value, 'value'):  # Enum
            old_values[key] = value.value
        elif hasattr(value, 'isoformat'):  # Date/DateTime
            old_values[key] = value.isoformat()
        elif isinstance(value, Decimal):
            old_values[key] = str(value)

    for key, value in update_dict.items():
        setattr(invoice, key, value)

    # Audit log
    client_info = get_client_info(request)
    new_values = update_dict.copy()
    for key, value in new_values.items():
        if hasattr(value, 'value'):  # Enum
            new_values[key] = value.value
        elif hasattr(value, 'isoformat'):  # Date/DateTime
            new_values[key] = value.isoformat()
        elif isinstance(value, Decimal):
            new_values[key] = str(value)

    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="update",
        old_value=old_values,
        new_value=new_values,
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()
    await db.refresh(invoice)

    return InvoiceResponse.model_validate(invoice)


@router.post("/batch-update")
@limiter.limit("30/minute")
async def batch_update_invoices(
    request: Request,
    batch_request: BatchUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """批量更新发票状态/归属人"""
    query = _apply_write_filter(select(Invoice), current_user).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()
    locked_ids = [invoice.id for invoice in invoices if invoice.status == InvoiceStatus.CONFIRMED]
    if locked_ids:
        raise HTTPException(status_code=409, detail="选中的发票包含已确认锁定项，请先取消锁定后再修改。")

    client_info = get_client_info(request)
    updated_count = 0
    for invoice in invoices:
        old_values = {}
        new_values = {}
        if batch_request.status is not None:
            old_values["status"] = invoice.status.value if invoice.status else None
            invoice.status = batch_request.status
            new_values["status"] = batch_request.status.value
        if batch_request.owner is not None:
            old_values["owner"] = invoice.owner
            invoice.owner = batch_request.owner
            new_values["owner"] = batch_request.owner
        updated_count += 1

        # Audit log for each invoice
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="batch_update",
            old_value=old_values,
            new_value=new_values,
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )

    await db.commit()

    return {
        "message": f"成功更新 {updated_count} 张发票",
        "updated_count": updated_count
    }


@router.post("/batch-delete")
@limiter.limit("20/minute")
async def batch_delete_invoices(
    request: Request,
    batch_request: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """批量删除发票及其关联数据"""
    if not batch_request.invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要删除的发票")

    # Query invoices to delete
    query = _apply_write_filter(select(Invoice), current_user).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()

    if not invoices:
        raise HTTPException(status_code=404, detail="未找到要删除的发票")

    # Validate the whole batch before deleting anything, so one protected record
    # cannot leave the request half-applied.
    for invoice in invoices:
        await _reject_evidence_deletion(db, invoice)

    client_info = get_client_info(request)
    deleted_count = 0
    for invoice in invoices:
        # Audit log for each deletion
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="delete",
            old_value={"file_name": invoice.file_name, "invoice_number": invoice.invoice_number},
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )
        await db.delete(invoice)
        deleted_count += 1

    await db.commit()

    return {
        "message": f"成功删除 {deleted_count} 张发票",
        "deleted_count": deleted_count
    }


@router.post("/batch-archive-vouchers")
@limiter.limit("20/minute")
async def batch_archive_vouchers(
    request: Request,
    batch_request: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark reviewed records as non-invoices and retain them in the voucher archive."""
    if not batch_request.invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要归档的单据")

    query = _apply_write_filter(select(Invoice), current_user).where(
        Invoice.id.in_(batch_request.invoice_ids),
        Invoice.document_type == "invoice",
    )
    invoices = (await db.execute(query)).scalars().all()
    if len(invoices) != len(set(batch_request.invoice_ids)):
        raise HTTPException(status_code=404, detail="部分发票不存在或无权操作")
    if any(invoice.status == InvoiceStatus.CONFIRMED for invoice in invoices):
        raise HTTPException(status_code=409, detail="已确认锁定的发票不能归档，请先取消锁定")
    if any(invoice.status != InvoiceStatus.REVIEWING for invoice in invoices):
        raise HTTPException(status_code=400, detail="只有待审核单据可以标记为非发票")

    client_info = get_client_info(request)
    for invoice in invoices:
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="archive_as_voucher",
            old_value={"document_type": invoice.document_type, "status": invoice.status.value},
            new_value={"document_type": "voucher", "status": InvoiceStatus.REVIEWING.value},
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )
        invoice.document_type = "voucher"
        invoice.status = InvoiceStatus.REVIEWING

    await db.commit()
    return {"message": f"已归档 {len(invoices)} 份凭证", "archived_count": len(invoices)}


@router.post("/batch-unlock")
async def batch_unlock_invoices(
    request: Request,
    batch_request: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unlock selected confirmed invoices; leave every other status untouched."""
    invoice_ids = set(batch_request.invoice_ids)
    if not invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要取消锁定的发票")
    result = await db.execute(
        _apply_write_filter(select(Invoice), current_user).where(
            Invoice.id.in_(invoice_ids), Invoice.document_type == "invoice"
        )
    )
    invoices = result.scalars().all()
    if len(invoices) != len(invoice_ids):
        raise HTTPException(status_code=404, detail="部分发票不存在或无权操作")
    client_info = get_client_info(request)
    unlocked = 0
    for invoice in invoices:
        if invoice.status != InvoiceStatus.CONFIRMED:
            continue
        invoice.status = InvoiceStatus.REVIEWING
        await log_audit_no_commit(
            db=db, entity_type="invoice", entity_id=invoice.id, action="unlock",
            old_value={"status": InvoiceStatus.CONFIRMED.value},
            new_value={"status": InvoiceStatus.REVIEWING.value},
            ip_address=client_info.get("ip_address"), user_agent=client_info.get("user_agent"),
        )
        unlocked += 1
    await db.commit()
    return {"message": f"已取消锁定 {unlocked} 张；跳过 {len(invoices) - unlocked} 张未锁定发票",
            "unlocked": unlocked, "skipped": len(invoices) - unlocked}


@router.post("/batch-reprocess")
@limiter.limit("5/minute")
async def batch_reprocess_invoices(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_request: BatchDeleteRequest,  # Reuse for invoice_ids
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """批量重新解析发票（有效结果产生前保留原数据）"""
    import logging
    logger = logging.getLogger(__name__)

    if not batch_request.invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要重新解析的发票")

    # Query invoices to reprocess
    query = _apply_write_filter(select(Invoice), current_user).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()

    if not invoices:
        raise HTTPException(status_code=404, detail="未找到要重新解析的发票")
    if any(invoice.status == InvoiceStatus.CONFIRMED for invoice in invoices):
        raise HTTPException(status_code=409, detail="选中的发票包含已确认锁定项，请先取消锁定后再重新解析。")

    # Keep saved fields and parsing evidence until a valid replacement commits.
    logger.info(f"Scheduling safe reprocess for {len(invoices)} invoices")

    # Schedule background processing
    for invoice in invoices:
        background_tasks.add_task(process_invoice_background, invoice.id, "ocr_and_llm", max_retries=0)

    return {
        "message": f"正在重新解析 {len(invoices)} 张发票；失败时保留原数据",
        "count": len(invoices)
    }


@router.post("/batch-sync")
@limiter.limit("10/minute")
async def batch_sync_invoices_to_dingtalk(
    request: Request,
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    keyword: Optional[str] = Query(None, description="关键词"),
    company_key: Optional[str] = Query(None, description="购买方公司分类"),
    party_type: Optional[str] = Query(None, pattern="^(company|person)$", description="购买方类型"),
    buyer_keyword: Optional[str] = Query(None, description="购买方名称关键词"),
    approval_number: Optional[str] = Query(None, description="审批编号"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue selected or filtered invoices for DingTalk synchronization."""
    query = _apply_write_filter(select(Invoice), current_user)
    query = query.where(Invoice.document_type == "invoice")
    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    if company_key is not None:
        query = query.where(company_key_expression() == company_key)
    buyer_tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    buyer_name = func.nullif(func.trim(Invoice.buyer_name), '')
    if party_type == "company":
        query = query.where(buyer_tax_id.is_not(None))
    elif party_type == "person":
        query = query.where(buyer_tax_id.is_(None), buyer_name.is_not(None))
    if buyer_keyword:
        query = query.where(Invoice.buyer_name.ilike(f"%{buyer_keyword}%"))
    if approval_number:
        query = query.where(Invoice.approval_instance_id == approval_number)
    if keyword:
        from sqlalchemy import or_
        query = query.where(or_(
            Invoice.invoice_number.ilike(f"%{keyword}%"),
            Invoice.buyer_name.ilike(f"%{keyword}%"),
            Invoice.seller_name.ilike(f"%{keyword}%"),
        ))
    start_date_obj = _parse_date_param(start_date, "start_date")
    if start_date_obj:
        query = query.where(Invoice.issue_date >= start_date_obj)
    end_date_obj = _parse_date_param(end_date, "end_date")
    if end_date_obj:
        query = query.where(Invoice.issue_date <= end_date_obj)

    invoices = (await db.execute(query.order_by(Invoice.id))).scalars().all()
    if not invoices:
        raise HTTPException(status_code=404, detail="当前条件下没有可同步的发票")

    from app.services.dingtalk_sync_service import (
        resolve_sync_sequence,
        sync_invoice_to_dingtalk,
    )
    queued = 0
    not_reimbursement = 0
    skipped_voucher = 0
    failed = 0
    for invoice in invoices:
        # 钉钉表只收报销数据：没有审批单号的一律跳过，避免该表出现空归属人/空审批单号的行
        if not (invoice.approval_instance_id or "").strip():
            not_reimbursement += 1
            continue
        sequence = await resolve_sync_sequence(db, invoice)
        # 无票号的凭证：resolve_sync_sequence 返回 None 表示「配套单据/金额为空」，
        # 按规则本就不该进表，不能算入队失败。
        if sequence is None and not (invoice.invoice_number or "").strip():
            skipped_voucher += 1
            continue
        if await sync_invoice_to_dingtalk(invoice, sequence):
            queued += 1
        else:
            failed += 1
    message = f"已提交 {queued} 张发票同步到钉钉"
    if not_reimbursement:
        message += f"；跳过 {not_reimbursement} 张非报销发票（无审批单号）"
    if skipped_voucher:
        message += f"；跳过 {skipped_voucher} 张凭证（配套单据或金额为空）"
    if failed:
        message += f"；{failed} 张入队失败"
    return {"message": message, "count": queued, "skipped_not_reimbursement": not_reimbursement,
            "skipped_voucher": skipped_voucher, "failed": failed}


@router.post("/{invoice_id}/process")
async def process_invoice(
    invoice_id: int,
    processing_mode: str = Query("llm_only", description="处理模式: ocr_only / llm_only / ocr_and_llm"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """处理发票：运行OCR解析"""
    from app.services.invoice_service import process_invoice as do_process

    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    _check_invoice_access(invoice, current_user)
    if invoice.status == InvoiceStatus.CONFIRMED:
        raise HTTPException(status_code=409, detail="发票已确认并锁定，请先取消锁定后再重新解析。")

    success = await do_process(invoice_id, db, processing_mode)

    if success:
        await db.refresh(invoice)
        from app.services.approval_match_service import queue_dingtalk_lookup
        queue_dingtalk_lookup(invoice)
        from app.services.dingtalk_sync_service import (
            resolve_sync_sequence,
            sync_invoice_to_dingtalk,
        )
        await sync_invoice_to_dingtalk(invoice, await resolve_sync_sequence(db, invoice))
        return {"message": "解析成功", "invoice_id": invoice_id}
    else:
        raise HTTPException(status_code=502, detail="解析失败：识别服务未返回有效且完整的结果，请检查模型服务或额度。原有数据已保留。")


@router.delete("/{invoice_id}")
async def delete_invoice(
    invoice_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除发票"""
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    _check_invoice_access(invoice, current_user)
    await _reject_evidence_deletion(db, invoice)

    # Audit log
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="delete",
        old_value={"file_name": invoice.file_name, "invoice_number": invoice.invoice_number},
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.delete(invoice)
    await db.commit()

    return {"message": "删除成功"}


@router.post("/{invoice_id}/diffs/{diff_id}/resolve")
async def resolve_diff(
    invoice_id: int,
    diff_id: int,
    resolve_request: ResolveDiffRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """解决解析差异，选择OCR、LLM或自定义值"""
    from datetime import datetime
    from decimal import Decimal as Dec

    # Verify invoice ownership
    inv_check_result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    inv_for_check = inv_check_result.scalar_one_or_none()
    if not inv_for_check:
        raise HTTPException(status_code=404, detail="发票不存在")
    _check_invoice_access(inv_for_check, current_user)
    if inv_for_check.status == InvoiceStatus.CONFIRMED:
        raise HTTPException(status_code=409, detail="发票已确认并锁定，请先取消锁定后再修改比对结果。")

    # Get the diff
    diff_query = select(ParsingDiff).where(
        ParsingDiff.id == diff_id,
        ParsingDiff.invoice_id == invoice_id
    )
    diff_result = await db.execute(diff_query)
    diff = diff_result.scalar_one_or_none()

    if not diff:
        raise HTTPException(status_code=404, detail="差异记录不存在")

    # Capture old value for audit
    old_diff_value = {
        "field_name": diff.field_name,
        "final_value": diff.final_value,
        "source": diff.source,
        "resolved": diff.resolved,
    }

    # Determine the final value
    if resolve_request.source == 'ocr':
        final_value = diff.ocr_value
    elif resolve_request.source == 'llm':
        final_value = diff.llm_value
    elif resolve_request.source == 'custom':
        final_value = resolve_request.custom_value
    else:
        raise HTTPException(status_code=400, detail="无效的来源类型")

    # Update the diff
    diff.final_value = final_value
    diff.source = resolve_request.source
    diff.resolved = 1

    # Get the invoice and update the corresponding field
    invoice_query = select(Invoice).where(Invoice.id == invoice_id)
    invoice_result = await db.execute(invoice_query)
    invoice = invoice_result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Update the invoice field based on field_name
    field_name = diff.field_name
    if final_value is not None:
        if field_name == 'issue_date':
            try:
                invoice.issue_date = datetime.strptime(final_value, '%Y-%m-%d').date()
            except ValueError:
                pass
        elif field_name in ['total_with_tax', 'amount', 'tax_amount', 'quantity', 'unit_price']:
            try:
                setattr(invoice, field_name, Dec(final_value))
            except (ValueError, TypeError):
                pass
        else:
            setattr(invoice, field_name, final_value)

    # Check if all diffs are resolved
    all_diffs_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    all_diffs_result = await db.execute(all_diffs_query)
    all_diffs = all_diffs_result.scalars().all()

    all_resolved = all(d.resolved == 1 for d in all_diffs)
    if all_resolved:
        invoice.status = InvoiceStatus.CONFIRMED

    # Audit log for diff resolution
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="parsing_diff",
        entity_id=diff_id,
        action="resolve",
        old_value=old_diff_value,
        new_value={
            "field_name": field_name,
            "final_value": final_value,
            "source": resolve_request.source,
            "resolved": 1,
            "invoice_id": invoice_id,
        },
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()

    return {
        "message": "差异已解决",
        "field_name": field_name,
        "final_value": final_value,
        "all_resolved": all_resolved
    }


@router.post("/{invoice_id}/confirm")
async def confirm_invoice(
    invoice_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """确认发票，标记所有差异为已解决。"""
    # Get invoice
    invoice_query = select(Invoice).where(Invoice.id == invoice_id)
    invoice_result = await db.execute(invoice_query)
    invoice = invoice_result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    _check_invoice_access(invoice, current_user)
    if invoice.status == InvoiceStatus.CONFIRMED:
        return {"message": "发票已确认并锁定", "resolved_count": 0}

    critical_fields = [
        "invoice_number",
        "issue_date",
        "total_with_tax",
    ]
    field_labels = {
        "invoice_number": "发票号码",
        "issue_date": "开票日期",
        "total_with_tax": "价税合计",
    }
    missing_fields = [field for field in critical_fields if not getattr(invoice, field)]
    if missing_fields:
        names = "、".join(field_labels.get(f, f) for f in missing_fields)
        raise HTTPException(
            status_code=400,
            detail=f"无法确认：必填字段（{names}）缺失，请补全后再确认。"
        )

    # Check if LLM result exists
    llm_query = select(LlmResult).where(LlmResult.invoice_id == invoice_id)
    llm_result = await db.execute(llm_query)
    llm = llm_result.scalar_one_or_none()
    has_llm = llm is not None

    # Get parsing diffs to verify comparison exists
    diffs_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    diffs_result = await db.execute(diffs_query)
    diffs = diffs_result.scalars().all()

    if has_llm and not diffs:
        raise HTTPException(
            status_code=400,
            detail="无法确认：缺少OCR和LLM的比对结果。请重新解析发票。"
        )

    # Mark all diffs as resolved
    for diff in diffs:
        if diff.resolved == 0:
            # Use existing final_value or ocr_value as default
            if not diff.final_value:
                diff.final_value = diff.ocr_value or diff.llm_value
            diff.resolved = 1

    # Update invoice status
    old_status = invoice.status.value if invoice.status else None
    invoice.status = InvoiceStatus.CONFIRMED

    # Audit log for confirmation
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="confirm",
        old_value={"status": old_status},
        new_value={
            "status": InvoiceStatus.CONFIRMED.value,
            "resolved_diffs": len(diffs),
            "source": "llm" if has_llm else "ocr",
        },
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()

    return {"message": "发票已确认", "resolved_count": len(diffs)}


@router.post("/{invoice_id}/archive-voucher")
async def archive_invoice_as_voucher(
    invoice_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Archive a reviewed record as a non-invoice without deleting its file."""
    invoice = (await db.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")
    _check_invoice_access(invoice, current_user)
    if invoice.document_type == "voucher":
        return {"message": "该单据已归档为凭证"}
    if invoice.status == InvoiceStatus.CONFIRMED:
        raise HTTPException(status_code=409, detail="已确认锁定的发票不能归档，请先取消锁定")
    if invoice.status != InvoiceStatus.REVIEWING:
        raise HTTPException(status_code=400, detail="只有待审核单据可以标记为非发票")

    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice.id,
        action="archive_as_voucher",
        old_value={"document_type": invoice.document_type, "status": invoice.status.value},
        new_value={"document_type": "voucher", "status": InvoiceStatus.REVIEWING.value},
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )
    invoice.document_type = "voucher"
    invoice.status = InvoiceStatus.REVIEWING
    await db.commit()
    return {"message": "已归档到凭证备份"}


@router.post("/{invoice_id}/unlock")
async def unlock_invoice(
    invoice_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel confirmation so the invoice can be edited or reprocessed again."""
    invoice = (await db.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")
    _check_invoice_access(invoice, current_user)
    if invoice.status != InvoiceStatus.CONFIRMED:
        raise HTTPException(status_code=400, detail="该发票当前未锁定")
    invoice.status = InvoiceStatus.REVIEWING
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db, entity_type="invoice", entity_id=invoice_id, action="unlock",
        old_value={"status": InvoiceStatus.CONFIRMED.value},
        new_value={"status": InvoiceStatus.REVIEWING.value},
        ip_address=client_info.get("ip_address"), user_agent=client_info.get("user_agent"),
    )
    await db.commit()
    return {"message": "已取消锁定，可编辑或重新解析"}


@router.get("/export/csv")
@limiter.limit("10/minute")
async def export_invoices_csv(
    request: Request,
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    company_key: Optional[str] = Query(None, description="购买方公司分类"),
    keyword: Optional[str] = Query(None, description="关键词（发票号/购买方/销售方）"),
    party_type: Optional[str] = Query(None, pattern="^(company|person)$", description="购买方类型"),
    buyer_keyword: Optional[str] = Query(None, description="购买方名称关键词"),
    approval_number: Optional[str] = Query(None, description="审批编号"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """导出发票为CSV格式"""
    import csv
    from urllib.parse import quote

    query = select(Invoice)
    query = _apply_user_filter(query, current_user)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    if company_key is not None:
        query = query.where(company_key_expression() == company_key)
    buyer_tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    buyer_name = func.nullif(func.trim(Invoice.buyer_name), '')
    if party_type == "company":
        query = query.where(buyer_tax_id.is_not(None))
    elif party_type == "person":
        query = query.where(buyer_tax_id.is_(None), buyer_name.is_not(None))
    if buyer_keyword:
        query = query.where(Invoice.buyer_name.ilike(f"%{buyer_keyword}%"))
    if approval_number:
        query = query.where(Invoice.approval_instance_id == approval_number)
    if keyword:
        from sqlalchemy import or_
        query = query.where(or_(
            Invoice.invoice_number.ilike(f"%{keyword}%"),
            Invoice.buyer_name.ilike(f"%{keyword}%"),
            Invoice.seller_name.ilike(f"%{keyword}%"),
        ))
    start_date_obj = _parse_date_param(start_date, "start_date")
    if start_date_obj:
        query = query.where(Invoice.issue_date >= start_date_obj)
    end_date_obj = _parse_date_param(end_date, "end_date")
    if end_date_obj:
        query = query.where(Invoice.issue_date <= end_date_obj)

    query = query.order_by(Invoice.created_at.desc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    # Create CSV content
    output = BytesIO()
    # Write BOM for Excel compatibility with Chinese characters
    output.write(b'\xef\xbb\xbf')

    import codecs
    writer = csv.writer(codecs.getwriter('utf-8')(output))

    # Header row
    writer.writerow([
        '发票号码', '开票日期', '购买方名称', '购买方纳税人识别号',
        '销售方名称', '销售方纳税人识别号', '项目名称',
        '金额', '税额', '价税合计', '税率',
        '状态', '归属人', '文件名', '创建时间'
    ])

    # Data rows
    for inv in invoices:
        writer.writerow([
            inv.invoice_number or '',
            str(inv.issue_date) if inv.issue_date else '',
            inv.buyer_name or '',
            inv.buyer_tax_id or '',
            inv.seller_name or '',
            inv.seller_tax_id or '',
            inv.item_name or '',
            str(inv.amount) if inv.amount else '',
            str(inv.tax_amount) if inv.tax_amount else '',
            str(inv.total_with_tax) if inv.total_with_tax else '',
            inv.tax_rate or '',
            inv.status.value if inv.status else '',
            inv.owner or '',
            inv.file_name or '',
            inv.created_at.strftime('%Y-%m-%d %H:%M:%S') if inv.created_at else ''
        ])

    output.seek(0)

    filename = quote('发票导出.csv')
    return StreamingResponse(
        output,
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"
        }
    )


@router.get("/export/excel")
@limiter.limit("10/minute")
async def export_invoices_excel(
    request: Request,
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    company_key: Optional[str] = Query(None, description="购买方公司分类"),
    keyword: Optional[str] = Query(None, description="关键词（发票号/购买方/销售方）"),
    party_type: Optional[str] = Query(None, pattern="^(company|person)$", description="购买方类型"),
    buyer_keyword: Optional[str] = Query(None, description="购买方名称关键词"),
    approval_number: Optional[str] = Query(None, description="审批编号"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """导出发票为Excel格式"""
    from urllib.parse import quote

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, Border, Side
    except ImportError:
        raise HTTPException(status_code=500, detail="Excel导出需要安装openpyxl库")

    query = select(Invoice)
    query = _apply_user_filter(query, current_user)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    if company_key is not None:
        query = query.where(company_key_expression() == company_key)
    buyer_tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    buyer_name = func.nullif(func.trim(Invoice.buyer_name), '')
    if party_type == "company":
        query = query.where(buyer_tax_id.is_not(None))
    elif party_type == "person":
        query = query.where(buyer_tax_id.is_(None), buyer_name.is_not(None))
    if buyer_keyword:
        query = query.where(Invoice.buyer_name.ilike(f"%{buyer_keyword}%"))
    if approval_number:
        query = query.where(Invoice.approval_instance_id == approval_number)
    if keyword:
        from sqlalchemy import or_
        query = query.where(or_(
            Invoice.invoice_number.ilike(f"%{keyword}%"),
            Invoice.buyer_name.ilike(f"%{keyword}%"),
            Invoice.seller_name.ilike(f"%{keyword}%"),
        ))
    start_date_obj = _parse_date_param(start_date, "start_date")
    if start_date_obj:
        query = query.where(Invoice.issue_date >= start_date_obj)
    end_date_obj = _parse_date_param(end_date, "end_date")
    if end_date_obj:
        query = query.where(Invoice.issue_date <= end_date_obj)

    query = query.order_by(Invoice.created_at.desc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "发票列表"

    # Header row
    headers = [
        '发票号码', '开票日期', '购买方名称', '购买方纳税人识别号',
        '销售方名称', '销售方纳税人识别号', '项目名称',
        '金额', '税额', '价税合计', '税率',
        '状态', '归属人', '文件名', '创建时间'
    ]

    header_font = Font(bold=True)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = Alignment(horizontal='center')

    # Data rows
    for row, inv in enumerate(invoices, 2):
        data = [
            inv.invoice_number or '',
            str(inv.issue_date) if inv.issue_date else '',
            inv.buyer_name or '',
            inv.buyer_tax_id or '',
            inv.seller_name or '',
            inv.seller_tax_id or '',
            inv.item_name or '',
            float(inv.amount) if inv.amount else '',
            float(inv.tax_amount) if inv.tax_amount else '',
            float(inv.total_with_tax) if inv.total_with_tax else '',
            inv.tax_rate or '',
            inv.status.value if inv.status else '',
            inv.owner or '',
            inv.file_name or '',
            inv.created_at.strftime('%Y-%m-%d %H:%M:%S') if inv.created_at else ''
        ]

        for col, value in enumerate(data, 1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = thin_border

    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column].width = adjusted_width

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = quote('发票导出.xlsx')
    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"
        }
    )
