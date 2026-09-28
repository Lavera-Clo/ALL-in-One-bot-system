"""
Document library for each tenant — the additive replacement for the old
"upload a PDF and overwrite everything" trainer.

Old behaviour:
    upload menu.pdf   -> knowledge_base = summary(menu.pdf)
    upload policy.pdf -> knowledge_base = summary(policy.pdf)      # menu is gone, silently

New behaviour:
    upload menu.pdf   -> row 1
    upload policy.pdf -> row 2
    knowledge_base    = compose(row 1, row 2)                      # rebuilt from the rows

Because the knowledge base is derived, deleting or deactivating a document rebuilds it
correctly, and the raw text of every upload is kept so it can be re-summarised later.
"""

import logging
import os
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import Tenant, TenantDocument
from training_service import analyze_document, compose_knowledge_base, extract_text_from_pdf

logger = logging.getLogger("uvicorn.error")

DOCS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "tenant_docs")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB — Meta's document limit for WhatsApp is 100 MB


def _safe_filename(filename: str) -> str:
    """Strip any path and unsafe characters so an upload can never escape its tenant folder."""
    base = os.path.basename(filename or "document.pdf")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned[:120] or "document.pdf"


def save_document_file(tenant_id: int, filename: str, content: bytes) -> tuple[str, str]:
    """Write the upload under static/tenant_docs/<tenant_id>/ and return (disk_path, public_url)."""
    tenant_dir = os.path.join(DOCS_ROOT, str(tenant_id))
    os.makedirs(tenant_dir, exist_ok=True)

    safe = _safe_filename(filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe}"
    disk_path = os.path.join(tenant_dir, stored_name)
    with open(disk_path, "wb") as fh:
        fh.write(content)

    public_url = f"{settings.BASE_URL.rstrip('/')}/static/tenant_docs/{tenant_id}/{stored_name}"
    return disk_path, public_url


async def get_tenant_documents(db: AsyncSession, tenant_id: int, active_only: bool = False):
    stmt = select(TenantDocument).where(TenantDocument.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(TenantDocument.is_active == True)  # noqa: E712
    stmt = stmt.order_by(TenantDocument.created_at.asc())
    return list((await db.execute(stmt)).scalars().all())


async def rebuild_knowledge_base(db: AsyncSession, tenant: Tenant) -> str:
    """
    Recompute tenant.knowledge_base from the tenant's active documents.

    Called after every add, edit, toggle and delete — the single place the knowledge base
    is ever written, which is what stops one upload from wiping another.
    """
    documents = await get_tenant_documents(db, tenant.id)
    tenant.knowledge_base = compose_knowledge_base(documents)
    await db.commit()
    await db.refresh(tenant)
    return tenant.knowledge_base


async def add_document(
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
    Add ONE document to this tenant's library and rebuild their knowledge base.

    Accepts either an uploaded file (PDF or UTF-8 text) or pasted text. Raises ValueError
    with a message suitable for showing the client.
    """
    if doc_type not in ("knowledge", "sendable", "both"):
        raise ValueError("doc_type must be 'knowledge', 'sendable' or 'both'.")

    raw_text = ""
    stored_path = None
    file_url = None
    source = "text"

    if content is not None:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

        name = (filename or "").lower()
        if name.endswith(".pdf"):
            raw_text = await extract_text_from_pdf(content)
            source = "pdf"
            if not raw_text.strip():
                raise ValueError(
                    "No text could be read from this PDF. It is probably a scanned image — "
                    "export a text PDF, or paste the details in as text instead."
                )
        else:
            try:
                raw_text = content.decode("utf-8")
                source = "text"
            except UnicodeDecodeError:
                raise ValueError("Uploaded file must be a PDF or a UTF-8 text file.")

        # Only files the bot can actually send get stored and given a public URL.
        if doc_type in ("sendable", "both") and source == "pdf":
            stored_path, file_url = save_document_file(tenant.id, filename or "document.pdf", content)

    if pasted_text and pasted_text.strip():
        raw_text = f"{raw_text}\n{pasted_text.strip()}".strip()

    if not raw_text.strip():
        raise ValueError("No documentation provided.")

    analysis = await analyze_document(raw_text, tenant.business_name, filename or title or "")

    document = TenantDocument(
        tenant_id=tenant.id,
        title=(title or analysis["title"])[:255],
        source=source,
        original_filename=filename,
        stored_path=stored_path,
        file_url=file_url,
        # A pasted note or a plain-text upload has no file to send, so it can only be knowledge.
        doc_type=doc_type if file_url else "knowledge",
        raw_text=raw_text,
        summary=analysis["summary"],
        trigger_keywords=", ".join(analysis["keywords"]),
        caption=analysis["caption"],
        char_count=len(raw_text),
        is_active=True,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    await rebuild_knowledge_base(db, tenant)
    logger.info(f"Tenant {tenant.id}: added document '{document.title}' ({document.char_count} chars).")
    return document


async def update_document(db: AsyncSession, document: TenantDocument, tenant: Tenant, **fields) -> TenantDocument:
    """Edit a document's title, keywords, caption, summary, type or active flag, then rebuild."""
    editable = {"title", "trigger_keywords", "caption", "summary", "doc_type", "is_active"}
    for key, value in fields.items():
        if key in editable and value is not None:
            if key == "doc_type" and value not in ("knowledge", "sendable", "both"):
                raise ValueError("doc_type must be 'knowledge', 'sendable' or 'both'.")
            if key == "doc_type" and value in ("sendable", "both") and not document.file_url:
                raise ValueError("This entry has no file attached, so it can only be 'knowledge'.")
            setattr(document, key, value)

    await db.commit()
    await rebuild_knowledge_base(db, tenant)
    await db.refresh(document)
    return document


async def delete_document(db: AsyncSession, document: TenantDocument, tenant: Tenant) -> None:
    """Remove a document, its file on disk, and its contribution to the knowledge base."""
    path = document.stored_path
    await db.delete(document)
    await db.commit()

    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning(f"Could not delete file {path}: {e}")

    await rebuild_knowledge_base(db, tenant)


async def retrain_document(db: AsyncSession, document: TenantDocument, tenant: Tenant) -> TenantDocument:
    """Re-run the AI summariser over the stored raw text (after a prompt or model change)."""
    if not (document.raw_text or "").strip():
        raise ValueError("This document has no stored text to retrain from.")

    analysis = await analyze_document(document.raw_text, tenant.business_name, document.original_filename or document.title)
    document.summary = analysis["summary"]
    if not (document.trigger_keywords or "").strip():
        document.trigger_keywords = ", ".join(analysis["keywords"])
    await db.commit()
    await rebuild_knowledge_base(db, tenant)
    await db.refresh(document)
    return document
