"""
Training on a conversation playbook: HOW the owner wants chats to go, not WHAT is true.

A playbook is a document of goals ("always find out the guest count and date"), sales and
objection-handling advice, and example conversations. It is trained separately from facts
(training_pipeline.py) because its numbers are not facts. Measured on a real playbook: an
example reply said a package "starts around Rs. 885,000" while the business's price list says
Rs. 783,000. Learned as a fact, that would have been quoted to customers.

So:
    1. READ      Same reader as fact documents (handles scanned PDFs).
    2. EXTRACT   One model turns it into goals, style, sales approach, hand-over rules,
                 objections and verbatim example exchanges.
    3. BLANK     Plain code replaces every price/amount in the bot's side with "[amount]".
                 The bot learns the shape of a good answer, and must take the figure from the
                 knowledge base.
    4. COMPARE   Amounts the playbook mentions that the knowledge base doesn't have are listed
                 in the training report, so the owner can see where the two disagree.

The result is injected into the system prompt as the business's own playbook
(ai_service.generate_bot_reply), and its example customer lines become automatic test
questions (self_test_service.py).
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import llm_router as llm
from training_pipeline import Progress, _no_progress, numbers_in, read_document

logger = logging.getLogger("uvicorn.error")

MAX_PLAYBOOK_CHARS = 6000     # what goes into every reply's prompt
MAX_EXAMPLES = 8

# A speaker label at the start of a line: "CUSTOMER:", "[BOT]:", "Guest -", "Bot (AI Response):"
_SPEAKER_RE = re.compile(
    r"^\s*\[?\s*(customer|client|guest|user|caller|patient|buyer|bot|agent|assistant|staff|ai|reply|us|we)\s*\]?"
    r"\s*(?:\([^)\n]{0,30}\))?\s*[:\-–]",
    re.IGNORECASE | re.MULTILINE)

# Links and phone numbers are contact details, not prices: "wa.me/94779998877", "+94 3422 75000"
# (even when the PDF breaks it across a line). They are protected from blanking and comparing.
_CONTACT_RE = re.compile(r"https?://\S+|wa\.me/\S+|\+?\d(?:[ \t\n\-]?\d){8,}")
_CUSTOMER_SIDE = {"customer", "client", "guest", "user", "caller", "patient", "buyer"}

# An amount: a currency mark beside a number, a thousands-separated number, or 4+ digits.
# Counts like "150 guests" are kept: they are part of the example's situation, not a quote.
# Years (1900-2099) are kept too: "our 2026 brochure" is not a price.
_AMOUNT_RE = re.compile(
    r"(?:rs\.?|lkr|usd|us\$|\$|€|£)\s?\d[\d,]*(?:\.\d+)?(?:\s?/[=-])?"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s?/[=-])?"
    r"|\b(?!(?:19|20)\d\d\b)\d{4,}(?:\.\d+)?\b",
    re.IGNORECASE)

# Internal reply tags copied into a playbook ("[SEND_DOC: ...]") must not be learned as style.
_TAG_RE = re.compile(r"\[\s*(?:SEND_DOC|HUMAN_HANDOFF_REQUESTED|NOT_IN_KNOWLEDGE_BASE)[^\]]*\]", re.IGNORECASE)


def looks_like_playbook(text: str) -> bool:
    """True for a document of example chats: several speaker-labelled lines from both sides."""
    speakers = [m.group(1).lower() for m in _SPEAKER_RE.finditer(text or "")]
    customer = sum(1 for s in speakers if s in _CUSTOMER_SIDE)
    return customer >= 2 and len(speakers) - customer >= 2 and len(speakers) >= 4


def blank_amounts(text: str) -> tuple:
    """('... starts at [amount] ...', number of amounts blanked). Links and phone numbers are kept."""
    kept: List[str] = []

    def protect(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    guarded = _CONTACT_RE.sub(protect, text or "")
    count = len(_AMOUNT_RE.findall(guarded))
    blanked = _AMOUNT_RE.sub("[amount]", guarded)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], blanked), count


@dataclass
class PlaybookResult:
    title: str
    summary: str               # the playbook text injected into the prompt
    source_text: str
    extraction_method: str
    report: Dict = field(default_factory=dict)


EXTRACT_PROMPT = """You are preparing a WhatsApp customer-service bot for "{business}". Below is the owner's
CONVERSATION PLAYBOOK: how they want the bot to talk to customers. Turn it into JSON:

{{"title": "short title, max 6 words",
  "goals": ["what the bot should find out or achieve in a conversation, e.g. 'Find out the guest count'"],
  "style": ["how the bot should sound and behave"],
  "sales": ["how to recommend, follow up and move towards a booking or purchase"],
  "handoff": ["situations the owner wants passed to a person"],
  "objections": [{{"when": "what the customer says or worries about", "do": "how to respond"}}],
  "examples": [{{"customer": "a customer message copied from the playbook", "bot": "the bot reply copied from the playbook"}}]}}

Rules:
- Use only what the playbook says. Keep each item short. Leave a list empty if the playbook says nothing.
- "examples": up to {max_examples} exchanges, copied as written (you may shorten long replies), the most
  useful and varied first. Keep each customer line and its reply together.
- Do not invent prices, products or policies.

