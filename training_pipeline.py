"""
Multi-model training pipeline: turns one uploaded business document into a verified
knowledge base the WhatsApp bot answers from.

    1. READ      Vision model transcribes each page (scanned pages, tables). Scanned pages
                 get a SECOND independent reader, and the two are compared number by number.
    2. EXTRACT   Several models from different providers list every fact, independently.
    3. MERGE     A judge model combines the drafts, keeping only what the source supports.
    4. VERIFY    Plain code — not AI — checks that every number in the result appears in the
                 source. Unsupported numbers get one AI correction pass; anything still
                 unsupported is deleted. A wrong price is worse than a missing one.
    5. FAQ       Likely customer questions (English / Singlish / Sinhala) with answers,
                 number-checked the same way.
    6. LABEL     Title, trigger keywords and caption for sending the file on WhatsApp.

Nothing here fine-tunes a model. "Training" means building the text the bot reads before
every reply — which is why step 4 matters so much.
"""

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from pypdf import PdfReader, PdfWriter

import llm_router as llm

logger = logging.getLogger("uvicorn.error")

PAGES_PER_READ = 4          # pages sent to the vision model per call (keeps output within limits)
SCANNED_CHARS_PER_PAGE = 80 # below this, a page has no real text layer
CHUNK_CHARS = 9000          # source text per extraction call
MAX_VISION_BYTES = 14 * 1024 * 1024

Progress = Callable[[str, int], Awaitable[None]]


async def _no_progress(stage: str, percent: int) -> None:
    return None


@dataclass
class TrainingResult:
    title: str
    summary: str                 # final verified knowledge base + FAQ, injected into the prompt
    source_text: str             # best transcription of the document
    keywords: List[str]
    caption: str
    extraction_method: str       # 'vision' | 'vision+text' | 'text' | 'pasted'
    report: Dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Numbers: the deterministic safety net
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+(?:\s?[,.]\s?\d+)*")


def _normalise_number(token: str) -> str:
    """'Rs.1,075,000' -> '1075000', '99, 500' -> '99500', '4170.00' -> '4170'."""
    token = re.sub(r"\s", "", token)            # any whitespace, incl. non-breaking
    if token.count(".") > 1:                       # 1.075.000 style thousands
        token = token.replace(".", "")
    token = token.replace(",", "")
    if "." in token:
        whole, _, frac = token.partition(".")
        token = whole if set(frac) <= {"0"} else f"{whole}.{frac}"
    return token.lstrip("0") or "0"


def numbers_in(text: str) -> List[str]:
    """Every number worth checking (2+ digits). Single digits are list markers and counts."""
    found = []
    for raw in _NUMBER_RE.findall(text or ""):
        norm = _normalise_number(raw)
        if len(norm.replace(".", "")) >= 2:
            found.append(norm)
    return found


def unsupported_numbers(text: str, source_numbers: set) -> List[str]:
    return sorted({n for n in numbers_in(text) if n not in source_numbers})


# ---------------------------------------------------------------------------
# 1. READ
# ---------------------------------------------------------------------------

