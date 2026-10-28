"""
What the bot remembers about a conversation, so it can continue it like a person would.

The reply model sees only the last few messages. On their own those lose what a good shop
assistant keeps in their head: who this is, what they are trying to do, what they already
told us, what we promised to check, and whether they wrote a minute ago or last month.

Two parts:

  ConversationContext - built fresh for every incoming message (no AI call): the customer's
      name, new or returning, minutes since their last message and since our last reply, the
      local time, and the stored brief. context_prompt() turns it into the block the reply
      model reads.

  The brief - a short JSON note per conversation (Conversation.memory): goal, stage, details
      the customer gave, questions still open, the sensible next step, their tone. Refreshed
      in the background by the fast model after replies (refresh_brief), so the customer never
      waits for it. It records what was SAID - it is never a source of business facts.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import llm_router

logger = logging.getLogger("uvicorn.error")

SRI_LANKA = timezone(timedelta(hours=5, minutes=30))
RETURNING_AFTER_HOURS = 12          # "welcome back" only after a real gap
REFRESH_EVERY_MESSAGES = 4          # new messages before the brief is rewritten
MIN_MESSAGES_FOR_BRIEF = 4
BRIEF_HISTORY_MESSAGES = 40         # how much of the chat the brief is written from

STAGES = ("greeting", "exploring", "comparing", "ready_to_buy", "booking", "waiting_for_us",
          "after_sale", "complaint", "other")
STAGE_HINTS = {
    "greeting": "they are just saying hello - welcome them and find out what they need",
    "exploring": "they are finding out what is offered - help them narrow it down",
    "comparing": "they are weighing options - help them decide, don't push",
    "ready_to_buy": "they want to go ahead - make the next step easy",
    "booking": "they are arranging a booking or order - collect only what is still missing",
    "waiting_for_us": "they are waiting for something from the business - give an update or say who will follow up",
    "after_sale": "they already bought or booked - look after them",
    "complaint": "something went wrong for them - listen, apologise once, fix or hand over",
}


@dataclass
class ConversationContext:
    tenant_id: int = 0
    phone: str = ""
    customer_name: str = ""
    first_contact: bool = True
    minutes_since_customer: Optional[float] = None   # since their previous message (not this one)
    minutes_since_bot: Optional[float] = None         # since our last message to them
    previous_messages: int = 0
    local_time: str = ""
    brief: Dict = field(default_factory=dict)
    brief_upto_id: Optional[int] = None
    newest_id: Optional[int] = None                    # the message being answered
    left_off: str = ""                                 # the last messages exchanged before this one

    @property
    def returning_after_gap(self) -> bool:
        return (not self.first_contact and self.minutes_since_customer is not None
                and self.minutes_since_customer >= RETURNING_AFTER_HOURS * 60)


# ---------------------------------------------------------------------------
# Building the context (no AI)
# ---------------------------------------------------------------------------

def _aware(when: Optional[datetime]) -> Optional[datetime]:
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


def usable_name(name: Optional[str]) -> str:
    """A first name to address them by, or '' (business names, emoji, numbers don't count)."""
    name = " ".join((name or "").split())
    if not name or len(name) > 40 or not re.search(r"[^\W\d_]", name):
        return ""
    first = re.split(r"[\s(,]", name)[0].strip(".-")
    return first if re.search(r"[^\W\d_]{2,}", first) else ""


def load_brief(raw: Optional[str]) -> Dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def build_context(conversation, history_records, newest_id: Optional[int] = None,
                  now: Optional[datetime] = None) -> ConversationContext:
    """
    `history_records`: this customer's ChatHistory rows BEFORE the message being answered,
    oldest first. `conversation`: their Conversation row (may be None).
    """
    now = now or datetime.now(timezone.utc)
    last_customer = last_bot = None
    for row in history_records:
        when = _aware(row.created_at)
        is_customer = (row.sender or ("customer" if row.role == "user" else "bot")) == "customer"
        if is_customer:
            last_customer = when
        else:
            last_bot = when
    minutes = lambda t: round((now - t).total_seconds() / 60, 1) if t else None  # noqa: E731
    # Where the conversation stood: the last couple of messages, so that a customer who comes back
    # after days gets "did you decide about the gift box?" and not a blank "how can I help?".
    left_off = []
    for row in history_records[-3:]:
        who = "Customer" if (row.sender or ("customer" if row.role == "user" else "bot")) == "customer" else "You"
        text = " ".join((row.content or "").split())
        if text:
            left_off.append(f"{who}: {text[:160]}")
    return ConversationContext(
        left_off="\n".join(left_off),
        tenant_id=getattr(conversation, "tenant_id", 0) or 0,
        phone=getattr(conversation, "customer_phone", "") or "",
        customer_name=usable_name(getattr(conversation, "display_name", "") if conversation else ""),
        first_contact=last_customer is None,
        minutes_since_customer=minutes(last_customer),
        minutes_since_bot=minutes(last_bot),
        previous_messages=len(history_records),
        local_time=now.astimezone(SRI_LANKA).strftime("%A %d %B, %I:%M %p").replace(" 0", " "),
        brief=load_brief(getattr(conversation, "memory", None)),
        brief_upto_id=getattr(conversation, "memory_upto_id", None),
        newest_id=newest_id,
    )


def describe_gap(minutes: Optional[float]) -> str:
    if minutes is None:
        return ""
    if minutes < 2:
        return "a moment ago"
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return f"about {int(hours)} hour{'s' if int(hours) != 1 else ''} ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''} ago"


def context_prompt(ctx: Optional[ConversationContext]) -> str:
    """The block the reply model reads about this customer and where the conversation stands."""
    if ctx is None:
        return ""
    lines = ["--- THIS CUSTOMER AND THIS CONVERSATION ---"]
    if ctx.customer_name:
        lines.append(f"Their name: {ctx.customer_name}. Use it now and then, naturally - not in every message.")
    if ctx.first_contact:
        lines.append("This is their FIRST message to the business. Welcome them; don't say 'welcome back'.")
    elif ctx.returning_after_gap:
        lines.append(f"They are coming back: their previous message was {describe_gap(ctx.minutes_since_customer)}. "
                     "Greet them as a returning customer and pick up where you left off.")
        if ctx.left_off:
            lines.append("How the last conversation ended:\n" + ctx.left_off)
            lines.append("If their message is only a greeting, say hello and refer to that unfinished thing in your own "
                         "words (e.g. ask whether they decided). Never answer a returning customer with a blank "
                         "'anything else?' or 'how can I help?'.")
    else:
        lines.append(f"This is an ongoing conversation (their previous message was "
                     f"{describe_gap(ctx.minutes_since_customer)}). Don't greet them again or start over - "
                     "just continue.")
    if ctx.local_time:
        lines.append(f"Local time now: {ctx.local_time}.")

    b = ctx.brief or {}
    if b.get("summary"):
        lines.append(f"What has happened so far: {b['summary']}")
    if b.get("goal"):
        lines.append(f"What they are trying to do: {b['goal']}")
    stage = b.get("stage")
    if stage in STAGE_HINTS:
        lines.append(f"Where they are: {stage.replace('_', ' ')} - {STAGE_HINTS[stage]}.")
    details = b.get("details") or {}
    if details:
        told = "; ".join(f"{k}: {v}" for k, v in details.items())
        lines.append(f"Details they already gave (NEVER ask for these again - use them): {told}")
    if b.get("open_questions"):
        lines.append("They asked these and haven't had an answer yet - answer or follow up: "
                     + "; ".join(b["open_questions"]))
    if b.get("next_step"):
        lines.append(f"Sensible next step: {b['next_step']}")
    if b.get("tone"):
        lines.append(f"Their mood/tone: {b['tone']}. Match it.")
    lines.append("These notes describe what was SAID. Prices, availability and policies still come only "
                 "from the knowledge base.")
    lines.append("------------------------------------------")
    return "\n".join(lines) + "\n"


# (How to continue a conversation like a person is part of ai_service.REPLY_RULES, rules 11-13.)


# ---------------------------------------------------------------------------
# Refreshing the brief (fast model, in the background)
# ---------------------------------------------------------------------------

BRIEF_PROMPT = (
    "You keep short notes for a business about one WhatsApp conversation with a customer, so the next "
    "reply can continue it naturally.\n"
    "Read the chat and answer with JSON only:\n"
    '{"summary": "2-3 short sentences: what has happened so far", '
    '"goal": "what the customer wants overall, one line", '
    '"stage": "one of: ' + ", ".join(STAGES) + '", '
    '"details": {"short label": "what the customer said", ...}, '
    '"open_questions": ["questions the customer asked that were not answered yet"], '
    '"next_step": "the most helpful next thing for the business to do or ask, one line", '
    '"tone": "the customer\'s mood in 1-3 words"}\n'
    "Rules:\n"
    "- details: only facts the CUSTOMER stated about themselves or their request (dates, number of "
    "people, product they want, budget, location, name). At most 10. Never business prices or offers.\n"
    "- Never invent anything. If something is unknown, leave it out.\n"
    "- Write in English, whatever language the chat is in.\n"
    "- Keep the existing notes' details unless the customer changed them."
)


def should_refresh(ctx: Optional[ConversationContext]) -> bool:
    if ctx is None or not ctx.tenant_id or not ctx.phone:
        return False
    total = ctx.previous_messages + 2          # + this message and our reply
    if total < MIN_MESSAGES_FOR_BRIEF:
        return False
    if not ctx.brief:
        return True
    if ctx.newest_id is None or ctx.brief_upto_id is None:
        return True
    # Message ids grow by one per message saved, across all customers, so this is an
    # over-estimate - it only means we refresh a little early, never late.
    return ctx.newest_id - ctx.brief_upto_id >= REFRESH_EVERY_MESSAGES


def clean_brief(data) -> Dict:
    """Keep only the expected fields, short and the right type."""
    if not isinstance(data, dict):
        return {}
    text = lambda v, n: " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""  # noqa: E731
    out = {}
    for key, limit in (("summary", 400), ("goal", 200), ("next_step", 200), ("tone", 40)):
        v = text(data.get(key, ""), limit)
        if v:
            out[key] = v
    stage = str(data.get("stage", "")).strip().lower().replace(" ", "_")
    if stage in STAGES:
        out["stage"] = stage
    details = data.get("details")
    if isinstance(details, dict):
        d = {}
        for k, v in list(details.items())[:10]:
            k, v = text(k, 40), text(v, 120)
            if k and v:
                d[k] = v
        if d:
            out["details"] = d
    questions = data.get("open_questions")
    if isinstance(questions, list):
        q = [text(x, 160) for x in questions[:4] if text(x, 160)]
        if q:
            out["open_questions"] = q
    return out


async def write_brief(history: List[Dict[str, str]], previous: Dict, business_name: str) -> Dict:
    """The fast model's notes on this chat, or {} if no model is available or it answered badly."""
    if not history or not llm_router.role_candidates("fast"):
        return {}
    transcript = "\n".join(
        f"{'Customer' if m.get('role') == 'user' else business_name}: {(m.get('content') or '')[:500]}"
        for m in history[-BRIEF_HISTORY_MESSAGES:]
    )
    user = (f"Business: {business_name}\n"
            f"Existing notes (update them): {json.dumps(previous, ensure_ascii=False) if previous else 'none'}\n\n"
            f"Chat, oldest first:\n{transcript}")
    try:
        data, result = await llm_router.complete_json(
            "fast", [{"role": "system", "content": BRIEF_PROMPT}, {"role": "user", "content": user}],
            max_tokens=500, temperature=0.1, timeout=30)
    except Exception as exc:  # noqa: BLE001 - a missing brief must never break a reply
        logger.info(f"[memory] brief not refreshed: {exc}")
        return {}
    return clean_brief(data)


async def refresh_brief(tenant_id: int, phone: str, business_name: str) -> Optional[Dict]:
    """Re-read this customer's chat and store new notes on their Conversation. Never raises."""
    from sqlalchemy import select
    from database import AsyncSessionLocal
    from models import ChatHistory, Conversation

    try:
        async with AsyncSessionLocal() as db:
            conv = (await db.execute(select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.customer_phone == phone))).scalar_one_or_none()
            if conv is None:
                return None
            rows = list((await db.execute(
                select(ChatHistory).where(ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone)
                .order_by(ChatHistory.id.desc()).limit(BRIEF_HISTORY_MESSAGES))).scalars().all())
            rows.reverse()
            history = [{"role": r.role, "content": r.content} for r in rows]
            brief = await write_brief(history, load_brief(conv.memory), business_name)
            if not brief:
                return None
            conv.memory = json.dumps(brief, ensure_ascii=False)
            conv.memory_upto_id = rows[-1].id if rows else conv.memory_upto_id
            await db.commit()
            logger.info(f"[memory] tenant {tenant_id}: notes updated for {phone[-4:]} (stage {brief.get('stage', '-')})")
            return brief
    except Exception:  # noqa: BLE001
        logger.exception("[memory] refreshing the conversation notes failed")
        return None


_background: set = set()


REFRESH_DELAY_SECONDS = 5.0     # let the reply reach WhatsApp and the inbox first, so the notes include it


async def _refresh_after_delay(tenant_id: int, phone: str, business_name: str) -> None:
    await asyncio.sleep(REFRESH_DELAY_SECONDS)
    await refresh_brief(tenant_id, phone, business_name)


def refresh_later(ctx: Optional[ConversationContext], business_name: str) -> None:
    """Start refresh_brief in the background when due. The reply is not delayed."""
    if not should_refresh(ctx):
        return
    try:
        task = asyncio.get_running_loop().create_task(_refresh_after_delay(ctx.tenant_id, ctx.phone, business_name))
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)
