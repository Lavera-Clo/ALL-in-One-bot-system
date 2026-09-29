"""
Pick only the parts of the knowledge base a customer's message needs.

WHY
---
After multi-model training, a real business's knowledge base is large (Wasala's three PDFs:
32,000 characters, ~9,000 tokens). Sending all of it with every WhatsApp message:
  - exceeds Groq's free-tier per-request limit ("request too large"), so every reply fell
    through to slower providers — measured 5-48 s per reply;
  - costs more tokens on paid tiers, and buries the answer in noise.

HOW
---
The knowledge base is already structured by training_pipeline.py:

    ===== SOURCE: <document title> =====
    ## PACKAGES & PRICES
    - fact
    ## FREQUENTLY ASKED QUESTIONS
    Q: ... (English / Singlish / Sinhala)
    A: ...

It is split into sections (big ones into parts). A small, fast model reads a one-line
catalog of those sections and picks the relevant ones. Because FAQ previews contain the
Sinhala and Singlish phrasings customers really use, this works across languages where plain
keyword matching would not. If the selector is unavailable, keyword overlap is used instead;
if the whole knowledge base is small, it is sent as-is.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import llm_router as llm

logger = logging.getLogger("uvicorn.error")

FULL_KB_UNDER = 9000        # chars: below this, retrieval isn't worth an extra call
CONTEXT_BUDGET = 9000       # chars of knowledge base per reply
PART_CHARS = 2600           # big sections are split into parts about this size
ALWAYS_INCLUDE = ("BUSINESS INFO", "CONTACT & LOCATION")
MAX_PICKS = 6


@dataclass
class Section:
    index: int
    source: str
    heading: str
    text: str
    part: str = ""

    @property
    def label(self) -> str:
        return f"{self.source} › {self.heading}{self.part}"

    def preview(self, limit: int = 170) -> str:
        if "FREQUENTLY ASKED" in self.heading:
            questions = re.findall(r"^Q:\s*(.+)$", self.text, re.M)
            snippet = " | ".join(questions)
        else:
            snippet = " ".join(line.lstrip("-• ").strip() for line in self.text.splitlines() if line.strip())
        return snippet[:limit]

    def render(self) -> str:
        return f"## {self.heading}{self.part}\n{self.text.strip()}"


def _split_long(text: str, heading: str) -> List[str]:
    """Split on blank lines for FAQ (keeps Q and A together), on lines otherwise."""
    units = re.split(r"\n\s*\n", text) if "FREQUENTLY ASKED" in heading else text.splitlines()
    joiner = "\n\n" if "FREQUENTLY ASKED" in heading else "\n"
    parts, current = [], ""
    for unit in units:
        if not unit.strip():
            continue
        if current and len(current) + len(unit) > PART_CHARS:
            parts.append(current)
            current = unit
        else:
            current = f"{current}{joiner}{unit}" if current else unit
    if current:
        parts.append(current)
    return parts


def split_knowledge_base(kb: str) -> List[Section]:
    sections: List[Section] = []
    source, heading = "General", "INFO"
    buffer: List[str] = []

    def flush():
        text = "\n".join(buffer).strip()
        if not text:
            return
        pieces = _split_long(text, heading) if len(text) > PART_CHARS else [text]
        for i, piece in enumerate(pieces):
            part = f" (part {i + 1}/{len(pieces)})" if len(pieces) > 1 else ""
            sections.append(Section(len(sections), source, heading, piece, part))

    for line in (kb or "").splitlines():
        src = re.match(r"^=====\s*SOURCE:\s*(.+?)\s*=====\s*$", line)
        head = re.match(r"^##\s+(.+?)\s*$", line)
        if src:
            flush(); buffer = []
            source, heading = src.group(1), "INFO"
        elif head:
            flush(); buffer = []
            heading = head.group(1).upper()
        else:
            buffer.append(line)
    flush()
    return sections


_WORD_RE = re.compile(r"[^\W_]{3,}|\d+", re.UNICODE)

# Words too common to say anything about which section is relevant (English + Singlish).
_STOPWORDS = {
    "the", "and", "you", "your", "for", "have", "has", "what", "how", "much", "can", "are", "with",
    "this", "that", "does", "any", "there", "please", "want", "need", "give", "tell", "about",
    "from", "will", "our", "was", "were", "which", "when", "where", "who", "get", "got", "also",
    "eke", "eka", "ekak", "ekata", "kiyada", "mata", "oya", "oyata", "puluwanda", "puluwan", "nam", "one",
}


def _query_words(text: str) -> set:
    return {w.lower() for w in _WORD_RE.findall(text or "")} - _STOPWORDS


def _keyword_scores(sections: List[Section], query: str) -> List[Tuple[float, Section]]:
    q = _query_words(query)
    scored = []
    for s in sections:
        words = set(w.lower() for w in _WORD_RE.findall(f"{s.source} {s.heading} {s.text}"))
        overlap = len(q & words)
        if overlap:
            scored.append((overlap / (1 + len(words) ** 0.3), s))
    scored.sort(key=lambda x: -x[0])
    return scored


async def _ai_pick(sections: List[Section], question: str, context: str) -> Optional[List[int]]:
    catalog = "\n".join(f"{s.index}. [{s.label}] {s.preview()}" for s in sections)
    prompt = f"""A customer sent this WhatsApp message to a business (it may be in English, Sinhala or Singlish):