PLAYBOOK:
{text}"""


def _lines(items) -> List[str]:
    out = []
    for item in items or []:
        text = str(item or "").strip().lstrip("-• ").strip()
        if text and text not in out:
            out.append(text[:300])
    return out


def render_playbook(data: Dict) -> tuple:
    """(playbook text, report counts). Every amount on the bot's side is blanked."""
    blanked = 0

    def clean(text):
        nonlocal blanked
        out, n = blank_amounts(_TAG_RE.sub("", text))
        blanked += n
        return re.sub(r"[ \t]{2,}", " ", out).strip()

    parts = []
    for heading, key in (("CONVERSATION GOALS", "goals"), ("STYLE", "style"),
                         ("SALES APPROACH", "sales"), ("WHEN TO HAND OVER", "handoff")):
        items = [clean(x) for x in _lines(data.get(key))]
        if items:
            parts.append(f"## {heading}\n" + "\n".join(f"- {x}" for x in items))

    objections = []
    for o in data.get("objections") or []:
        if isinstance(o, dict) and str(o.get("when") or "").strip() and str(o.get("do") or "").strip():
            objections.append(f"- When {str(o['when']).strip()[:200]}: {clean(str(o['do']).strip()[:300])}")
    if objections:
        parts.append("## OBJECTIONS\n" + "\n".join(objections))

    examples = []
    for e in data.get("examples") or []:
        if isinstance(e, dict) and str(e.get("customer") or "").strip() and str(e.get("bot") or "").strip():
            # Both sides: a customer line quoting a wrong price ("Rs. 885,000 is above our budget")
            # would otherwise put that price in front of the model on every reply.
            examples.append(f"CUSTOMER: {clean(str(e['customer']).strip()[:400])}\nBOT: {clean(str(e['bot']).strip()[:700])}")
        if len(examples) == MAX_EXAMPLES:
            break
    if examples:
        parts.append("## EXAMPLE CONVERSATIONS\n" + "\n\n".join(examples))

    # Keep within the prompt budget: drop examples from the end first.
    text = "\n\n".join(parts)
    while len(text) > MAX_PLAYBOOK_CHARS and examples:
        examples.pop()
        parts[-1] = "## EXAMPLE CONVERSATIONS\n" + "\n\n".join(examples) if examples else ""
        text = "\n\n".join(p for p in parts if p)
    counts = {
        "goals": len(_lines(data.get("goals"))),
        "rules": sum(len(_lines(data.get(k))) for k in ("style", "sales", "handoff")),
        "objections": len(objections),
        "examples": len(examples),
        "amounts_hidden": blanked,
    }
    return text[:MAX_PLAYBOOK_CHARS], counts


def amount_conflicts(playbook_source: str, knowledge_base: str) -> List[str]:
    """Amounts written in the playbook that the knowledge base doesn't contain."""
    kb_numbers = set(numbers_in(knowledge_base or ""))
    missing = []
    for raw in _AMOUNT_RE.findall(_CONTACT_RE.sub(" ", playbook_source or "")):
        values = numbers_in(raw)
        if values and values[0] not in kb_numbers and raw.strip() not in missing:
            missing.append(raw.strip())
    return missing[:12]


def example_exchanges(playbook_text: str) -> List[Dict]:
    """The CUSTOMER/BOT pairs in a trained playbook, for the automatic test."""
    pairs = []
    for block in re.split(r"\n\s*\n", playbook_text or ""):
        # search, not match: the first example sits right under the "## EXAMPLE CONVERSATIONS" heading
        m = re.search(r"^CUSTOMER:\s*(.+?)\nBOT:\s*(.+)", block, re.DOTALL | re.MULTILINE)
        if m:
            pairs.append({"customer": m.group(1).strip(), "bot": m.group(2).strip()})
    return pairs


async def train_playbook(*, business_name: str, filename: str, pdf_bytes: Optional[bytes] = None,
                         plain_text: str = "", title_hint: str = "", knowledge_base: str = "",
                         progress: Progress = _no_progress) -> PlaybookResult:
    started = time.time()
    read = await read_document(pdf_bytes, plain_text, progress)
    source = read["source"]
    if not source.strip():
        raise ValueError("No text could be read from this playbook.")

    await progress("AI is learning how you want conversations to go", 55)
    data, result = await llm.complete_json("judge", [{"role": "user", "content": EXTRACT_PROMPT.format(
        business=business_name, max_examples=MAX_EXAMPLES, text=source[:24000])}], max_tokens=4000, temperature=0.1)
    if not isinstance(data, dict):
        raise ValueError("The AI could not read this playbook. Try again, or paste it as text.")

    await progress("Hiding example prices and comparing with your price documents", 85)
    summary, counts = render_playbook(data)
    if not summary.strip():
        raise ValueError("No conversation goals, rules or example chats were found in this document. "
                         "If it is a price list or menu, upload it as business information instead.")

    report: Dict = {"kind": "playbook", **counts, "model": result.label, "reader": {"method": read["method"]},
                    "warnings": []}
    conflicts = amount_conflicts(source, knowledge_base)
    if conflicts:
        report["amounts_not_in_knowledge_base"] = conflicts
        report["warnings"].append(
            "Your playbook mentions amounts that are not in your price documents. The bot never quotes "
            "playbook prices - only the price documents - but check which is right.")
    if counts["amounts_hidden"]:
        report["warnings"].append(
            f"{counts['amounts_hidden']} price(s) in the example replies were hidden, so the bot copies the style, "
            "not the numbers.")
    if not report["warnings"]:
        report.pop("warnings")
    report["duration_seconds"] = round(time.time() - started, 1)

    title = str(data.get("title") or title_hint or "Conversation playbook").strip()[:120]
    return PlaybookResult(title=title, summary=summary, source_text=source,
                          extraction_method=read["method"], report=report)
