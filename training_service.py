"""
Small shared helpers for training.

The training itself — reading PDFs with AI vision, multi-model fact extraction, number
verification, FAQ generation — lives in training_pipeline.py. The background job and file
handling live in document_service.py.
"""

import io

from pypdf import PdfReader


async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extracts a PDF's text layer with pypdf (empty for scanned PDFs)."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
        return text.strip()
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {e}")


OWNER_ANSWERS_TITLE = "Answers from the owner"
EMPTY_KNOWLEDGE_BASE = "No business documentation has been uploaded yet."


def typed_knowledge(knowledge_base: str) -> str:
    """
    The part of a knowledge base someone typed in directly (admin.html, PUT /api/tenants),
    rather than one built from documents. Built ones start with a "===== SOURCE:" banner, so
    typed text is whatever comes before the first banner.
    """
    text = (knowledge_base or "").split("===== SOURCE:", 1)[0].strip()
    return "" if text == EMPTY_KNOWLEDGE_BASE else text


def compose_knowledge_base(documents, owner_answers=(), typed_text: str = "") -> str:
    """
    Build the tenant's full knowledge base by joining every active document's summary.

    The knowledge base is DERIVED from the documents table, never typed over. Add a document
    and it is appended; delete one and it disappears cleanly; nothing else is lost. A document
    that is retraining keeps contributing its previous summary until the new one is ready.

    `owner_answers` are (question, answer) pairs the owner typed for questions the bot could
    not answer (models.KnowledgeGap). They are written as an FAQ section, the format the
    retriever and the reply model already understand.

    `typed_text` is knowledge typed in directly. It is kept ahead of everything else, so
    teaching an answer to a business with no uploaded documents never wipes what it had.
    """
    sections = []
    for doc in documents:
        # A sendable-only file isn't read, and a playbook is how to talk, not facts.
        if not doc.is_active or doc.doc_type in ("sendable", "playbook"):
            continue
        if not (doc.summary or "").strip():
            continue
        sections.append(f"===== SOURCE: {doc.title} =====\n{doc.summary.strip()}")

    pairs = [(q.strip(), a.strip()) for q, a in owner_answers if (q or "").strip() and (a or "").strip()]
    if pairs:
        faq = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)
        sections.append(f"===== SOURCE: {OWNER_ANSWERS_TITLE} =====\n## FREQUENTLY ASKED QUESTIONS\n{faq}")

    if (typed_text or "").strip():
        sections.insert(0, typed_text.strip())
    if not sections:
        return EMPTY_KNOWLEDGE_BASE
    return "\n\n".join(sections)
