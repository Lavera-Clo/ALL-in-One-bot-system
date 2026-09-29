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


def compose_knowledge_base(documents) -> str:
    """
    Build the tenant's full knowledge base by joining every active document's summary.

    The knowledge base is DERIVED from the documents table, never typed over. Add a document
    and it is appended; delete one and it disappears cleanly; nothing else is lost. A document
    that is retraining keeps contributing its previous summary until the new one is ready.
    """
    sections = []
    for doc in documents:
        if not doc.is_active or doc.doc_type == "sendable":
            continue
        if not (doc.summary or "").strip():
            continue
        sections.append(f"===== SOURCE: {doc.title} =====\n{doc.summary.strip()}")

    if not sections:
        return "No business documentation has been uploaded yet."
    return "\n\n".join(sections)