def _split_pdf(pdf_bytes: bytes) -> List[Dict]:
    """Split into small PDFs of PAGES_PER_READ pages, each with its own text-layer text."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    batches = []
    for start in range(0, len(reader.pages), PAGES_PER_READ):
        pages = reader.pages[start:start + PAGES_PER_READ]
        writer = PdfWriter()
        for page in pages:
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        text_layer = "\n".join((p.extract_text() or "") for p in pages)
        batches.append({
            "first_page": start + 1,
            "page_count": len(pages),
            "pdf": buffer.getvalue(),
            "text_layer": text_layer,
            "scanned": len(text_layer.strip()) < SCANNED_CHARS_PER_PAGE * len(pages),
        })
    return batches


def _renumber_pages(text: str, first_page: int) -> str:
    """Vision models number pages from 1 within each batch; shift to real page numbers."""
    return re.sub(r"###\s*Page\s*(\d+)",
                  lambda m: f"### Page {int(m.group(1)) + first_page - 1}", text)


async def _read_batch(batch: Dict, vision_ok: bool) -> Dict:
    """Read one page batch. Returns text plus which models read it and any disagreements."""
    out = {"pages": f"{batch['first_page']}-{batch['first_page'] + batch['page_count'] - 1}",
           "readers": [], "disagreements": [], "method": "text"}

    if not vision_ok:
        out["text"] = batch["text_layer"]
        return out

    try:
        first = await llm.read_pdf(batch["pdf"])
    except llm.LLMUnavailable as exc:
        logger.warning(f"[train] vision read failed for pages {out['pages']}: {exc}")
        out["text"] = batch["text_layer"]
        out["warning"] = "Vision reading failed for these pages; used the PDF's text layer."
        return out

    out["text"] = _renumber_pages(first.text, batch["first_page"])
    out["readers"].append(first.label)
    out["method"] = "vision"

    # Cross-check: scanned pages have no text layer to compare against, so a second,
    # different vision model reads them too. Text pages are checked against the text layer.
    if batch["scanned"]:
        try:
            second = await llm.read_pdf(batch["pdf"], exclude_models={first.label})
            out["readers"].append(second.label)
            a, b = set(numbers_in(first.text)), set(numbers_in(second.text))
            out["disagreements"] = sorted(a ^ b)
        except llm.LLMUnavailable:
            out["single_reader"] = True
    else:
        layer = set(numbers_in(batch["text_layer"]))
        out["disagreements"] = sorted(set(numbers_in(first.text)) - layer)
        out["method"] = "vision+text"
    return out


async def read_document(pdf_bytes: Optional[bytes], plain_text: str, progress: Progress) -> Dict:
    """
    Produce the best possible source text, plus 'witness' text used to verify numbers later.
    """
    if not pdf_bytes:
        return {"source": plain_text, "witness": plain_text, "method": "pasted", "batches": [], "pages": 0}

    batches = _split_pdf(pdf_bytes)
    vision_ok = bool(llm.role_candidates("reader")) and len(pdf_bytes) <= MAX_VISION_BYTES
    await progress(f"Reading {sum(b['page_count'] for b in batches)} page(s)"
                   f"{' with AI vision' if vision_ok else ''}", 8)

    results = []
    for i, batch in enumerate(batches):         # sequential: vision free tiers are tight
        results.append(await _read_batch(batch, vision_ok))
        await progress(f"Read pages {results[-1]['pages']}", 8 + int(22 * (i + 1) / len(batches)))

    source = "\n\n".join(r["text"] for r in results if r.get("text"))
    if plain_text:
        source = f"{source}\n\n### Additional notes\n{plain_text}"
    witness = "\n".join(b["text_layer"] for b in batches) + "\n" + plain_text

    methods = {r["method"] for r in results}
    method = "vision" if methods == {"vision"} else ("text" if methods == {"text"} else "vision+text")
    return {"source": source.strip(), "witness": witness, "method": method,
            "batches": results, "pages": sum(b["page_count"] for b in batches)}


# ---------------------------------------------------------------------------
# 2-3. EXTRACT (ensemble) + MERGE (judge)
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = (
    "You extract facts from business documents for a WhatsApp customer-service assistant. "
    "You never add anything that is not in the text."
)

EXTRACT_RULES = """List EVERY fact a customer could ask about.

Rules:
- One fact per line, starting with "- ". Group lines under short "## " headings.
- Tables: one line per cell, fully spelled out so it reads alone, e.g.
  "- Silver Menu, 100 Pax, Grand Ballroom: Rs.539,500"
  Never write a line that depends on a header the reader can't see.
- Copy every number, price, date, time and phone number EXACTLY as written.
- Include menus and every item in them, what each package includes, extra charges,
  conditions, capacities, times, and contact details.
- Skip decoration (repeated logos, page footers).
- If something is genuinely unclear in the source, copy it as written and add "(unclear in source)".
- Output only the list."""


def _chunks(source: str) -> List[str]:
    """Split on page markers so a chunk never cuts a table in half."""
    parts = re.split(r"(?=###\s*Page\s*\d+)", source)
    chunks, current = [], ""
    for part in parts:
        if len(current) + len(part) > CHUNK_CHARS and current.strip():
            chunks.append(current)
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current)
    return chunks or [source]


async def _extract_and_merge(chunk: str, business: str, doc_title: str, report: Dict) -> str:
    prompt = (f"Business: {business}\nDocument: {doc_title}\n\nSOURCE TEXT:\n<<<\n{chunk}\n>>>\n\n{EXTRACT_RULES}")
    drafts = await llm.ensemble("extract", [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": prompt},
    ], n=3, max_tokens=8000)

    report.setdefault("extractors", [])
    for d in drafts:
        if d.label not in report["extractors"]:
            report["extractors"].append(d.label)

    if not drafts:
        raise llm.LLMUnavailable("No AI model was available to extract facts. Try again in a few minutes.")
    if len(drafts) == 1:
        report.setdefault("warnings", []).append(
            "Only one AI model was available for part of this document, so it was not cross-checked by a second model.")
        return drafts[0].text

    labelled = "\n\n".join(f"DRAFT {chr(65 + i)} (by {d.label}):\n{d.text}" for i, d in enumerate(drafts))
    merge_prompt = f"""{len(drafts)} AI models independently listed the facts in the SOURCE TEXT below.
