"""
Checks for the industry layer of the brain (industry_brain.py) and everything wired to it:
the tenants.industry column and its one-time backfill, per-industry prompts and buttons,
the offline fallback, the bot-profile API and the WhatsApp webhook.

Runs offline against a throwaway database - no AI provider is called, nothing is sent to
WhatsApp, and whatsapp_bot.db is never opened.

    python test_industry_brain.py
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

# Must be set before any project module is imported: config.py reads it at import time.
_TMP = tempfile.TemporaryDirectory()
_DB_PATH = os.path.join(_TMP.name, "industry_test.db")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + _DB_PATH.replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import bot_profile_generator  # noqa: E402
import industry_brain as brain  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, Tenant  # noqa: E402
from prompts.persona_system_prompt import AVAILABLE_TEMPLATES, build_persona_prompt  # noqa: E402
from tenant_config import build_bot_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def tenant_row(**overrides):
    """A stand-in for a Tenant row, with every column build_bot_config reads."""
    row = dict(
        id=1, business_name="Test Business", bot_name=None, bot_role_title=None, business_location=None,
        currency=None, industry=None, persona_template=None, human_handoff_number=None,
        front_office_number=None, handoff_rules=None, quick_menu=None, quick_buttons=None,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


def history(*user_messages):
    turns = []
    for text in user_messages:
        turns += [{"role": "user", "content": text}, {"role": "assistant", "content": "Sure."}]
    return turns


# ---------------------------------------------------------------------------
# 1. The packs themselves
# ---------------------------------------------------------------------------

def test_packs():
    print("\nIndustry packs")
    check(len(brain.INDUSTRY_KEYS) == 12 and "generic" in brain.INDUSTRY_KEYS, "12 industries, including generic")

    aliases = {}
    for key in brain.INDUSTRY_KEYS:
        pack = brain.get_pack(key)
        assert pack.persona in AVAILABLE_TEMPLATES, f"{key}: persona '{pack.persona}' has no template"
        assert pack.intake and pack.handoff_rules and pack.role_title, f"{key}: incomplete pack"
        for title in (*pack.buttons, pack.contact_button):
            # Meta counts UTF-16 units and rejects a button title over 20.
            assert len(title.encode("utf-16-le")) // 2 <= brain.MAX_BUTTON_TITLE, f"{key}: '{title}' too long"
        for alias in pack.aliases:
            assert alias not in brain.INDUSTRY_KEYS and alias not in aliases, f"alias '{alias}' is ambiguous"
            aliases[alias] = key
    check(True, "every pack has a persona template, intake, handoff rules and buttons within Meta's 20 characters")

    check(brain.normalise_industry("Hotel") == "hospitality", "'Hotel' -> hospitality")
    check(brain.normalise_industry("Real Estate") == "real_estate", "'Real Estate' -> real_estate")
    check(brain.normalise_industry("gym") == "fitness", "'gym' -> fitness")
    check(brain.normalise_industry("friendly") == "", "a tone word ('friendly') is not an industry")
    check(brain.resolve_industry(None, "retail") == "retail", "legacy persona_template='retail' still means retail")
    check(brain.resolve_industry(None, "friendly") == "generic", "unknown values fall back to generic")
    check(brain.get_pack("nonsense").key == "generic", "unknown key gets the generic pack")

    check(brain.detect_industry("teeth cleaning, braces, consultation with our dentist", "Smile Dental Clinic") == "clinic",
          "detects a clinic from its own words")
    check(brain.detect_industry("Welcome! We help with anything.", "ABC Holdings") == "",
          "does not guess when the business is unclear")
    check(brain.detect_industry("", "Grand Royal Hotel & Spa") == "", "a tie (hotel vs spa) is left undecided")


# ---------------------------------------------------------------------------
# 2. Tenant row -> BotConfig
# ---------------------------------------------------------------------------

def test_bot_config():
    print("\nTenant config")
    clinic = build_bot_config(tenant_row(industry="clinic"))
    check(clinic.persona_template == "clinic" and clinic.bot_role_title == "Patient Care Coordinator",
          "a clinic gets the clinic persona and job title")
    check("Medical advice" in clinic.handoff_rules, "a clinic hands medical questions to a human by default")
    check([b["title"] for b in clinic.quick_buttons()] == ["📅 Book Appointment", "🕒 Opening Hours", "📞 Call Reception"],
          "a clinic's greeting buttons are its own")
    check(clinic.quick_buttons()[-1]["id"] == brain.CONTACT_BUTTON_ID, "the contact button keeps a fixed id")

    stale = build_bot_config(tenant_row(industry="hospitality", persona_template="friendly"))
    check(stale.persona_template == "hospitality", "a stale persona value ('friendly') no longer forces the generic persona")

    custom = build_bot_config(tenant_row(
        industry="salon", bot_role_title="Front Desk", handoff_rules="Bridal only",
        quick_buttons="✂️ Price List\n📅 Book Now\nignored third line", persona_template="generic"))
    check(custom.bot_role_title == "Front Desk" and custom.handoff_rules == "Bridal only",
          "what the client typed beats the industry default")
    check([b["title"] for b in custom.quick_buttons()] == ["✂️ Price List", "📅 Book Now", "📞 Contact Us"],
          "client's own two buttons, then the contact button")
    check(custom.persona_template == "generic", "an explicit valid persona override is respected")

    legacy = build_bot_config(tenant_row(persona_template="hospitality"))
    check(legacy.industry == "hospitality", "rows from before the industry column still resolve")

    restaurant_prompt = build_persona_prompt(build_bot_config(tenant_row(industry="restaurant", business_name="Kandy Spice")))
    check("Kandy Spice" in restaurant_prompt and "You know the menu" in restaurant_prompt,
          "the persona prompt is the restaurant's")


# ---------------------------------------------------------------------------
# 3. The brain: language, gibberish, offline fallback
# ---------------------------------------------------------------------------

def test_language_and_gibberish():
    print("\nLanguage and gibberish detection")
    for message in ("Can you send me the brochure?", "How much for a wedding?",
                    "I would like to browse your products", "Is the cleaner available on Monday?"):
        check(ai_service.detect_language_intent(message, []) == "english", f"English stays English: {message!r}")
    for message in ("price list eka ewannako", "hariyata kiyanna", "ane bro meka kiyada"):
        check(ai_service.detect_language_intent(message, []) == "singlish", f"Singlish still detected: {message!r}")
    check(ai_service.detect_language_intent("මිල කීයද", []) == "sinhala", "Sinhala script still detected")

    check(not ai_service.is_gibberish("Do you have parking?"), "'Do you have parking?' is not gibberish")
    check(ai_service.is_gibberish("asdfgh") and ai_service.is_gibberish("ffsdsff"), "keyboard mashing still is")
    check(ai_service.is_gibberish("botox filler"), "unknown words with no context are still caught")
    check(not ai_service.is_gibberish("botox filler", frozenset({"botox"})),
          "...unless they are words from the tenant's own knowledge base")


def test_offline_fallback():
    print("\nOffline fallback (no AI provider)")
    salon = build_bot_config(tenant_row(industry="salon", business_name="Lotus Salon"))
    salon_kb = "## SERVICES & PRICES\n- Haircut (ladies): Rs. 1,500\n- Beard trim: Rs. 800\n- Bridal dressing: Rs. 45,000\n## HOURS\n- Open 9 AM to 7 PM, closed on Mondays"

    reply = ai_service.smart_knowledge_base_search("what is the price for a haircut", salon_kb, "", [], salon)
    check("Rs. 1,500" in reply, "a salon's price question is answered from the salon's knowledge base")
    check("Contact Details" not in reply, "'for' is no longer read as 'fo' (front office)")
    check("attach" not in reply.lower(), "does not claim to attach a PDF the salon never uploaded")

    reply = ai_service.smart_knowledge_base_search("are you closed on mondays", salon_kb, "", [], salon)
    check("closed on Mondays" in reply, "a non-price question finds its line too")

    reply = ai_service.smart_knowledge_base_search(
        "how much is a coffee?", "- Latte: Rs. 650\n- Our coffee is roasted in Kandy", "", [], salon)
    check("roasted" in reply, "'coffee' is matched as a word, not as the fee inside it")

    kb_with_rule = salon_kb + "\n\n--- LIVE ERP INVENTORY ---\n- Shampoo: LKR 2,400.00 (5 in stock)\n\n[CRITICAL RULE]: When quoting products or prices, ALWAYS refer to the live ERP inventory above."
    reply = ai_service.smart_knowledge_base_search("shampoo price?", kb_with_rule, "", [], salon)
    check("2,400" in reply and "CRITICAL RULE" not in reply, "instructions meant for the AI are never shown to a customer")

    clinic = build_bot_config(tenant_row(industry="clinic", business_name="City Clinic"))
    reply = ai_service.smart_knowledge_base_search(
        "hi", "", "", history("can I book an appointment with a doctor", "ok thanks"), clinic)
    check("an appointment" in reply and "event" not in reply, "a returning patient is welcomed back to 'an appointment'")

    reply = ai_service.smart_knowledge_base_search("hello", "", "", history("ok thanks", "yes please"), clinic)
    check("Welcome back" in reply and "Last time" not in reply, "with no recognisable topic, none is invented")

    venue = build_bot_config(tenant_row(industry="hospitality", quick_menu="Starlight Ballroom | 100-350 guests"))
    is_returning, topic = ai_service.get_returning_customer_context(
        history("tell me about the starlight ballroom", "ok"), venue)
    check(is_returning and topic == "Starlight Ballroom", "a tenant's own quick-menu names are used as topics")


async def test_prompt_assembly():
    print("\nSystem prompt sent to the AI")
    captured = {}

    async def fake_complete(role, messages, **kwargs):
        captured["system"] = messages[0]["content"]
        return llm_router.LLMResult(text="Sure, which day suits you?", provider="test", model="fake", latency=0.0)

    real_complete, real_candidates = llm_router.complete, llm_router.role_candidates
    llm_router.complete = fake_complete
    llm_router.role_candidates = lambda role: [("test", "fake")]
    try:
        clinic = build_bot_config(tenant_row(industry="clinic", business_name="City Clinic"))
        reply, handoff = await ai_service.generate_ai_response("Be kind.", "Consultation: by appointment.", [], "I have a fever", clinic)
        check(reply == "Sure, which day suits you?" and not handoff, "reply comes back through the normal path")
        system = captured["system"]
        check("INDUSTRY PLAYBOOK (Clinics, dental & healthcare)" in system, "the clinic playbook is in the prompt")
        check("Never diagnose" in system and "emergency" in system, "with the clinic's medical limits")
        check("Which service or doctor they need" in system, "and what to find out from a patient")
        check("calm voice at the front desk" in system, "in the clinic persona")

        shop = build_bot_config(tenant_row(industry="retail", business_name="Shoe Hub"))
        await ai_service.generate_ai_response("", "Sneakers: Rs. 9,500", [], "do you have size 42", shop)
        check("Never diagnose" not in captured["system"] and "live inventory" in captured["system"],
              "a shop gets the shop's limits, not the clinic's")
    finally:
        llm_router.complete, llm_router.role_candidates = real_complete, real_candidates


# ---------------------------------------------------------------------------
# 4. Database: the column migration and its one-time backfill
# ---------------------------------------------------------------------------

LEGACY_TENANTS = [
    # (business_name, phone_number_id, bot_role_title, system_prompt, persona_template)
    ("Wasala Nature Resort - Banquet & Events", "1001", "Virtual Concierge",
     "You are the concierge. Answer guests about wedding and event packages and ballroom options.", "friendly"),
    ("Colombo Shoe Hub", "1002", None, "You are a helpful assistant.", "retail"),
    ("ABC Holdings", "1003", None, "You are a helpful assistant.", "generic"),
    ("Smile Dental Clinic", "1004", None, "Help patients book a consultation with our dentist.", None),
]


def make_legacy_database():
    """A database as it looked before this change: a tenants table with no industry column."""
    db = sqlite3.connect(_DB_PATH)
    db.execute("""CREATE TABLE tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_name VARCHAR(255) NOT NULL,
        whatsapp_phone_number_id VARCHAR(255) NOT NULL UNIQUE, meta_access_token TEXT NOT NULL,
        system_prompt TEXT NOT NULL, knowledge_base TEXT NOT NULL, human_handoff_number VARCHAR(50),
        front_office_number VARCHAR(50), bank_details TEXT, bot_name VARCHAR(100), bot_role_title VARCHAR(150),
        business_location VARCHAR(255), currency VARCHAR(10), persona_template VARCHAR(50), handoff_rules TEXT,
        quick_menu TEXT, created_at DATETIME, updated_at DATETIME)""")
    for name, phone_id, role, prompt, persona in LEGACY_TENANTS:
        db.execute(
            "INSERT INTO tenants (business_name, whatsapp_phone_number_id, meta_access_token, system_prompt,"
            " knowledge_base, bot_role_title, persona_template, created_at, updated_at)"
            " VALUES (?, ?, 'EAA_placeholder_token', ?, 'Open 9 AM to 5 PM.', ?, ?, datetime('now'), datetime('now'))",
            (name, phone_id, prompt, role, persona))
    db.commit()
    db.close()


def stored_industries():
    db = sqlite3.connect(_DB_PATH)
    rows = dict(db.execute("SELECT whatsapp_phone_number_id, industry FROM tenants").fetchall())
    db.close()
    return rows


async def test_migration():
    print("\nDatabase migration")
    make_legacy_database()
    await init_db()
    industries = stored_industries()
    check(industries["1001"] == "hospitality", "a resort stuck on persona 'friendly' is backfilled as hospitality")
    check(industries["1002"] == "retail", "persona_template='retail' carries over")
    check(industries["1003"] == "generic", "an unclear business stays generic")
    check(industries["1004"] == "clinic", "a dental clinic with no persona is recognised")

    db = sqlite3.connect(_DB_PATH)
    db.execute("UPDATE tenants SET industry = NULL WHERE whatsapp_phone_number_id = '1003'")
    db.commit()
    db.close()
    await init_db()
    check(stored_industries()["1003"] is None, "the backfill runs once; later startups leave rows alone")


# ---------------------------------------------------------------------------
# 5. API and webhook
# ---------------------------------------------------------------------------

async def admin_token() -> str:
    async with AsyncSessionLocal() as db:
        user = AdminUser(username="tester", password_hash=hash_password("not-used"), role="owner")
        db.add(user)
        await db.flush()
        token = generate_session_token()
        db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
        await db.commit()
    return token


async def tenant_columns(phone_id: str) -> Tenant:
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_id))).scalar_one()


async def test_api(client: httpx.AsyncClient):
    print("\nBot profile API")
    resort = await tenant_columns("1001")
    profile = (await client.get(f"/api/tenants/{resort.id}/bot-profile")).json()
    check(profile["industry"] == "hospitality" and profile["persona_template"] == "hospitality",
          "GET shows the resolved industry and persona")
    check(len(profile["available_industries"]) == 12 and "handoff_rules" in profile["available_industries"][0],
          "GET lists every industry with its defaults for the portal")

    res = await client.patch(f"/api/tenants/{resort.id}/bot-profile", json={"industry": "not-a-real-industry"})
    check(res.status_code == 422, "an unknown industry is rejected")

    res = await client.patch(f"/api/tenants/{resort.id}/bot-profile", json={"industry": "Real Estate", "quick_buttons": "🏝️ Beach Plots"})
    saved = await tenant_columns("1001")
    check(res.status_code == 200 and saved.industry == "real_estate", "a common name is stored as its key")
    check(saved.persona_template == "", "changing industry drops the old persona value")
    profile = (await client.get(f"/api/tenants/{resort.id}/bot-profile")).json()
    check(profile["persona_template"] == "real_estate" and "Legal, title" in profile["handoff_rules"],
          "the new industry's persona and handoff defaults apply")
    check(profile["quick_buttons"] == "🏝️ Beach Plots", "custom greeting buttons are saved")

    res = await client.post("/api/tenants", json={
        "business_name": "Iron Gym", "whatsapp_phone_number_id": "1005",
        "meta_access_token": "EAA_placeholder_token", "industry": "gym"})
    check(res.status_code == 201 and res.json()["industry"] == "fitness", "a new tenant can be created with an industry")

    print("\nAuto-profile")
    shop = await tenant_columns("1002")
    real_complete_json = bot_profile_generator.llm.complete_json

    async def busy(*args, **kwargs):
        raise llm_router.LLMUnavailable("all providers busy")

    async def answers(*args, **kwargs):
        return ({"business_name": "Colombo Shoe Hub", "industry": "Shop", "bot_name": "Dilini",
                 "bot_role_title": "Sales Assistant", "system_prompt": "You sell shoes. Be helpful.",
                 "handoff_rules": ["Refunds", "Wholesale"], "quick_menu": "Sneakers | New arrivals",
                 "currency": "LKR", "persona_template": "friendly"},
                llm_router.LLMResult(text="", provider="test", model="fake", latency=0.0))

    try:
        bot_profile_generator.llm.complete_json = busy
        res = await client.post(f"/api/tenants/{shop.id}/auto-profile")
        untouched = await tenant_columns("1002")
        check(res.status_code == 503 and untouched.system_prompt == "You are a helpful assistant.",
              "when the AI is unavailable, the tenant's settings are left alone")

        bot_profile_generator.llm.complete_json = answers
        res = await client.post(f"/api/tenants/{shop.id}/auto-profile")
        generated = await tenant_columns("1002")
        check(res.status_code == 200 and generated.industry == "retail", "auto-profile stores a real industry")
        check(generated.persona_template is None and build_bot_config(generated).persona_template == "retail",
              "and no longer writes a tone word into persona_template")
        check(generated.handoff_rules == "Refunds\nWholesale", "a list answer is stored one rule per line")
    finally:
        bot_profile_generator.llm.complete_json = real_complete_json


def webhook_payload(phone_id: str, message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id},
        "messages": [{"from": "94770000001", **message}],
    }}]}]}


async def test_webhook(client: httpx.AsyncClient):
    print("\nWhatsApp webhook")
    sent = {"buttons": [], "texts": []}

    async def record_buttons(**kwargs):
        sent["buttons"].append(kwargs["buttons"])
        return {}

    async def record_text(**kwargs):
        sent["texts"].append(kwargs["message_text"])
        return {}

    async def canned_reply(**kwargs):
        return ai_service.BotReply("Hello! How can I help?")

    originals = (main.send_whatsapp_button_message, main.send_whatsapp_message, main.generate_bot_reply)
    main.send_whatsapp_button_message, main.send_whatsapp_message, main.generate_bot_reply = record_buttons, record_text, canned_reply
    try:
        await client.post("/webhook", json=webhook_payload("1004", {"id": "wamid.1", "type": "text", "text": {"body": "hi"}}))
        check(sent["buttons"] and [b["title"] for b in sent["buttons"][-1]] == ["📅 Book Appointment", "🕒 Opening Hours", "📞 Call Reception"],
              "greeting a clinic gets the clinic's buttons")

        await client.post("/webhook", json=webhook_payload("1002", {"id": "wamid.2", "type": "text", "text": {"body": "hello"}}))
        check([b["title"] for b in sent["buttons"][-1]][0] == "🛍️ Browse Products", "greeting a shop gets the shop's")

        async with AsyncSessionLocal() as db:
            from sqlalchemy import update
            await db.execute(update(Tenant).where(Tenant.whatsapp_phone_number_id == "1004").values(front_office_number="+94112345678"))
            await db.commit()
        sent["texts"].clear()
        await client.post("/webhook", json=webhook_payload("1004", {
            "id": "wamid.3", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "btn_contact", "title": "📞 Call Reception"}}}))
        check(any("+94112345678" in text for text in sent["texts"]),
              "tapping the contact button sends the number, whatever the button is called")
    finally:
        main.send_whatsapp_button_message, main.send_whatsapp_message, main.generate_bot_reply = originals


async def test_dynamic_kb():
    print("\nLive inventory in the knowledge base")
    from models import Product
    clinic, shop = await tenant_columns("1004"), await tenant_columns("1002")
    async with AsyncSessionLocal() as db:
        db.add(Product(tenant_id=shop.id, name="Running Shoe", price=12500, stock_quantity=4))
        await db.commit()
        check(await main.get_dynamic_kb(clinic, db) == clinic.knowledge_base,
              "a business with no products is not told 'No products currently listed'")
        kb = await main.get_dynamic_kb(shop, db)
        check("Running Shoe: LKR 12,500.00 (4 in stock)" in kb and "$" not in kb,
              "prices use the tenant's own currency, not '$'")


async def run_async_tests():
    await test_prompt_assembly()
    await test_migration()
    token = await admin_token()
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        await test_api(client)
        await test_webhook(client)
    await test_dynamic_kb()
    await engine.dispose()


if __name__ == "__main__":
    try:
        test_packs()
        test_bot_config()
        test_language_and_gibberish()
        test_offline_fallback()
        asyncio.run(run_async_tests())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
