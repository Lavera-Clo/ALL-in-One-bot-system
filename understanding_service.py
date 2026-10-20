"""
Working out what kind of business a client is, automatically, from their own documents.

    document trained (document_service.run_training)
        -> understand_business(): two independent opinions on the industry
              1. an AI model reads the knowledge base and names the industry, with how sure it is
                 and phrases from the documents as evidence (bot_profile_generator.py)
              2. plain code counts each industry's tell-tale words (industry_brain.detect_industry)
           agreement = high confidence; disagreement = low, and the owner is asked to confirm
        -> fills in profile fields the owner left EMPTY (bot name, job title, location, currency,
           hand-over rules, quick menu, and the placeholder prompt from the "New business" form)
        -> runs the automatic test, so a new business gets a score with no clicks

An industry the OWNER chose is never changed. If the documents point elsewhere, that is shown
as a suggestion. Anything the owner typed is never overwritten.
"""

import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import select

import llm_router as llm
from bot_profile_generator import generate_bot_profile
from database import AsyncSessionLocal
from document_service import get_tenant_documents
from industry_brain import detect_industry, get_pack
from models import Tenant, utc_now
from training_service import EMPTY_KNOWLEDGE_BASE

logger = logging.getLogger("uvicorn.error")

# The system prompt the portal's "New business" form writes; it may be replaced by a real one.
PLACEHOLDER_PROMPT_START = "You are the WhatsApp assistant for "
_RANK = {"low": 0, "medium": 1, "high": 2}


def load(tenant: Tenant) -> Dict:
    try:
        return json.loads(tenant.understanding) if tenant.understanding else {}
    except (TypeError, ValueError):
        return {}


def _save(tenant: Tenant, data: Dict) -> None:
    tenant.understanding = json.dumps(data, ensure_ascii=False)


def combine(ai_industry: str, ai_confidence: str, keyword_industry: str) -> tuple:
    """(industry, confidence) from the two opinions."""
    if ai_industry and keyword_industry:
        if ai_industry == keyword_industry:
            return ai_industry, "high"
        return ai_industry, "low"          # they disagree: go with the reader, ask the owner
    if ai_industry:
        return ai_industry, "medium" if ai_confidence in ("high", "medium") else "low"
    if keyword_industry:
        return keyword_industry, "medium"
    return "generic", "low"


def _blank(value: Optional[str]) -> bool:
    return not (value or "").strip()


