"""
Self-learning Q&A cache: remembers good answers so similar questions can be answered instantly.

SAFETY RULES (added after review — a cache that replays old answers can bypass every check
the live reply path does):

1. NUMBERS MUST MATCH. "Silver menu for 150 guests" and "...for 200 guests" are 95% similar as
   text, so pure text similarity would answer the 200-guest question with the 150-guest price.
   A cached answer is only reused when the question contains exactly the same numbers.
2. KNOWLEDGE CHANGES CLEAR THE CACHE. When documents are retrained or the bot profile (phone
   numbers, handoff rules) changes, cached answers may be stale — clear_tenant_cache() is called
   from document_service.rebuild_knowledge_base() and the bot-profile endpoint.
3. NO PERSONAL OR CONTEXT-DEPENDENT ANSWERS. Replies that mention an order number, greet a
   returning customer by history, depend on the time of day, hand over to a manager, or answer
   a short follow-up ("and for 200?") are never cached — replaying them to another customer
   would be wrong or leak someone else's details.
4. ANSWERS WITH MORE COMPLAINTS THAN SUCCESSES ARE NOT REUSED.
"""

import re
from difflib import SequenceMatcher

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import BotQACache, utc_now
from training_pipeline import numbers_in

_ORDER_NUMBER_RE = re.compile(r"\bORD-[A-Z0-9]{4,}\b", re.IGNORECASE)
_PERSONAL_MARKERS = (
    "welcome back", "last time", "kalin apa", "ආපහු සාදරයෙන්",          # returning-customer greetings
    "good morning", "good afternoon", "good evening",                   # time-of-day greetings
    "සුභ උදෑසනක්", "සුභ දහවල්", "සුභ සැන්දෑවක්",
    "wa.me/",                                                            # hand-over to a manager
)
_FOLLOW_UP_STARTS = ("and ", "what about", "how about", "also ", "then ", "ok ", "okay ", "ehenam", "එහෙනම්")


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _normalise(question: str) -> str:
    return re.sub(r"\s+", " ", (question or "").strip().lower())


def same_numbers(a: str, b: str) -> bool:
    """Questions are only interchangeable if they ask about the same quantities."""
    return sorted(numbers_in(a)) == sorted(numbers_in(b))


def is_cacheable(question: str, answer: str) -> bool:
    """False for answers that are personal, time-dependent, hand-overs, or depend on earlier messages."""
    q = _normalise(question)
    a = (answer or "").lower()
    if len(q.split()) < 3:                       # "hi", "yes ok", "price?" — too little to match on safely
        return False
    if q.startswith(_FOLLOW_UP_STARTS):          # meaning depends on the previous message
        return False
    if _ORDER_NUMBER_RE.search(answer or ""):    # someone's order
        return False
    if any(marker in a for marker in _PERSONAL_MARKERS):
        return False
    return True


def _best_match(caches, question: str, threshold: float):
    best, best_score = None, 0.0
    for cache in caches:
        if not same_numbers(question, cache.question):
            continue
        score = similar(question, cache.question)
        if score >= threshold and score > best_score:
            best, best_score = cache, score
    return best


async def get_cached_answer(db: AsyncSession, tenant_id: int, question: str, threshold: float = 0.85) -> str | None:
    if not is_cacheable(question, ""):
        return None
    stmt = select(BotQACache).where(BotQACache.tenant_id == tenant_id)
    caches = (await db.execute(stmt)).scalars().all()

    best_match = _best_match(
        [c for c in caches if c.negative_signals <= c.positive_signals], _normalise(question), threshold)
    if best_match:
        best_match.use_count += 1
        best_match.last_used_at = utc_now()
        await db.commit()
        return best_match.answer
    return None


async def cache_qa_pair(db: AsyncSession, tenant_id: int, question: str, answer: str):
    if not is_cacheable(question, answer):
        return
    question = _normalise(question)
    stmt = select(BotQACache).where(BotQACache.tenant_id == tenant_id, BotQACache.question == question)
    existing = (await db.execute(stmt)).scalars().first()
    if existing:
        existing.answer = answer
        existing.updated_at = utc_now()
    else:
        db.add(BotQACache(
            tenant_id=tenant_id,
            question=question,
            answer=answer,
            positive_signals=0,
            negative_signals=0,
            use_count=0,
        ))
    await db.commit()


async def record_signal(db: AsyncSession, tenant_id: int, question: str, is_positive: bool):
    stmt = select(BotQACache).where(BotQACache.tenant_id == tenant_id)
    caches = (await db.execute(stmt)).scalars().all()
    best_match = _best_match(caches, _normalise(question), 0.8)
    if best_match:
        if is_positive:
            best_match.positive_signals += 1
        else:
            best_match.negative_signals += 1
        await db.commit()


async def clear_tenant_cache(db: AsyncSession, tenant_id: int) -> None:
    """Forget every cached answer for a tenant — call whenever its knowledge or contact details change."""
    await db.execute(delete(BotQACache).where(BotQACache.tenant_id == tenant_id))
    await db.commit()


async def get_learning_stats(db: AsyncSession, tenant_id: int) -> dict:
    stmt = select(BotQACache).where(BotQACache.tenant_id == tenant_id)
    caches = (await db.execute(stmt)).scalars().all()
    return {
        "total_cached_pairs": len(caches),
        "total_cache_hits": sum(c.use_count for c in caches),
        "positive_signals": sum(c.positive_signals for c in caches),
        "negative_signals": sum(c.negative_signals for c in caches),
    }
