"""
Auto Bot Profile Generator
Reads a tenant's knowledge base and automatically generates a complete bot persona,
system prompt, greeting style, handoff rules, and quick menu.
"""
from dataclasses import dataclass, field
from typing import List

import llm_router as llm
from industry_brain import INDUSTRY_CHOICES, detect_industry, get_pack, normalise_industry


@dataclass
class BotProfile:
    business_name: str
    business_type: str  # the industry's display label, e.g. "Clinics, dental & healthcare"
    industry: str  # key of an IndustryPack in industry_brain.py; stored on tenants.industry
    bot_name: str
    bot_role_title: str
    persona_template: str  # the persona that industry speaks with (always a real template)
    system_prompt: str
    handoff_rules: str  # one rule per line
    quick_menu: str  # "Title | Description" per line
    greeting_style: str
    currency: str
    ai_industry: str = ""             # the model's own industry answer ('' if it gave none we recognise)
    industry_confidence: str = "low"  # the model's own view: high | medium | low
    industry_evidence: List[str] = field(default_factory=list)   # phrases from the documents
    business_location: str = ""


def _as_lines(value) -> str:
    """Models sometimes return a JSON list where a newline-separated string was asked for."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


async def generate_bot_profile(knowledge_base_text: str, business_name_hint: str = "") -> BotProfile:
    """
    Analyzes a business's knowledge base and auto-generates a complete bot profile.
    Uses the LLM router so it works with any configured AI provider.

    Raises llm_router.LLMUnavailable when no model produced a usable profile, so the caller
    can leave the tenant untouched instead of saving placeholder text over their settings.
    """
    industry_options = "\n".join(f"  {choice['key']} = {choice['label']}" for choice in INDUSTRY_CHOICES)
    system_instruction = f"""You are an expert AI assistant builder for WhatsApp business bots.
Analyze the provided business knowledge base and generate a complete bot profile.

Return ONLY valid JSON matching this exact schema:
{{
  "business_name": "detected or inferred business name",
  "industry": "exactly one key from the INDUSTRIES list below",
  "industry_confidence": "high | medium | low - how clearly the documents show this industry",
  "industry_evidence": ["3-6 short phrases copied from the documents that show what the business does"],
  "business_location": "town/city and country if the documents state it, otherwise empty",
  "bot_name": "A friendly human name for the bot persona (e.g., 'Maya', 'Alex', 'Sara')",
  "bot_role_title": "Role title (e.g., 'Virtual Concierge', 'Customer Care Agent', 'Front Desk Assistant')",
  "system_prompt": "A comprehensive, tailored system prompt (200-400 words) for this business's WhatsApp bot. Include tone, boundaries, and what the bot should/shouldn't do.",
  "handoff_rules": "One rule per line. When the bot should hand off to a human. E.g.:\\nCustomer asks for discount or price negotiation\\nCustomer has a complaint\\nCustomer wants to speak to a manager",
  "quick_menu": "3-6 quick menu items, one per line as 'Title | Short Description'. E.g.:\\nPricing | View our prices and packages\\nBook Now | Make a reservation\\nContact Us | Speak with our team",
  "greeting_style": "A warm greeting template the bot uses. Include the business name. E.g., 'Welcome to [Business]! I'm [BotName], your virtual assistant. How can I help you today?'",
  "currency": "Detected currency code (e.g., USD, LKR, GBP, EUR)"
}}

INDUSTRIES (key = what it covers):
{industry_options}
Pick the one that best describes what this business sells. Use "generic" only if none fits."""

    user_prompt = f"Business name hint: {business_name_hint or 'Unknown'}\n\nKnowledge Base:\n{knowledge_base_text[:15000]}"

    data, result = await llm.complete_json("judge", [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": user_prompt},
    ], max_tokens=3000, temperature=0.3)

    if not isinstance(data, dict) or not str(data.get("system_prompt") or "").strip():
        raise llm.LLMUnavailable("the model did not return a usable profile")

    # The model's answer is only trusted if it names a real industry; otherwise the business's
    # own words decide, and an unclear case stays generic.
    business_name = str(data.get("business_name") or business_name_hint or "Unknown Business")
    ai_industry = normalise_industry(data.get("industry")) or normalise_industry(data.get("business_type"))
    industry = ai_industry or detect_industry(knowledge_base_text[:15000], business_name) or "generic"
    pack = get_pack(industry)
    confidence = str(data.get("industry_confidence") or "").strip().lower()
    evidence = data.get("industry_evidence") if isinstance(data.get("industry_evidence"), list) else []

    return BotProfile(
        business_name=business_name,
        business_type=pack.label,
        industry=industry,
        bot_name=str(data.get("bot_name") or "Assistant"),
        bot_role_title=str(data.get("bot_role_title") or pack.role_title),
        persona_template=pack.persona,
        system_prompt=str(data["system_prompt"]).strip(),
        handoff_rules=_as_lines(data.get("handoff_rules")) or pack.handoff_text,
        quick_menu=_as_lines(data.get("quick_menu")),
        greeting_style=str(data.get("greeting_style") or "Welcome! I'm your virtual assistant. How can I help you today?"),
        currency=str(data.get("currency") or "").strip(),
        ai_industry=ai_industry,
        industry_confidence=confidence if confidence in ("high", "medium", "low") else "low",
        industry_evidence=[str(e).strip()[:80] for e in evidence if str(e).strip()][:6],
        business_location=str(data.get("business_location") or "").strip()[:255],
    )