async def understand_business(tenant_id: int, run_test: bool = True) -> Optional[Dict]:
    """Background job. Never raises: a failure is recorded on the tenant for the portal to show."""
    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return None
        kb = tenant.knowledge_base or ""
        if not kb.strip() or kb.strip() == EMPTY_KNOWLEDGE_BASE:
            return None

        state = load(tenant)
        state.update({"status": "running", "started_at": utc_now().isoformat()})
        _save(tenant, state)
        await db.commit()

        try:
            profile = await generate_bot_profile(kb, business_name_hint=tenant.business_name)
        except llm.LLMUnavailable as e:
            state.update({"status": "failed", "error": f"No AI provider was available ({e})"[:400]})
            _save(tenant, state)
            await db.commit()
            logger.warning(f"[understand] tenant {tenant_id}: {e}")
            return state

        keyword_industry = detect_industry(kb[:20000], tenant.business_name)
        industry, confidence = combine(profile.ai_industry, profile.industry_confidence, keyword_industry)
        current = tenant.industry or ""
        owner_chose = tenant.industry_source == "owner"

        filled: List[str] = []
        if owner_chose:
            suggestion = industry if industry != current and _RANK[confidence] >= 1 else None
        else:
            suggestion = None
            if industry != current:
                tenant.industry = industry
                tenant.persona_template = None     # the new industry brings its own personality
                filled.append("industry")
            tenant.industry_source = "auto"

        # Only empty fields. "Assistant" is the default name, so it counts as empty.
        pack = get_pack(tenant.industry)
        fills = {
            "bot_name": (tenant.bot_name in (None, "", "Assistant"), profile.bot_name),
            "bot_role_title": (_blank(tenant.bot_role_title), profile.bot_role_title),
            "business_location": (_blank(tenant.business_location), profile.business_location),
            "currency": (_blank(tenant.currency), profile.currency),
            "handoff_rules": (_blank(tenant.handoff_rules), profile.handoff_rules),
            "quick_menu": (_blank(tenant.quick_menu), profile.quick_menu),
            "system_prompt": (_blank(tenant.system_prompt) or tenant.system_prompt.startswith(PLACEHOLDER_PROMPT_START),
                              profile.system_prompt),
        }
        for column, (is_empty, value) in fills.items():
            if is_empty and (value or "").strip() and value != getattr(tenant, column):
                setattr(tenant, column, value.strip())
                filled.append(column)

        state = {
            "status": "done",
            "industry": tenant.industry,
            "industry_label": get_pack(tenant.industry).label,
            "detected": industry,
            "detected_label": get_pack(industry).label,
            "confidence": confidence,
            "evidence": profile.industry_evidence,
            "ai_opinion": profile.ai_industry or None,
            "keyword_opinion": keyword_industry or None,
            "suggestion": suggestion,
            "suggestion_label": get_pack(suggestion).label if suggestion else None,
            "filled": filled,
            "documents_seen": len([d for d in await get_tenant_documents(db, tenant.id, active_only=True)
                                   if d.doc_type != "playbook" and d.status == "ready"]),
            "finished_at": utc_now().isoformat(),
            "role_title_default": pack.role_title,
        }
        _save(tenant, state)
        await db.commit()
        if filled:
            from bot_learning import clear_tenant_cache
            await clear_tenant_cache(db, tenant.id)
        logger.info(f"[understand] tenant {tenant_id}: {industry} ({confidence}); filled {filled or 'nothing'}"
                    + (f"; suggests {suggestion}" if suggestion else ""))

    if run_test:
        await run_test_if_idle(tenant_id)
    return state


async def run_test_if_idle(tenant_id: int) -> None:
    """Start the automatic test, unless one is already running for this business."""
    from self_test_service import run_self_test, start_run
    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return
        try:
            run = await start_run(db, tenant)
        except ValueError:
            return
    await run_self_test(run.id)


async def after_document_trained(tenant_id: int, playbook: bool = False) -> None:
    """
    Called when a document finishes training. A facts document may change what the business
    is, so it is understood again (which then re-tests). A playbook only changes how the bot
    talks, so it is just re-tested.
    """
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            still_training = any(d.status in ("queued", "processing")
                                 for d in await get_tenant_documents(db, tenant_id)) if tenant else False
        if still_training:
            return       # several uploads at once: understand and test once, after the last one
        if playbook:
            await run_test_if_idle(tenant_id)
        else:
            await understand_business(tenant_id)
    except Exception:  # noqa: BLE001 — never let this break training
        logger.exception(f"[understand] follow-up after training failed for tenant {tenant_id}")


async def recover_interrupted() -> int:
    """On startup: an understanding job that was running when the server stopped never finishes."""
    async with AsyncSessionLocal() as db:
        count = 0
        for tenant in (await db.execute(select(Tenant).where(Tenant.understanding.is_not(None)))).scalars():
            state = load(tenant)
            if state.get("status") == "running":
                state.update({"status": "failed", "error": "Interrupted by a server restart - press Re-check."})
                _save(tenant, state)
                count += 1
        await db.commit()
        return count


def understanding_payload(tenant: Tenant) -> Dict:
    state = load(tenant)
    return {
        **state,
        "industry": tenant.industry,
        "industry_label": get_pack(tenant.industry).label if tenant.industry else None,
        "industry_source": tenant.industry_source or "auto",
    }
