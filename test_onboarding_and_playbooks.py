"""
Checks for automatic understanding of a new business (understanding_service.py) and for
training on conversation playbooks (playbook_pipeline.py).

Runs offline against a throwaway database: the AI providers are replaced by a scripted fake,
nothing is sent to WhatsApp, and whatsapp_bot.db is never opened.

    python test_onboarding_and_playbooks.py
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "onboarding_test.db").replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
import playbook_pipeline as pb  # noqa: E402
import understanding_service as us  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from document_service import create_document, get_tenant_documents, run_training  # noqa: E402
from models import AdminSession, AdminUser, BotTestRun, Tenant  # noqa: E402
from sqlalchemy import select  # noqa: E402
from tenant_config import build_bot_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


CLINIC_KB = """## DOCTORS & FEES
- Dr. Perera (General Physician): consultation Rs. 2,500, weekdays 4 PM to 8 PM
- Dr. Silva (Dentist): consultation Rs. 3,000
- Patients can book an appointment by phone
## CONTACT & LOCATION
- 45 Galle Road, Colombo 03"""

PLAYBOOK_TEXT = """CONVERSATION PLAYBOOK
Always find out the event type, guest count and date.

Customer: Hi, how much is a wedding package?
Bot (AI Response): Congratulations! Our Silver package starts around Rs. 885,000. How many guests are you expecting?

Customer: That's too expensive.
Bot: I understand. Many couples choose the Silver menu and add a few extras later. What budget did you have in mind?

