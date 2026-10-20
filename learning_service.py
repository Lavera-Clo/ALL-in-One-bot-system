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

import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        "created_at": gap.created_at.isoformat() if gap.created_at else None,
        "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
        "answered_at": gap.answered_at.isoformat() if gap.answered_at else None,
    }
