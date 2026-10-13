"""
Document library for each tenant — additive, multi-model training.

    upload menu.pdf   -> row 1 (queued) -> background training -> ready
    upload policy.pdf -> row 2 (queued) -> background training -> ready
    knowledge_base    =  compose(every active, trained row)

The knowledge base is DERIVED from the rows, so adding, editing, switching off or deleting
one document never loses another. Training itself lives in training_pipeline.py; this file
handles files on disk, database rows and the background job around it.

Files:
    uploads/<tenant>/...            private original of every upload (never served; used to retrain)
    static/tenant_docs/<tenant>/... public copy, only for documents the bot may SEND to customers
"""

import io
import json
import logging
import os
import re
import uuid

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import llm_router
from config import settings
from database import AsyncSessionLocal
from models import Tenant, TenantDocument
from training_pipeline import clean_keywords, train_document
from training_service import compose_knowledge_base

logger = logging.getLogger("uvicorn.error")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_ROOT = os.path.join(PROJECT_ROOT, "uploads")
PUBLIC_ROOT = os.path.join(PROJECT_ROOT, "static", "tenant_docs")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
NOTES_MARKER = "### Additional notes\n"
DOC_TYPES = ("knowledge", "sendable", "both")


def _safe_filename(filename: str) -> str:
    """Strip any path and unsafe characters so an upload can never escape its tenant folder."""
    base = os.path.basename(filename or "document.pdf")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned[:120] or "document.pdf"


def _write(root: str, tenant_id: int, filename: str, content: bytes) -> tuple[str, str]:
    folder = os.path.join(root, str(tenant_id))
    os.makedirs(folder, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:8]}_{_safe_filename(filename)}"
    path = os.path.join(folder, stored_name)
    with open(path, "wb") as fh:
        fh.write(content)
    return path, stored_name


def _publish(tenant_id: int, filename: str, content: bytes) -> tuple[str, str]:
    """Public copy the bot can send on WhatsApp. Returns (disk_path, public_url)."""
    path, stored_name = _write(PUBLIC_ROOT, tenant_id, filename, content)
    return path, f"{settings.BASE_URL.rstrip('/')}/static/tenant_docs/{tenant_id}/{stored_name}"


def _remove_if_ours(path: str | None) -> None:
    """Delete a file only if this service created it — never a hand-placed file like static/pdfs/."""
    if not path or not os.path.exists(path):
        return
    real = os.path.realpath(path)
    if real.startswith(os.path.realpath(UPLOAD_ROOT)) or real.startswith(os.path.realpath(PUBLIC_ROOT)):
        try:
            os.remove(real)
        except OSError as e:
            logger.warning(f"Could not delete file {real}: {e}")


def _default_title(filename: str | None) -> str:
    stem = (filename or "").rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return (stem or "Pasted notes")[:120]