Customer: Can I call someone?
Bot: Of course, call our hotline on +94 3422 75000."""

# ---------------------------------------------------------------------------
# Scripted AI
# ---------------------------------------------------------------------------

captured = {"system": []}
profile_answer = {"industry": "clinic", "industry_confidence": "high"}


async def fake_complete_json(role, messages, **kwargs):
    prompt = "\n".join(str(m.get("content")) for m in messages if isinstance(m.get("content"), str))
    result = llm_router.LLMResult(text="", provider="test", model="fake", latency=0.0)
    if "expert AI assistant builder" in prompt:
        return {"business_name": "City Care", "industry": profile_answer["industry"],
                "industry_confidence": profile_answer["industry_confidence"],
                "industry_evidence": ["consultation Rs. 2,500", "Dr. Perera (General Physician)", "book an appointment"],
                "business_location": "Colombo 03, Sri Lanka", "bot_name": "Nila", "bot_role_title": "Patient Care Coordinator",
                "system_prompt": "You help patients of City Care book appointments.", "handoff_rules": ["Medical advice", "Complaints"],
                "quick_menu": "Doctors | Who is available\nFees | Consultation fees", "currency": "LKR"}, result
    if "CONVERSATION PLAYBOOK" in prompt:
        return {"title": "Wedding sales playbook",
                "goals": ["Find out the event type", "Find out the guest count", "Find out the date"],
                "style": ["Warm and congratulating"], "sales": ["Suggest the Silver menu, add extras later"],
                "handoff": ["Customer asks to call someone"],
                "objections": [{"when": "the customer says it is too expensive", "do": "Empathise and ask their budget"}],
                "examples": [
                    {"customer": "Hi, how much is a wedding package?",
                     "bot": "Congratulations! Our Silver package starts around Rs. 885,000. How many guests are you expecting?"},
                    {"customer": "That's too expensive.", "bot": "I understand. What budget did you have in mind?"},
                    {"customer": "Can I call someone?", "bot": "Of course, call our hotline on +94 3422 75000."}]}, result
    if "Pick the sections" in prompt:
        return {"sections": []}, result
    if "checking a WhatsApp customer-service bot" in prompt:
        ids = [int(line[1:]) for line in prompt.splitlines() if line.startswith("#") and line[1:].isdigit()]
        return {"verdicts": [{"id": i, "verdict": "correct", "reason": ""} for i in ids]}, result
    return {}, result


async def fake_complete(role, messages, **kwargs):
    captured["system"].append(messages[0]["content"])
    return llm_router.LLMResult(text="Congratulations! How many guests are you expecting?", provider="test",
                                model="fake", latency=0.0)


llm_router.complete = fake_complete
llm_router.complete_json = fake_complete_json
llm_router.role_candidates = lambda role: [("test", "fake")]


async def add_tenant(name, phone_id, kb, **kw) -> int:
    async with AsyncSessionLocal() as db:
        kw.setdefault("system_prompt", f"You are the WhatsApp assistant for {name}. Answer customers warmly.")
        t = Tenant(business_name=name, whatsapp_phone_number_id=phone_id, meta_access_token="EAA_placeholder_token",
                   knowledge_base=kb, **kw)
        db.add(t)
        await db.commit()
        return t.id


async def get_tenant(tid) -> Tenant:
    async with AsyncSessionLocal() as db:
        return await db.get(Tenant, tid)


# ---------------------------------------------------------------------------
# 1. Playbook pieces
# ---------------------------------------------------------------------------

def test_playbook_pieces():
    print("\nPlaybook recognition and price blanking")
    check(pb.looks_like_playbook(PLAYBOOK_TEXT), "a document of example chats is recognised as a playbook")
    check(not pb.looks_like_playbook(CLINIC_KB), "a price list is not")
    text, n = pb.blank_amounts("Silver starts around Rs. 885,000 (Rs. 3,595 per guest) for 150 guests. Call +94 3422 75000.")
    check(n == 2 and "[amount]" in text and "150 guests" in text and "+94 3422 75000" in text,
          "prices are blanked; guest counts and phone numbers are kept")
    check("2026" in pb.blank_amounts("Here is our 2026 brochure, from Rs. 99,500")[0],
          "years are not mistaken for prices")
    text, _ = pb.render_playbook({"examples": [{"customer": "Rs. 885,000 is above our budget",
                                                "bot": "Sharing it now! [SEND_DOC: 2026 Brochure] Silver is Rs. 708,000."}]})
    check("885,000" not in text and "708,000" not in text and "SEND_DOC" not in text and "2026" not in text.replace("[amount]", ""),
          "example chats lose prices on both sides, and copied internal tags")
    check(pb.amount_conflicts(PLAYBOOK_TEXT, "Silver: Rs. 783,000") == ["Rs. 885,000"],
          "a playbook price that the price list doesn't have is reported (phone numbers are not)")
    check(us.combine("clinic", "high", "clinic") == ("clinic", "high"), "two agreeing opinions = high confidence")
    check(us.combine("clinic", "high", "salon") == ("clinic", "low"), "disagreeing opinions = low confidence")
    check(us.combine("", "", "retail") == ("retail", "medium"), "keyword opinion alone = medium")
    check(us.combine("", "", "") == ("generic", "low"), "no opinion = generic, low")


# ---------------------------------------------------------------------------
# 2. Training a playbook end to end
# ---------------------------------------------------------------------------

async def test_playbook_training():
    print("\nTraining a playbook")
    tid = await add_tenant("Lake Hall", "3001", "## PACKAGES\n- Silver Menu, 150 Pax: Rs. 783,000", industry="hospitality",
                           industry_source="owner")
    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tid)
        doc = await create_document(db, tenant, pasted_text=PLAYBOOK_TEXT, doc_type="both")
    check(doc.doc_type == "playbook" and "playbook" in doc.training_stage and not doc.file_url,
          "uploaded as 'answer and send', it is recognised as a playbook - and never offered to customers as a file")

    await run_training(doc.id)
    async with AsyncSessionLocal() as db:
        doc = (await get_tenant_documents(db, tid))[0]
        tenant = await db.get(Tenant, tid)
        config = build_bot_config(tenant, await get_tenant_documents(db, tid, active_only=True))
        runs = (await db.execute(select(BotTestRun).where(BotTestRun.tenant_id == tid))).scalars().all()
    report = __import__("json").loads(doc.training_report)
    check(doc.status == "ready" and "## CONVERSATION GOALS" in doc.summary and "## EXAMPLE CONVERSATIONS" in doc.summary,
          "it trains into goals, style, sales, objections and example chats")
    check("885,000" not in doc.summary and "[amount]" in doc.summary and "+94 3422 75000" in doc.summary,
          "the example's price is hidden; the phone number stays")
    check(report["amounts_not_in_knowledge_base"] == ["Rs. 885,000"] and report["examples"] == 3,
          "the report flags the playbook price that disagrees with the price list")
    check("Playbook" not in tenant.knowledge_base and "885" not in tenant.knowledge_base
          and "Silver Menu, 150 Pax: Rs. 783,000" in tenant.knowledge_base,
          "the playbook never enters the knowledge base, and the typed price list is kept")
    check("Find out the guest count" in config.playbook, "the bot's config carries the playbook")

    reply = await ai_service.generate_bot_reply(tenant.system_prompt, tenant.knowledge_base, [], "hi, wedding prices?", config)
    system = captured["system"][-1]
    check("THIS BUSINESS'S CONVERSATION PLAYBOOK" in system and "Find out the guest count" in system and "[amount]" in system,
          "the playbook is in the prompt for every reply, with the rule that its examples are style, not facts")
    check(runs and runs[0].status == "done", "training a playbook re-runs the automatic test by itself")
    results = __import__("json").loads(runs[0].results)
    playbook_cases = [r for r in results if r["kind"] == "playbook"]
    check(len(playbook_cases) == 3 and playbook_cases[0]["question"] == "Hi, how much is a wedding package?",
          "the playbook's own example chats became test questions")

    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tid)
        from document_service import update_document
        try:
            await update_document(db, doc, tenant, doc_type="knowledge")
            check(False, "a playbook can't be quietly turned into a facts document")
        except ValueError:
            check(True, "a playbook can't be quietly turned into a facts document")


# ---------------------------------------------------------------------------
# 3. Understanding a business automatically
# ---------------------------------------------------------------------------

async def test_understanding():
    print("\nUnderstanding a new business")
    tid = await add_tenant("City Care", "3002", CLINIC_KB, industry_source="auto")
    state = await us.understand_business(tid, run_test=False)
    tenant = await get_tenant(tid)
    check(tenant.industry == "clinic" and state["confidence"] == "high",
          "the industry is worked out from the documents, and two opinions agreeing = high confidence")
    check(state["evidence"] and "consultation Rs. 2,500" in state["evidence"], "with evidence quoted from the documents")
    check(tenant.bot_name == "Nila" and tenant.business_location == "Colombo 03, Sri Lanka" and tenant.currency == "LKR"
          and tenant.handoff_rules == "Medical advice\nComplaints",
          "empty profile fields are filled in")
    check(tenant.system_prompt == "You help patients of City Care book appointments.",
          "the placeholder prompt from the New business form is replaced")
    check(build_bot_config(tenant).persona_template == "clinic", "and the bot now speaks as a clinic")

    # The owner's own choices are never overwritten.
    tid2 = await add_tenant("Smile Studio", "3003", CLINIC_KB, industry="salon", industry_source="owner",
                            bot_name="Ruvi", currency="USD", system_prompt="Owner's own instructions.")
    state = await us.understand_business(tid2, run_test=False)
    tenant = await get_tenant(tid2)
    check(tenant.industry == "salon" and state["suggestion"] == "clinic",
          "an industry the owner chose is kept; the disagreement is offered as a suggestion")
    check(tenant.bot_name == "Ruvi" and tenant.currency == "USD" and tenant.system_prompt == "Owner's own instructions.",
          "what the owner typed is never overwritten")

    # Disagreement between the two opinions is reported as low confidence.
    profile_answer.update(industry="retail", industry_confidence="high")
    tid3 = await add_tenant("Mixed Co", "3004", CLINIC_KB, industry_source="auto")
    state = await us.understand_business(tid3, run_test=False)
    check(state["confidence"] == "low" and state["ai_opinion"] == "retail" and state["keyword_opinion"] == "clinic",
          "when the AI and the word count disagree, it says so (low confidence) for the owner to check")
    profile_answer.update(industry="clinic", industry_confidence="high")

    # No AI available: nothing is changed, and the failure is visible.
    real = llm_router.complete_json

    async def down(*a, **k):
        raise llm_router.LLMUnavailable("all busy")

    llm_router.complete_json = down
    try:
        tid4 = await add_tenant("Quiet Co", "3005", CLINIC_KB, industry_source="auto")
        state = await us.understand_business(tid4, run_test=False)
        tenant = await get_tenant(tid4)
        check(state["status"] == "failed" and tenant.industry is None and tenant.bot_name is None,
              "if no AI is available, nothing is changed and the reason is shown")
    finally:
        llm_router.complete_json = real


# ---------------------------------------------------------------------------
# 4. API
# ---------------------------------------------------------------------------

async def test_api():
    print("\nPortal API")
    async with AsyncSessionLocal() as db:
        user = AdminUser(username="tester", password_hash=hash_password("not-used"), role="owner")
        db.add(user)
        await db.flush()
        token = generate_session_token()
        db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
        await db.commit()

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        res = await client.post("/api/tenants", json={
            "business_name": "New Place", "whatsapp_phone_number_id": "test-9", "meta_access_token": "EAA_placeholder_token",
            "system_prompt": "You are the WhatsApp assistant for New Place. Answer warmly.",
            "knowledge_base": "No business documentation has been uploaded yet."})
        new_id = res.json()["id"]
        check((await get_tenant(new_id)).industry_source == "auto", "a business created without an industry is left for the system to work out")

        res = await client.post(f"/api/tenants/{new_id}/understand")
        check(res.status_code == 400, "understanding needs at least one document first")

        u = (await client.get(f"/api/tenants/{new_id}/understanding")).json()
        check("status" not in u and u["industry"] is None and u["industry_source"] == "auto",
              "before any document, the understanding endpoint simply reports nothing worked out yet")

        res = await client.patch(f"/api/tenants/{new_id}/bot-profile", json={"industry": "fitness"})
        check((await get_tenant(new_id)).industry_source == "owner", "choosing an industry in the profile marks it as the owner's choice")
        u = (await client.get(f"/api/tenants/{new_id}/understanding")).json()
        check(u["industry"] == "fitness" and u["industry_source"] == "owner", "the understanding endpoint reports it")

        res = await client.post("/api/tenants", json={
            "business_name": "Picked", "whatsapp_phone_number_id": "test-10", "meta_access_token": "EAA_placeholder_token",
            "industry": "restaurant"})
        check((await get_tenant(res.json()["id"])).industry_source == "owner", "an industry picked on the New business form is the owner's choice")


async def main_async():
    await init_db()
    test_playbook_pieces()
    await test_playbook_training()
    await test_understanding()
    await test_api()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
