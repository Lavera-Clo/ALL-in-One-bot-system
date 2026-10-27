"""
Learning from real chats — with the owner in the loop.

    customer asks something the documents don't cover
        -> the bot says it will check, and flags the reply [NOT_IN_KNOWLEDGE_BASE]
        -> record_gap() files the question (similar questions are counted on one row)
        -> the owner sees "Questions to answer" in the Train Bot portal and types the answer
        -> answer_gap() adds it to the knowledge base, and the next customer gets it

WHY NOT LEARN AUTOMATICALLY
---------------------------
bot_learning.py stores every reply the bot gives, but replaying a reply to another customer
would repeat its mistakes and serve stale stock and prices. Here the bot never learns its own
words: it only learns what the owner wrote, and it learns it as a knowledge-base fact, so the
answer still goes through the language rules, the hand-off rules and the number check.
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import llm_router
from bot_learning import same_numbers, similar
from document_service import rebuild_knowledge_base
from models import KnowledgeGap, Tenant, utc_now

logger = logging.getLogger("uvicorn.error")

SAME_QUESTION = 0.8      # text similarity above which two questions are counted as one
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 2000


def _normalise(text: str) -> str:
    return " ".join((text or "").split())


async def _find_similar(db: AsyncSession, tenant_id: int, question: str) -> Optional[KnowledgeGap]:
    rows = (await db.execute(select(KnowledgeGap).where(KnowledgeGap.tenant_id == tenant_id))).scalars().all()
    best, best_score = None, SAME_QUESTION
    for gap in rows:
        if not same_numbers(question, gap.question):
            continue      # "...for 150 guests" and "...for 200 guests" are different questions
        score = similar(question, gap.question)
        if score >= best_score:
            best, best_score = gap, score
    return best


async def record_gap(db: AsyncSession, tenant_id: int, question: str, bot_reply: str = "",
                     language: str = "", customer_phone: str = "", source: str = "chat") -> Optional[KnowledgeGap]:
    """File a question the bot couldn't answer. Never raises — learning must not block a reply."""
    question = _normalise(question)[:MAX_QUESTION_CHARS]
    if len(question.split()) < 2:        # "hi", "price?" — nothing an owner could answer
        return None
    try:
        gap = await _find_similar(db, tenant_id, question)
        if gap:
            gap.times_asked += 1
            gap.last_customer = customer_phone or gap.last_customer
            if gap.status == "open":
                gap.bot_reply = bot_reply or gap.bot_reply
        else:
            gap = KnowledgeGap(tenant_id=tenant_id, question=question, bot_reply=bot_reply, language=language or None,
                               last_customer=customer_phone or None, source=source)
            db.add(gap)
        await db.commit()
        await db.refresh(gap)
        logger.info(f"[learning] tenant {tenant_id}: gap #{gap.id} ({gap.status}, asked {gap.times_asked}x): {question[:80]!r}")
        return gap
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("[learning] could not record a knowledge gap")
        return None


async def suggest(db: AsyncSession, tenant_id: int, question: str, answer: str, language: str = "",
                  customer_phone: str = "", source: str = "call") -> Optional[KnowledgeGap]:
    """
    A person on the team answered a customer's question (on a call): file it with their answer as a
    suggestion. The owner checks it and saves it - the bot never uses it before that. A question that
    is already answered is left alone.
    """
    question = _normalise(question)[:MAX_QUESTION_CHARS]
    answer = (answer or "").strip()[:MAX_ANSWER_CHARS]
    if len(question.split()) < 2 or not answer:
        return None
    try:
        gap = await _find_similar(db, tenant_id, question)
        if gap is None:
            gap = KnowledgeGap(tenant_id=tenant_id, question=question, language=language or None,
                               last_customer=customer_phone or None, source=source)
            db.add(gap)
        elif gap.status != "open":
            return gap                       # answered (or dismissed) already: the owner decided
        else:
            gap.times_asked += 1
            gap.last_customer = customer_phone or gap.last_customer
        gap.suggested_answer = answer
        await db.commit()
        await db.refresh(gap)
        logger.info(f"[learning] tenant {tenant_id}: suggestion for gap #{gap.id} from a {source}: {question[:80]!r}")
        return gap
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("[learning] could not file a suggestion")
        return None