def _pdf_text_layer(content: bytes) -> str:
    """Quick text-layer read, used to validate the upload before training starts."""
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as e:  # noqa: BLE001 — pypdf raises many types on corrupt files
        raise ValueError(f"This file could not be opened as a PDF ({e}).")
    if reader.is_encrypted:
        raise ValueError("This PDF is password-protected. Remove the password and upload it again.")
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_tenant_documents(db: AsyncSession, tenant_id: int, active_only: bool = False):
    stmt = select(TenantDocument).where(TenantDocument.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(TenantDocument.is_active == True)  # noqa: E712
    stmt = stmt.order_by(TenantDocument.created_at.asc())
    return list((await db.execute(stmt)).scalars().all())


async def rebuild_knowledge_base(db: AsyncSession, tenant: Tenant) -> str:
    """
    Recompute tenant.knowledge_base from the tenant's documents.

    The single place the knowledge base is ever written, which is what stops one upload
    from wiping another.
    """
    documents = await get_tenant_documents(db, tenant.id)
    previous = tenant.knowledge_base
    tenant.knowledge_base = compose_knowledge_base(documents)
    await db.commit()
    if tenant.knowledge_base != previous:
        # Cached answers were built from the old knowledge (old prices) — forget them.
        from bot_learning import clear_tenant_cache
        await clear_tenant_cache(db, tenant.id)
    await db.refresh(tenant)
    return tenant.knowledge_base


# ---------------------------------------------------------------------------
# Create + train
# ---------------------------------------------------------------------------

async def create_document(
    db: AsyncSession,
    tenant: Tenant,
    *,
    content: bytes | None = None,
    filename: str | None = None,
    pasted_text: str | None = None,
    title: str | None = None,
    doc_type: str = "both",
) -> TenantDocument:
    """
    Validate and store an upload, and queue it for training. Returns immediately —
    call run_training(document.id) (normally as a background task) to train it.

    Raises ValueError with a message suitable for showing the client.
    """
    if doc_type not in DOC_TYPES:
        raise ValueError("doc_type must be 'knowledge', 'sendable' or 'both'.")

    quick_text = ""
    stored_path = public_path = file_url = None
    source = "text"

    if content is not None:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        if (filename or "").lower().endswith(".pdf"):
            source = "pdf"
            quick_text = _pdf_text_layer(content)
            if not quick_text and not llm_router.role_candidates("reader"):
                raise ValueError(
                    "This PDF is a scanned image with no text. Reading it needs an AI vision "
                    "provider — add GEMINI_API_KEY to .env, or paste the details as text.")
            stored_path, _ = _write(UPLOAD_ROOT, tenant.id, filename, content)
            if doc_type in ("sendable", "both"):
                public_path, file_url = _publish(tenant.id, filename, content)
        else:
            try:
                quick_text = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Uploaded file must be a PDF or a UTF-8 text file.")

    notes = (pasted_text or "").strip()
    if notes:
        quick_text = f"{quick_text}\n\n{NOTES_MARKER}{notes}".strip() if source == "pdf" else f"{quick_text}\n{notes}".strip()

    if source != "pdf" and not quick_text.strip():
        raise ValueError("No documentation provided.")

    document = TenantDocument(
        tenant_id=tenant.id,
        title=(title or _default_title(filename))[:255],
        source=source,
        original_filename=filename,
        stored_path=stored_path,
        public_path=public_path,
        file_url=file_url,
        doc_type=doc_type if file_url else "knowledge",   # nothing to send without a file
        raw_text=quick_text,
        summary="",
        trigger_keywords="",
        caption=None,
        char_count=len(quick_text),
        is_active=True,
        status="queued",
        training_stage="Waiting to start",
        training_progress=0,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    logger.info(f"Tenant {tenant.id}: queued '{document.title}' for training.")
    return document


def _split_notes(raw_text: str) -> str:
    return raw_text.split(NOTES_MARKER, 1)[1].strip() if NOTES_MARKER in (raw_text or "") else ""


async def run_training(document_id: int) -> None:
    """
    Background job: run the multi-model pipeline on one document and store the result.

    Uses its own database session (the request that queued it has already finished).
    On failure the document keeps its previous summary, so a failed RETRAIN never makes
    the bot forget something it already knew.
    """
    async with AsyncSessionLocal() as db:
        document = await db.get(TenantDocument, document_id)
        if not document:
            return
        tenant = await db.get(Tenant, document.tenant_id)
        if not tenant:
            return

        async def progress(stage: str, percent: int) -> None:
            document.status = "processing"
            document.training_stage = stage
            document.training_progress = max(0, min(99, percent))
            await db.commit()

        await progress("Starting", 2)

        pdf_bytes = None
        if document.stored_path and document.stored_path.lower().endswith(".pdf") and os.path.exists(document.stored_path):
            with open(document.stored_path, "rb") as fh:
                pdf_bytes = fh.read()
        plain_text = _split_notes(document.raw_text) if pdf_bytes else (document.raw_text or "")

        try:
            result = await train_document(
                business_name=tenant.business_name,
                filename=document.original_filename or document.title,
                pdf_bytes=pdf_bytes,
                plain_text=plain_text,
                title_hint=document.title,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure to the portal
            logger.exception(f"Training failed for document {document_id}")
            document.status = "failed"
            document.training_stage = str(exc)[:500] or exc.__class__.__name__
            document.training_progress = 0
            await db.commit()
            return

        document.summary = result.summary
        document.raw_text = result.source_text
        document.char_count = len(result.source_text)
        document.extraction_method = result.extraction_method
        document.training_report = json.dumps(result.report, ensure_ascii=False)

        # Never overwrite what the owner typed: only fill title/keywords/caption if still default.
        if document.title == _default_title(document.original_filename):
            document.title = result.title[:255]
        if not (document.trigger_keywords or "").strip():
            document.trigger_keywords = ", ".join(result.keywords)
        if not (document.caption or "").strip():
            document.caption = result.caption

        document.status = "ready"
        document.training_stage = "Trained"
        document.training_progress = 100
        await db.commit()
        await rebuild_knowledge_base(db, tenant)
        logger.info(f"Tenant {tenant.id}: trained '{document.title}' in "
                    f"{result.report.get('duration_seconds')}s ({result.report.get('facts')} facts).")


async def add_document(db: AsyncSession, tenant: Tenant, **kwargs) -> TenantDocument:
    """Create AND train in one call (blocks for the whole pipeline). For scripts and the legacy endpoint."""
    document = await create_document(db, tenant, **kwargs)
    await run_training(document.id)
    await db.refresh(document)
    await db.refresh(tenant)
    if document.status == "failed":
        raise ValueError(document.training_stage or "Training failed.")
    return document


async def queue_retrain(db: AsyncSession, document: TenantDocument) -> TenantDocument:
    """Mark a document for retraining; the caller schedules run_training(document.id)."""
    if not document.stored_path and not (document.raw_text or "").strip():
        raise ValueError("This document has no stored file or text to retrain from.")
    document.status = "queued"
    document.training_stage = "Waiting to start"
    document.training_progress = 0
    await db.commit()
    await db.refresh(document)
    return document


async def recover_interrupted_trainings() -> int:
    """On startup: jobs that were running when the server stopped will never finish on their own."""
    async with AsyncSessionLocal() as db:
        stmt = select(TenantDocument).where(TenantDocument.status.in_(("queued", "processing")))
        stuck = list((await db.execute(stmt)).scalars().all())
        for document in stuck:
            document.status = "failed"
            document.training_stage = "Interrupted by a server restart — press Retrain."
            document.training_progress = 0
        await db.commit()
        return len(stuck)


# ---------------------------------------------------------------------------
# Edit / delete
# ---------------------------------------------------------------------------

async def update_document(db: AsyncSession, document: TenantDocument, tenant: Tenant, **fields) -> TenantDocument:
    """Edit title, keywords, caption, summary, type or active flag, then rebuild the knowledge base."""
    editable = {"title", "trigger_keywords", "caption", "summary", "doc_type", "is_active"}
    for key, value in fields.items():
        if key not in editable or value is None:
            continue
        if key == "doc_type":
            if value not in DOC_TYPES:
                raise ValueError("doc_type must be 'knowledge', 'sendable' or 'both'.")
            if value in ("sendable", "both") and not document.file_url:
                has_pdf = document.stored_path and document.stored_path.lower().endswith(".pdf") \
                    and os.path.exists(document.stored_path)
                if not has_pdf:
                    raise ValueError("This entry has no PDF attached, so it can only be 'knowledge'.")
                with open(document.stored_path, "rb") as fh:
                    document.public_path, document.file_url = _publish(
                        tenant.id, document.original_filename or "document.pdf", fh.read())
            elif value == "knowledge" and document.public_path:
                _remove_if_ours(document.public_path)
                document.public_path = None
                document.file_url = None
        if key == "trigger_keywords":
            value = ", ".join(clean_keywords([k for k in value.split(",")], tenant.business_name))
        setattr(document, key, value)

    await db.commit()
    await rebuild_knowledge_base(db, tenant)
    await db.refresh(document)
    return document


async def delete_document(db: AsyncSession, document: TenantDocument, tenant: Tenant) -> None:
    """Remove a document, the files this service created for it, and its part of the knowledge base."""
    paths = [document.stored_path, document.public_path]
    await db.delete(document)
    await db.commit()
    for path in paths:
        _remove_if_ours(path)
    await rebuild_knowledge_base(db, tenant)