Merge them into ONE final knowledge base for this document.

SOURCE TEXT:
<<<
{chunk}
>>>

{labelled}

Rules:
1. Include a fact ONLY if the SOURCE TEXT supports it. Drop anything a draft invented.
2. Include EVERY supported fact from ANY draft. Do not drop a fact just because one draft has it.
3. Where drafts disagree, the SOURCE TEXT decides.
4. Copy numbers exactly as the SOURCE TEXT writes them.
5. Tables become fully explicit lines, e.g. "- Gold Menu, 150 Pax, Starlight Ballroom: Rs.753,000".
6. Use these headings where relevant, in this order:
   ## BUSINESS INFO
   ## PACKAGES & PRICES
   ## MENUS & WHAT'S INCLUDED
   ## EXTRA CHARGES
   ## POLICIES & TERMS
   ## CONTACT & LOCATION
   ## OTHER
7. One fact per line starting with "- ". No duplicates. No preamble, no closing remarks."""

    try:
        merged = await llm.complete("judge", [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": merge_prompt},
        ], max_tokens=10000, temperature=0.1, timeout=180)
        report.setdefault("judges", [])
        if merged.label not in report["judges"]:
            report["judges"].append(merged.label)
        return merged.text
    except llm.LLMUnavailable:
        # Better an unmerged union than nothing; verification below still removes bad numbers.
        report.setdefault("warnings", []).append("The merge step was unavailable; drafts were combined without a judge.")
        return max((d.text for d in drafts), key=len)


# ---------------------------------------------------------------------------
# 4. VERIFY
# ---------------------------------------------------------------------------

async def _verify(kb: str, source_numbers: set, source: str, report: Dict) -> str:
    """Every number must exist in the source. One AI fix attempt, then delete what's left."""
    lines = kb.splitlines()
    checked = numbers_in(kb)
    report["numbers_checked"] = len(checked)

    bad_lines = {i: unsupported_numbers(line, source_numbers) for i, line in enumerate(lines)}
    bad_lines = {i: nums for i, nums in bad_lines.items() if nums}
    report["numbers_unsupported_initially"] = sum(len(v) for v in bad_lines.values())

    corrected, removed = [], []
    if bad_lines:
        numbered = "\n".join(f"{i}: {lines[i]}  <-- not in source: {', '.join(nums)}" for i, nums in bad_lines.items())
        try:
            fixes, fixer = await llm.complete_json("judge", [{"role": "user", "content": f"""These knowledge-base lines contain numbers that do NOT appear in the source document.

SOURCE TEXT:
<<<
{source[:24000]}
>>>

LINES TO FIX:
{numbered}

For each line: rewrite it so every number matches the SOURCE TEXT exactly, or return null if
the source does not support the line. Never invent or calculate a number.
Return JSON only: {{"<line number>": "<corrected line>" or null, ...}}"""}], max_tokens=4000, temperature=0)
            report["fixer"] = fixer.label
            for key, value in (fixes or {}).items():
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if index not in bad_lines:
                    continue
                original = lines[index].strip()
                if isinstance(value, str) and value.strip() and not unsupported_numbers(value, source_numbers):
                    corrected.append({"was": original, "now": value.strip()})
                    lines[index] = value
                elif not (isinstance(value, str) and value.strip()):
                    removed.append(original)       # the AI agreed the source doesn't support it
                    lines[index] = ""
        except llm.LLMUnavailable:
            report.setdefault("warnings", []).append("The number-correction step was unavailable.")

    # Final gate: whatever still carries an unsupported number is removed.
    final_lines = []
    for line in lines:
        if line.strip() and unsupported_numbers(line, source_numbers):
            removed.append(line.strip())
            continue
        final_lines.append(line)

    report["lines_corrected"] = corrected[:25]
    report["lines_removed"] = removed[:25]
    report["numbers_verified"] = len(numbers_in("\n".join(final_lines)))
    return "\n".join(final_lines).strip()