async def teach_many(db: AsyncSession, tenant: Tenant, pairs: List[Tuple[str, str]], source: str = "faq") -> Dict:
    """
    Many questions and answers at once (an uploaded FAQ sheet): each becomes a taught answer, and the
    knowledge base is rebuilt once at the end. A question already taught gets the new answer.
    """
    added = updated = skipped = 0
    for question, answer in pairs:
        question = _normalise(question)[:MAX_QUESTION_CHARS]
        answer = (answer or "").strip()[:MAX_ANSWER_CHARS]
        if len(question) < 3 or not answer:
            skipped += 1
            continue
        gap = await _find_similar(db, tenant.id, question)
        if gap is None:
            gap = KnowledgeGap(tenant_id=tenant.id, question=question, source=source, times_asked=0)
            db.add(gap)
            added += 1
        else:
            updated += 1
        gap.question = question
        gap.answer = answer
        gap.suggested_answer = None
        gap.status = "answered"
        gap.answered_at = utc_now()
        await db.flush()
    await db.commit()
    if added or updated:
        await rebuild_knowledge_base(db, tenant)
    return {"added": added, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Learning from what the team told customers (chats and calls)
# ---------------------------------------------------------------------------
#
# When a person answers a customer - typed in the inbox or on the phone - general facts they gave
# ("we deliver to Kandy for Rs 450", "open till 9 on Sundays") become SUGGESTED answers on the
# owner's list. They are used once the owner checks and saves them: a person can be wrong, or say
# something meant for that one customer only.

LEARN_PROMPT = (
    "Below is a {kind} between a customer and a staff member of a business. Find questions the customer "
    "asked that the staff member answered with a GENERAL fact about the business that would help answer other "
    "customers too: opening hours, prices, delivery areas and fees, payment methods, policies, what products or "
    "services exist. Leave out anything about this customer only (their order, booking, address, name, phone, "
    "complaint), promises, and anything the staff member was unsure about. Write each question as a customer "
    "would ask it and the answer as a short factual sentence, in English. "
    'JSON only: {{"learned": [{{"question": "...", "answer": "..."}}]}} - an empty list if there is nothing.'
)
PERSONAL = re.compile(r"\+?\d[\d\s-]{7,}\d|@[a-z0-9-]+\.[a-z]|\b(order|booking)\s*(no\.?|number|#)\s*\w+", re.I)
MAX_LEARNED = 5


async def learn_from_conversation(tenant_id: int, turns: List[Dict], customer_phone: str = "", language: str = "",
                                  source: str = "chat-team", kind: str = "WhatsApp chat") -> List[dict]:
    """
    turns: [{"who": "customer" | "team" | "bot", "text": ...}] in order. Returns the suggestions filed.
    Never raises - learning must not break the inbox or a call.
    """
    from database import AsyncSessionLocal
    if not llm_router.role_candidates("fast") or not any(t.get("who") == "team" for t in turns):
        return []
    said = "\n".join(f"{'Customer' if t['who'] == 'customer' else 'Staff'}: {t['text']}" for t in turns if t.get("text"))
    learned: List[dict] = []
    try:
        data, _ = await llm_router.complete_json("fast", [{"role": "system", "content": LEARN_PROMPT.format(kind=kind)},
                                                          {"role": "user", "content": said[-8000:]}],
                                                 max_tokens=500, temperature=0.0, timeout=30)
        items = (data.get("learned") or []) if isinstance(data, dict) else []
        for item in items[:MAX_LEARNED]:
            q, ans = str((item or {}).get("question", "")).strip(), str((item or {}).get("answer", "")).strip()
            if not q or not ans or PERSONAL.search(q + " " + ans):
                continue                                  # about this customer only: not for everyone
            async with AsyncSessionLocal() as db:
                gap = await suggest(db, tenant_id, q, ans, language=language, customer_phone=customer_phone, source=source)
            if gap is not None:
                learned.append({"question": q, "answer": ans, "gap_id": gap.id})
        if learned:
            from realtime import inbox_manager
            try:
                await inbox_manager.broadcast(tenant_id, {"type": "learning.suggested", "count": len(learned)})
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.exception("[learning] could not learn from a conversation")
    return learned


CHAT_LEARN_DELAY = 90        # seconds after a team reply: wait for the rest of the exchange
CONTEXT_BEFORE = 6           # customer messages before a team reply that it may be answering
_pending: set = set()


async def learn_from_chat(tenant_id: int, phone: str) -> List[dict]:
    """The team replied in this chat since it was last looked at: learn from those replies."""
    from database import AsyncSessionLocal
    from models import ChatHistory, Conversation
    async with AsyncSessionLocal() as db:
        conv = (await db.execute(select(Conversation).where(Conversation.tenant_id == tenant_id,
                                                            Conversation.customer_phone == phone))).scalar_one_or_none()
        upto = (conv.learned_upto_id or 0) if conv else 0
        rows = (await db.execute(select(ChatHistory).where(
            ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone)
            .order_by(ChatHistory.id.desc()).limit(80))).scalars().all()
        rows = list(reversed(rows))
        if not rows:
            return []
        newest = rows[-1].id
        if conv is not None:
            conv.learned_upto_id = newest
            await db.commit()
    new_team = [i for i, r in enumerate(rows) if r.id > upto and r.sender == "agent" and r.message_type != "call"]
    if not new_team:
        return []
    start = new_team[0]
    customers = 0
    while start > 0 and customers < CONTEXT_BEFORE:      # the questions the replies may be answering
        start -= 1
        if rows[start].sender == "customer" or rows[start].role == "user":
            customers += 1
    turns = [{"who": "team" if r.sender == "agent" else ("customer" if r.role == "user" else "bot"), "text": r.content}
             for r in rows[start:] if r.content and not r.content.startswith("[")]
    return await learn_from_conversation(tenant_id, turns, customer_phone=phone)


def learn_from_chat_later(tenant_id: int, phone: str, delay: Optional[float] = None) -> None:
    """Called after every team reply; one look per chat, a little later (a reply is often several messages)."""
    key = (tenant_id, phone)
    if key in _pending:
        return
    _pending.add(key)

    async def run():
        try:
            await asyncio.sleep(CHAT_LEARN_DELAY if delay is None else delay)
            await learn_from_chat(tenant_id, phone)
        finally:
            _pending.discard(key)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        _pending.discard(key)


async def learn_from_past_chats(tenant_id: int, days: int = 90, max_chats: int = 150) -> Dict:
    """The one-click scan: every chat where the team replied in the last `days`."""
    from datetime import timedelta
    from database import AsyncSessionLocal
    from models import ChatHistory
    since = utc_now() - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        phones = (await db.execute(select(ChatHistory.customer_phone_number).where(
            ChatHistory.tenant_id == tenant_id, ChatHistory.sender == "agent", ChatHistory.created_at >= since)
            .distinct().limit(max_chats))).scalars().all()
    total = 0
    for phone in phones:
        total += len(await learn_from_chat(tenant_id, phone))
    return {"chats": len(phones), "suggestions": total}


async def list_gaps(db: AsyncSession, tenant_id: int) -> List[KnowledgeGap]:
    stmt = (select(KnowledgeGap).where(KnowledgeGap.tenant_id == tenant_id)
            .order_by(KnowledgeGap.times_asked.desc(), KnowledgeGap.updated_at.desc()))
    return list((await db.execute(stmt)).scalars().all())


async def get_gap(db: AsyncSession, tenant_id: int, gap_id: int) -> Optional[KnowledgeGap]:
    stmt = select(KnowledgeGap).where(KnowledgeGap.id == gap_id, KnowledgeGap.tenant_id == tenant_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def answer_gap(db: AsyncSession, tenant: Tenant, gap: KnowledgeGap, answer: str, question: str = "") -> KnowledgeGap:
    """Save the owner's answer and rebuild the knowledge base, so the very next customer gets it."""
    answer = (answer or "").strip()[:MAX_ANSWER_CHARS]
    if not answer:
        raise ValueError("Type the answer the bot should give.")
    if (question or "").strip():
        gap.question = _normalise(question)[:MAX_QUESTION_CHARS]   # the owner may tidy the wording
    gap.answer = answer
    gap.status = "answered"
    gap.answered_at = utc_now()
    await db.commit()
    await rebuild_knowledge_base(db, tenant)
    await db.refresh(gap)
    return gap


async def teach(db: AsyncSession, tenant: Tenant, question: str, answer: str, source: str = "owner") -> KnowledgeGap:
    """Teach an answer directly (from a failed self-test, or the owner thinking ahead)."""
    question = _normalise(question)[:MAX_QUESTION_CHARS]
    if not question:
        raise ValueError("Type the question.")
    gap = await _find_similar(db, tenant.id, question)
    if not gap:
        gap = KnowledgeGap(tenant_id=tenant.id, question=question, source=source, times_asked=0)
        db.add(gap)
        await db.flush()
    return await answer_gap(db, tenant, gap, answer, question)


async def dismiss_gap(db: AsyncSession, tenant: Tenant, gap: KnowledgeGap) -> None:
    """Ignore a question, or take back an answer. Either way it leaves the knowledge base."""
    was_answered = gap.status == "answered"
    gap.status = "dismissed"
    await db.commit()
    if was_answered:
        await rebuild_knowledge_base(db, tenant)


async def reopen_gap(db: AsyncSession, gap: KnowledgeGap) -> None:
    gap.status = "open"
    await db.commit()


def gap_payload(gap: KnowledgeGap) -> dict:
    return {
        "id": gap.id,
        "question": gap.question,
        "bot_reply": gap.bot_reply,
        "language": gap.language,
        "times_asked": gap.times_asked,
        "source": gap.source,
        "status": gap.status,
        "answer": gap.answer,
        "suggested_answer": gap.suggested_answer,
        "created_at": gap.created_at.isoformat() if gap.created_at else None,
        "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
        "answered_at": gap.answered_at.isoformat() if gap.answered_at else None,
    }
