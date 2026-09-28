import io
import json
import logging
import re

from pypdf import PdfReader
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger("uvicorn.error")

openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

# A single document is summarised into at most this many tokens. Several documents each get
# their own budget, which is why splitting uploads into rows beats one giant overwritten blob.
SUMMARY_MAX_TOKENS = 1600

# Long PDFs are summarised in chunks so a 60-page menu does not get crushed into one paragraph.
CHUNK_CHARS = 12000


def _using_placeholder_key() -> bool:
    key = (settings.OPENAI_API_KEY or "").strip()
    return not key or "placeholder" in key.lower()


async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extracts text from a PDF file using pypdf."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
        return text.strip()
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {e}")


def _chunk(text: str, size: int = CHUNK_CHARS):
    """Split long text on paragraph boundaries so sentences are not cut in half."""
    paragraphs = text.split("\n")
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) + 1 > size and current:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current.strip():
        chunks.append(current)
    return chunks


async def _summarise_chunk(chunk: str, business_name: str, part: str = "") -> str:
    prompt = f"""
You are an expert AI Bot Trainer for {business_name}. Convert this raw business documentation
into a compressed, structured knowledge base that a WhatsApp chatbot will answer customers from.

Raw Documentation{part}:
{chunk}

Instructions:
1. Extract the core facts (products, prices, policies, operating hours, FAQs). Keep EVERY price,
   number, name and date exactly as written — never round, never rephrase a figure.
2. Group under headings like: BUSINESS INFO / PRODUCTS & PRICING / POLICIES / FAQ.
3. One fact per line. No conversational fluff, no preamble — this text goes straight into a prompt.
4. If something is ambiguous in the source, keep it as written rather than guessing.
"""
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a backend utility that formats raw text into structured AI prompts."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=SUMMARY_MAX_TOKENS,
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


async def generate_bot_knowledge_base(raw_text: str, business_name: str = "this business") -> str:
    """
    Summarise one document into a structured knowledge-base section.

    Long documents are summarised chunk by chunk and joined, so nothing past the first
    few pages is silently dropped the way a single capped call used to drop it.
    """
    if _using_placeholder_key():
        preview = raw_text.strip()[:400]
        return (
            f"(Offline mode — no OpenAI key configured. Raw text stored as-is.)\n\n{preview}"
        )

    chunks = _chunk(raw_text)
    if len(chunks) == 1:
        return await _summarise_chunk(chunks[0], business_name)

    parts = []
    for i, chunk in enumerate(chunks, start=1):
        parts.append(await _summarise_chunk(chunk, business_name, part=f" (part {i} of {len(chunks)})"))
    return "\n\n".join(parts)


def _fallback_keywords(title: str, raw_text: str) -> list:
    """Cheap keyword guess used when the AI is unavailable: title words + frequent nouns."""
    words = re.findall(r"[a-z]{4,}", f"{title} {raw_text[:2000]}".lower())
    stop = {"this", "that", "with", "from", "your", "will", "have", "hall", "page", "shall", "been", "which", "their"}
    seen, keywords = set(), []
    for word in words:
        if word in stop or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= 8:
            break
    return keywords


async def analyze_document(raw_text: str, business_name: str, filename: str = "") -> dict:
    """
    Read one uploaded document and return everything needed to file it:

        {"title", "summary", "keywords": [...], "caption"}

    `keywords` is what makes per-client document sending work: the bot sends THIS file when a
    customer's message contains one of them. They are suggestions — the client can edit them
    in the portal.
    """
    summary = await generate_bot_knowledge_base(raw_text, business_name)
    default_title = (filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip() or "Untitled document")[:120]

    if _using_placeholder_key():
        return {
            "title": default_title,
            "summary": summary,
            "keywords": _fallback_keywords(default_title, raw_text),
            "caption": f"{business_name} - {default_title}",
        }

    meta_prompt = f"""
A business called "{business_name}" uploaded a document named "{filename or 'untitled'}".
Here is the start of its content:

{raw_text[:3000]}

Reply with ONLY a JSON object, no markdown fence:
{{
  "title": "short human title, max 6 words",
  "keywords": ["8-15 lowercase words or short phrases a customer might type on WhatsApp when they want THIS document; include Sinhala and Singlish (romanized Sinhala) variants where natural"],
  "caption": "one line shown when the file is sent to a customer, max 12 words"
}}
"""
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You return strict JSON and nothing else."},
                {"role": "user", "content": meta_prompt},
            ],
            max_tokens=500,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        meta = json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.warning(f"Document metadata generation failed ({e}); falling back to heuristics.")
        meta = {}

    keywords = [str(k).strip().lower() for k in meta.get("keywords", []) if str(k).strip()]
    return {
        "title": (meta.get("title") or default_title)[:120],
        "summary": summary,
        "keywords": keywords or _fallback_keywords(default_title, raw_text),
        "caption": (meta.get("caption") or f"{business_name} - {default_title}")[:200],
    }


def compose_knowledge_base(documents) -> str:
    """
    Build the tenant's full knowledge base by joining every active document's summary.

    This is the heart of the additive-upload fix: the knowledge base is DERIVED from the
    documents table, never typed over. Add a document and it is appended; delete one and it
    disappears cleanly; nothing else the client trained is lost.
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