MESSAGE: {question}
{f"EARLIER IN THE CHAT: {context}" if context else ""}

Knowledge-base sections (number. [document › heading] preview):
{catalog}

Pick the sections needed to answer the message accurately and completely.
- If they ask about cost or packages, include the sections containing those prices.
- Include matching FAQ parts when their questions resemble the message.
- A follow-up like "and for 200?" refers to the earlier topic.
- Greetings or small talk with no question: return an empty list.
Return JSON only: {{"sections": [numbers, most relevant first, at most {MAX_PICKS}]}}"""
    try:
        data, result = await llm.complete_json("fast", [{"role": "user", "content": prompt}],
                                               max_tokens=600, temperature=0, timeout=15, patient=False)
    except llm.LLMUnavailable as exc:
        logger.warning(f"[kb] section selector unavailable ({exc}); using keyword match")
        return None
    picks = data.get("sections") if isinstance(data, dict) else data
    valid = {s.index for s in sections}
    out = []
    for p in picks or []:
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if p in valid and p not in out:
            out.append(p)
    return out[:MAX_PICKS]


async def select_context(kb: str, question: str, history: Optional[List[Dict]] = None) -> Tuple[str, Dict]:
    """
    Return (knowledge-base text for this reply, info about how it was chosen).
    Never raises: on any problem it falls back to keyword matching or the full text.
    """
    if not kb or len(kb) <= FULL_KB_UNDER:
        return kb, {"mode": "full", "chars": len(kb or "")}

    sections = split_knowledge_base(kb)
    if not sections:
        return kb, {"mode": "full", "chars": len(kb)}

    recent_user = [m.get("content", "") for m in (history or [])[-6:] if m.get("role") == "user"][-2:]
    context = " / ".join(t[:200] for t in recent_user)

    # Hybrid: the AI picker understands Sinhala/Singlish and intent but only sees a short preview
    # of each section; keyword overlap finds facts buried deep inside a section ("parking").
    # Using both measurably fixed a miss where the picker skipped the parking fact and the reply
    # model then invented a car count.
    keyword_picks = [s.index for _, s in _keyword_scores(sections, f"{question} {context}")]
    picks = await _ai_pick(sections, question, context)
    if picks is None:
        mode, picks = "keywords", keyword_picks[:MAX_PICKS]
    else:
        mode = "ai+keywords"
        picks = picks + [i for i in keyword_picks[:3] if i not in picks]

    chosen = [s for s in sections if s.heading in ALWAYS_INCLUDE]
    chosen += [sections[i] for i in picks if sections[i] not in chosen]

    # Fill the budget in priority order, then print in document order so it reads naturally.
    kept, used = [], 0
    for s in chosen:
        size = len(s.text) + len(s.heading) + 10
        if used + size > CONTEXT_BUDGET and kept:
            continue
        kept.append(s)
        used += size
    kept.sort(key=lambda s: s.index)

    blocks, current_source = [], None
    for s in kept:
        if s.source != current_source:
            blocks.append(f"===== SOURCE: {s.source} =====")
            current_source = s.source
        blocks.append(s.render())
    text = "\n\n".join(blocks)
    return text, {"mode": mode, "chars": len(text), "of": len(kb), "sections": [s.label for s in kept]}
