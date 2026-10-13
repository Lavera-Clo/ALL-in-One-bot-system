"""
Auto Bot Profile Generator
Reads a tenant's knowledge base and automatically generates a complete bot persona,
system prompt, greeting style, handoff rules, and quick menu.
"""
from dataclasses import dataclass, asdict
from typing import List
import llm_router as llm


@dataclass
class BotProfile:
    business_name: str
    business_type: str  # hotel | restaurant | retail | clinic | salon | generic
    bot_name: str
    bot_role_title: str
    persona_template: str  # professional | casual | friendly | empathetic | energetic
    system_prompt: str
    handoff_rules: str  # one rule per line
    quick_menu: str  # "Title | Description" per line
    greeting_style: str
    currency: str


async def generate_bot_profile(knowledge_base_text: str, business_name_hint: str = "") -> BotProfile:
    """
    Analyzes a business's knowledge base and auto-generates a complete bot profile.
    Uses the LLM router so it works with any configured AI provider.
    """
    system_instruction = """You are an expert AI assistant builder for WhatsApp business bots.
Analyze the provided business knowledge base and generate a complete bot profile.

Return ONLY valid JSON matching this exact schema:
{
  "business_name": "detected or inferred business name",
  "business_type": "one of: hotel | restaurant | retail | clinic | salon | gym | education | finance | generic",
  "bot_name": "A friendly human name for the bot persona (e.g., 'Maya', 'Alex', 'Sara')",
  "bot_role_title": "Role title (e.g., 'Virtual Concierge', 'Customer Care Agent', 'Front Desk Assistant')",
  "persona_template": "one of: professional | casual | friendly | empathetic | energetic",
  "system_prompt": "A comprehensive, tailored system prompt (200-400 words) for this business's WhatsApp bot. Include tone, boundaries, and what the bot should/shouldn't do.",
  "handoff_rules": "One rule per line. When the bot should hand off to a human. E.g.:\\nCustomer asks for discount or price negotiation\\nCustomer has a complaint\\nCustomer wants to speak to a manager",
  "quick_menu": "3-6 quick menu items, one per line as 'Title | Short Description'. E.g.:\\nPricing | View our prices and packages\\nBook Now | Make a reservation\\nContact Us | Speak with our team",
  "greeting_style": "A warm greeting template the bot uses. Include the business name. E.g., 'Welcome to [Business]! I'm [BotName], your virtual assistant. How can I help you today?'",
  "currency": "Detected currency code (e.g., USD, LKR, GBP, EUR)"
}"""

    user_prompt = f"Business name hint: {business_name_hint or 'Unknown'}\n\nKnowledge Base:\n{knowledge_base_text[:15000]}"

    try:
        data, result = await llm.complete_json("judge", [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ], max_tokens=3000, temperature=0.3)
    except llm.LLMUnavailable:
        data = {}

    if not isinstance(data, dict):
        data = {}

    return BotProfile(
        business_name=data.get("business_name", business_name_hint or "Unknown Business"),
        business_type=data.get("business_type", "generic"),
        bot_name=data.get("bot_name", "Assistant"),
        bot_role_title=data.get("bot_role_title", "Virtual Assistant"),
        persona_template=data.get("persona_template", "professional"),
        system_prompt=data.get("system_prompt", "You are a helpful and polite customer service assistant."),
        handoff_rules=data.get("handoff_rules", "Customer asks for discount\nCustomer has a complaint\nCustomer wants to speak to a manager"),
        quick_menu=data.get("quick_menu", "About Us | Learn about our business\nContact | Get in touch with us"),
        greeting_style=data.get("greeting_style", f"Welcome! I'm your virtual assistant. How can I help you today?"),
        currency=data.get("currency", "USD"),
    )