# ---------------------------------------------------------------------------
# 5. FAQ
# ---------------------------------------------------------------------------

async def _faq(kb: str, business: str, source_numbers: set, report: Dict) -> str:
    try:
        data, result = await llm.complete_json("judge", [{"role": "user", "content": f"""You write the FAQ for {business}'s WhatsApp assistant, using ONLY this knowledge base:

<<<
{kb[:20000]}
>>>

Write 15-25 questions real customers in Sri Lanka would actually send on WhatsApp, with answers.
- Mix the languages customers really use: English, Singlish (romanized Sinhala, e.g.
  "wedding package eke ganan kiyada?") and Sinhala script. Answer in the SAME language as the question.
- Cover prices, what's included, capacity, extra charges, policies and contact details.
- Answers: 1-3 short sentences, exact prices copied from the knowledge base. Never calculate totals.
- If the knowledge base does not answer something, don't write that question.

Return JSON only: {{"faq": [{{"q": "...", "a": "..."}}, ...]}}"""}], max_tokens=6000, temperature=0.4)
    except llm.LLMUnavailable:
        report.setdefault("warnings", []).append("FAQ generation was unavailable; the bot still answers from the facts above.")
        return ""

    items = data.get("faq", []) if isinstance(data, dict) else data
    kept, dropped = [], 0
    for item in items or []:
        if not isinstance(item, dict) or not item.get("q") or not item.get("a"):
            continue
        if unsupported_numbers(f"{item['q']} {item['a']}", source_numbers):
            dropped += 1
            continue
        kept.append(f"Q: {item['q'].strip()}\nA: {item['a'].strip()}")
    report["faq_writer"] = result.label
    report["faq_count"] = len(kept)
    report["faq_removed_for_bad_numbers"] = dropped
    return "\n\n".join(kept)


# ---------------------------------------------------------------------------
# 6. LABEL
# ---------------------------------------------------------------------------

_GENERIC_KEYWORDS = {"pdf", "document", "file", "send", "list", "details", "detail", "info", "brochure", "please"}


def clean_keywords(raw: List, business_name: str) -> List[str]:
    """
    Keywords decide when the bot SENDS this file, by substring match on the customer's message.
    So: strip the business name out of each phrase (every message to this business may say it,
    which would send the file every time), keep phrases short enough that customers actually
    type them, and remove duplicates.
    """
    name_words = {w for w in re.findall(r"[^\W\d_]{3,}", business_name.lower())}
    seen, keywords = set(), []
    for item in raw or []:
        words = [w for w in str(item).lower().replace("_", " ").split() if w not in name_words]
        phrase = " ".join(words).strip(" -,.")
        if not phrase or phrase in _GENERIC_KEYWORDS or len(words) > 4 or len(phrase) > 40 or phrase in seen:
            continue
        seen.add(phrase)
        keywords.append(phrase)
        if len(keywords) == 20:
            break
    return keywords


# Known-correct Sinhala for words that signal a request for a DOCUMENT (price, list, terms...).
# AI models write Sinhala unreliably — one produced "කැමරාව මිල" ("camera price") as a keyword
# for a wedding price list — so AI-written Sinhala keywords are only trusted if they appear in
# the document or in this glossary. Broad topic words ("wedding", "hall", "food") are left out
# on purpose: as triggers they would send the file on almost every message.
SINHALA_GLOSSARY = {
    "price": ["මිල", "ගණන්", "ගාස්තුව"],
    "cost": ["වියදම", "ගාස්තුව"],
    "rate": ["ගාස්තුව", "ගණන්"],
    "list": ["ලැයිස්තුව"],
    "menu": ["මෙනුව"],
    "package": ["පැකේජ්", "පැකේජය"],
    "registration": ["රෙජිස්ට්‍රේෂන්", "ලියාපදිංචි"],
    "discount": ["වට්ටම්"],
    "terms": ["කොන්දේසි"],
    "policy": ["කොන්දේසි"],
}
_SINHALA_RE = re.compile(r"[඀-෿]")


def vet_ai_keywords(keywords: List[str], source: str) -> List[str]:
    """Drop AI-written Sinhala keywords we can't verify; add glossary Sinhala for common English terms."""
    trusted = {w for words in SINHALA_GLOSSARY.values() for w in words}
    kept = [k for k in keywords if not _SINHALA_RE.search(k) or k in source or k in trusted]
    english = " ".join(k for k in kept if not _SINHALA_RE.search(k))
    for term, sinhala in SINHALA_GLOSSARY.items():
        if re.search(rf"\b{term}", english):
            kept.extend(w for w in sinhala if w not in kept)
    return kept[:24]


def tidy_knowledge_base(kb: str) -> str:
    """Drop headings with nothing under them — they cost tokens in every single reply."""
    out: List[str] = []
    pending_heading: Optional[str] = None
    for line in kb.splitlines():
        if line.strip().startswith("## "):
            pending_heading = line
            continue
        if line.strip():
            if pending_heading is not None:
                if out:
                    out.append("")
                out.append(pending_heading)
                pending_heading = None
            out.append(line)
    return "\n".join(out).strip()


async def _label(source: str, business: str, filename: str, fallback_title: str) -> Dict:
    try:
        data, _ = await llm.complete_json("judge", [{"role": "user", "content": f"""A business called "{business}" uploaded a document named "{filename or 'untitled'}".
Start of its content:

{source[:3000]}

Return JSON only:
{{"title": "short human title, max 6 words",
  "keywords": ["12-20 SHORT phrases (1-3 words) a customer might type on WhatsApp when they want THIS document. Do NOT include the business name. No duplicates. Mix English, Sinhala script and Singlish (romanized Sinhala), e.g. 'price list', 'ganan', 'මිල'"],
  "caption": "one line shown when the file is sent to a customer, max 12 words"}}"""}], max_tokens=1200, temperature=0.2)
    except llm.LLMUnavailable:
        data = {}
    keywords = vet_ai_keywords(clean_keywords(data.get("keywords") or [], business), source)
    return {
        "title": (data.get("title") or fallback_title)[:120],
        "keywords": keywords,
        "caption": (data.get("caption") or f"{business} - {fallback_title}")[:200],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def train_document(*, business_name: str, filename: str, pdf_bytes: Optional[bytes] = None,
                         plain_text: str = "", title_hint: str = "",
                         progress: Progress = _no_progress) -> TrainingResult:
    started = time.time()
    report: Dict = {"warnings": []}
    fallback_title = (title_hint or (filename or "").rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
                      or "Untitled document")[:120]

    # 1. READ
    read = await read_document(pdf_bytes, plain_text, progress)
    source = read["source"]
    if not source.strip():
        raise ValueError("No text could be read from this document, even with AI vision. "
                         "Check the file isn't password-protected or blank.")
    report["reader"] = {
        "method": read["method"],
        "pages": read["pages"],
        "characters": len(source),
        "models": sorted({m for b in read["batches"] for m in b.get("readers", [])}),
    }
    disagreements = sorted({n for b in read["batches"] for n in b.get("disagreements", [])})
    if disagreements:
        report["reader"]["numbers_read_differently"] = disagreements[:30]
        report["warnings"].append(
            "Two readers disagreed on some numbers (listed under 'numbers read differently'). "
            "Open the PDF and check those prices.")
    for b in read["batches"]:
        if b.get("warning"):
            report["warnings"].append(f"Pages {b['pages']}: {b['warning']}")

    source_numbers = set(numbers_in(source)) | set(numbers_in(read["witness"]))

    # 2-3. EXTRACT + MERGE
    chunks = _chunks(source)
    sections = []
    for i, chunk in enumerate(chunks):
        await progress(f"AI models extracting facts (part {i + 1} of {len(chunks)})",
                       32 + int(33 * i / len(chunks)))
        sections.append(await _extract_and_merge(chunk, business_name, fallback_title, report))
    kb = "\n\n".join(sections)

    # 4. VERIFY
    await progress("Checking every price and number against the document", 70)
    kb = tidy_knowledge_base(await _verify(kb, source_numbers, source, report))

    # 5. FAQ
    await progress("Writing customer questions and answers", 82)
    faq = await _faq(kb, business_name, source_numbers, report)

    # 6. LABEL
    await progress("Naming the document and choosing keywords", 94)
    label = await _label(source, business_name, filename, fallback_title)

    summary = kb
    if faq:
        summary += "\n\n## FREQUENTLY ASKED QUESTIONS\n" + faq

    report["duration_seconds"] = round(time.time() - started, 1)
    report["facts"] = sum(1 for line in kb.splitlines() if line.strip().startswith("- "))
    if not report["warnings"]:
        report.pop("warnings")

    return TrainingResult(
        title=label["title"],
        summary=summary,
        source_text=source,
        keywords=label["keywords"],
        caption=label["caption"],
        extraction_method=read["method"],
        report=report,
    )
