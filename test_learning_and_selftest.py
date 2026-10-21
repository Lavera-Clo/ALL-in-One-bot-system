"""
Checks for learning from real chats (learning_service.py), automatic testing
(self_test_service.py) and creating a business from the portal API.

Runs offline against a throwaway database: the AI providers are replaced by a scripted fake,
nothing is sent to WhatsApp, and whatsapp_bot.db is never opened.

    python test_learning_and_selftest.py
"""

import asyncio
import os
import sys
import tempfile

# Must be set before any project module is imported: config.py reads it at import time.
_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "learning_test.db").replace("\\", "/")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

import ai_service  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
import self_test_service  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from learning_service import answer_gap, dismiss_gap, record_gap  # noqa: E402
from models import AdminSession, AdminUser, KnowledgeGap, Tenant  # noqa: E402
from tenant_config import build_bot_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


KB = """## PACKAGES & PRICES
- Silver Menu, 150 Pax: Rs.708,000
- Gold Menu, 150 Pax: Rs.783,000
## FREQUENTLY ASKED QUESTIONS
Q: How much is the Silver menu for 150 guests?
A: The Silver menu for 150 guests is Rs.708,000.

Q: Gold menu 150 pax ekata kiyada?
A: Gold menu 150 pax ekata Rs.783,000.

Q: Is there a dance floor?
A: Yes, the ballroom has a wooden dance floor.

Q: wedding package eke ganan kiyada?
A: It includes a poruwa and a DJ (9:30am-3:30pm)."""


# ---------------------------------------------------------------------------
# A scripted stand-in for the AI providers
# ---------------------------------------------------------------------------

def scripted_reply(question: str) -> str:
    q = question.lower()
    if "silver" in q:
        return "The Silver menu for 150 guests is *Rs.708,000*. Which date are you thinking of?"
    if "gold" in q:
        return "Gold menu is Rs.783,000. Which date?"          # English reply to a Singlish question
    if "dance" in q:
        return "There's no dance floor, sorry."                # contradicts the FAQ: the judge catches it
    if "booked" in q:
        return "I can't confirm dates myself - only the events team can, once they check the calendar."
    if "discount" in q or "headache" in q or "tablets" in q:
        return "Let me pass you to our manager. [HUMAN_HANDOFF_REQUESTED]"
    if "helipad" in q or "parking" in q:
        return "I don't have that detail, let me check with the team. [NOT_IN_KNOWLEDGE_BASE]"
    return "Sure."


async def fake_complete(role, messages, **kwargs):
    question = messages[-1]["content"]
    return llm_router.LLMResult(text=scripted_reply(question), provider="test", model="fake", latency=0.0)


async def fake_complete_json(role, messages, **kwargs):
    prompt = messages[-1]["content"]
    result = llm_router.LLMResult(text="", provider="test", model="fake", latency=0.0)
    if "Pick the sections" in prompt:                         # the knowledge-base section picker
        return {"sections": []}, result
    if "checking a WhatsApp customer-service bot" in prompt:  # the self-test judge
        verdicts = []
        for block in prompt.split("\n#")[1:]:
            item_id = int(block.split("\n", 1)[0])
            if "dance floor" in block:
                verdicts.append({"id": item_id, "verdict": "wrong", "reason": "Says there is no dance floor; the FAQ says there is."})
            elif "poruwa" in block:
                verdicts.append({"id": item_id, "verdict": "bad_faq", "reason": "The answer lists inclusions, not a price."})
            else:
                verdicts.append({"id": item_id, "verdict": "correct", "reason": ""})
        return {"verdicts": verdicts}, result
    return {}, result


llm_router.complete = fake_complete
llm_router.complete_json = fake_complete_json
llm_router.role_candidates = lambda role: [("test", "fake")]


async def make_tenant(db, phone_id="2001", industry="hospitality", kb=KB) -> Tenant:
    tenant = Tenant(business_name="Lake View Hall", whatsapp_phone_number_id=phone_id,
                    meta_access_token="EAA_placeholder_token", system_prompt="Be helpful.",
                    knowledge_base=kb, industry=industry, human_handoff_number="+94770000001")
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


# ---------------------------------------------------------------------------
# 1. The bot says when it doesn't know - and the customer never sees the tag
# ---------------------------------------------------------------------------

async def test_unknown_flag(tenant):
    print("\nKnowing what it doesn't know")
    config = build_bot_config(tenant)
    reply = await ai_service.generate_bot_reply("Be helpful.", KB, [], "Is there parking for 100 cars?", config)
    check(reply.unknown and "NOT_IN_KNOWLEDGE_BASE" not in reply.text and "check with the team" in reply.text,
          "a question the documents don't cover is flagged, and the tag is removed from the reply")
    reply = await ai_service.generate_bot_reply("Be helpful.", KB, [], "How much is the Silver menu for 150 guests?", config)
    check(not reply.unknown and "708,000" in reply.text, "a covered question is not flagged")
    text, handoff = await ai_service.generate_ai_response("Be helpful.", KB, [], "Is there parking?", config)
    check("NOT_IN" not in text and handoff is False, "the older two-value function still works, tag-free")

    # The model admits it doesn't know but forgets the tag: still filed.
    check(ai_service._sounds_unsure("I honestly am not sure if we have dedicated parking, let me check with the front desk team."),
          "an untagged 'not sure, let me check with the team' reply is still recognised")
    check(not ai_service._sounds_unsure("The Silver menu for 150 guests is Rs.708,000. Which date are you thinking of?"),
          "a normal answer is not mistaken for 'not sure'")

    # A business with nothing uploaded yet must never answer from the model's imagination.
    empty = "No business documentation has been uploaded yet."
    reply = await ai_service.generate_bot_reply("Be helpful.", empty, [], "Do you have a pharmacy inside the clinic?", config)
    check(reply.unknown and reply.handoff and "pharmacy" not in reply.text.lower() and "check that with our team" in reply.text,
          "with no knowledge yet, a real question gets 'let me check with the team', is handed over and filed")
    reply = await ai_service.generate_bot_reply("Be helpful.", empty, [], "pharmacy ekak thiyenawada?", config)
    check("ape team" in reply.text, "...in the customer's language")
    reply = await ai_service.generate_bot_reply("Be helpful.", empty, [], "hi", config)
    check(not reply.unknown and not reply.handoff, "a greeting still gets a normal reply")


# ---------------------------------------------------------------------------
# 2. Learning: gaps are collected, merged, answered, and reach the knowledge base
# ---------------------------------------------------------------------------

async def test_learning(tenant):
    print("\nLearning from real chats")
    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tenant.id)
        first = await record_gap(db, tenant.id, "Is there parking at the hall?", "I'll check.", "english", "9477111")
        again = await record_gap(db, tenant.id, "is there parking at the hall", "I'll check.", "english", "9477222")
        check(first.id == again.id and again.times_asked == 2, "the same question from two customers is counted on one row")

        other = await record_gap(db, tenant.id, "Is there parking for 150 cars?", "", "english")
        check(other.id != first.id, "questions about different numbers are kept apart")
        check(await record_gap(db, tenant.id, "parking?") is None, "a one-word message is not filed")

        await answer_gap(db, tenant, first, "Yes, free parking for 80 cars next to the hall.")
        await db.refresh(tenant)
        check("===== SOURCE: Answers from the owner =====" in tenant.knowledge_base
              and "Q: Is there parking at the hall?\nA: Yes, free parking for 80 cars" in tenant.knowledge_base,
              "the owner's answer joins the knowledge base as an FAQ entry")
        check(tenant.knowledge_base.startswith("## PACKAGES & PRICES") and "Rs.708,000" in tenant.knowledge_base,
              "knowledge typed in directly (no documents) is kept when an answer is taught")

        config = build_bot_config(tenant)
        allowed = ai_service.unsupported_reply_numbers("Parking for 80 cars.", tenant.knowledge_base, config)
        check(allowed == [], "a number the owner taught passes the made-up-number check")

        await dismiss_gap(db, tenant, first)
        await db.refresh(tenant)
        check("Answers from the owner" not in tenant.knowledge_base and "Rs.708,000" in tenant.knowledge_base,
              "removing an answer takes it out again, and the typed knowledge stays")


# ---------------------------------------------------------------------------
# 3. Automatic test
# ---------------------------------------------------------------------------

def test_question_selection():
    print("\nAutomatic test - choosing questions")
    pairs = self_test_service.faq_pairs(KB)
    check(len(pairs) == 4 and pairs[0]["source"] == "General", "FAQ pairs are read, with their document name")
    picked = self_test_service.pick_questions(pairs, limit=2)
    check(len(picked) == 2 and len({self_test_service.detect_language_intent(p["question"], []) for p in picked}) == 2,
          "the picked questions cover both languages before repeating one")
    cases = self_test_service.build_cases(pairs, "clinic")
    check([c["kind"] for c in cases[-3:]] == ["unknown", "handoff", "handoff"] and "tablets" in cases[-1]["question"],
          "behaviour checks are added, with the industry's own safety question")
    check(self_test_service.reply_language("Gold menu eka Rs.783,000 wenawa, oyata date eka kiyanna puluwanda?") == "singlish",
          "a Singlish reply is recognised")
    check(self_test_service.reply_language("Premier Menu එක 100 pax Grand Ballroom එකට Rs.709,000. Meka kohomada?") == "mixed",
          "a reply mixing Sinhala script with romanised Sinhala is flagged")
    check(self_test_service.reply_language("Menu 01 extra person charge eka Rs. 2,250 /- per person. Meka ganna event ekakata, nadda?") == "singlish",
          "a Singlish reply built from small everyday words is recognised")
    check(self_test_service.reply_language("Ow, obviously! We have wooden chairs and round tables for weddings.") == "english",
          "one Sinhala word in an English reply doesn't make it Singlish")
    check(self_test_service.key_numbers("Open 9:30am-3:30pm, Rs.708,000 for 25 pax") == ["708000"],
          "only prices and amounts must appear in the reply; times and small counts are left to the judge")


async def test_self_test_run(tenant):
    print("\nAutomatic test - full run")
    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tenant.id)
        run = await self_test_service.start_run(db, tenant)
        try:
            await self_test_service.start_run(db, tenant)
            check(False, "a second run is refused while one is going")
        except ValueError:
            check(True, "a second run is refused while one is going")
    await self_test_service.run_self_test(run.id)

    async with AsyncSessionLocal() as db:
        payload = self_test_service.run_payload(await db.get(type(run), run.id))
    results = {r["question"]: r for r in payload["results"]}
    check(payload["status"] == "done" and payload["total"] == 7, "the run finishes: 4 FAQ questions + 3 behaviour checks")
    check(results["How much is the Silver menu for 150 guests?"]["passed"], "a correct price answer passes")
    gold = results["Gold menu 150 pax ekata kiyada?"]
    check(not gold["passed"] and any("replied in english" in p for p in gold["problems"]),
          "an English reply to a Singlish question fails, with the reason")
    dance = results["Is there a dance floor?"]
    check(not dance["passed"] and any("wrong" in p.lower() for p in dance["problems"]),
          "a wrong answer with no numbers is caught by the judge")
    check(results[self_test_service.UNKNOWN_PROBE]["passed"], "admitting an unknown fact passes")
    check(results[self_test_service.DISCOUNT_PROBE]["passed"], "handing a discount request to a person passes")
    booking = results[self_test_service.SAFETY_PROBES["hospitality"]]
    check(booking["passed"] and "own words" in booking.get("note", ""),
          "declining to confirm a booking in its own words passes, without the formal hand-over")
    mismatched = results["wedding package eke ganan kiyada?"]
    check(mismatched["passed"] and "doesn't match its question" in mismatched.get("note", ""),
          "an FAQ whose answer doesn't fit its question is flagged for the owner, not failed")
    check(payload["passed"] == 5 and payload["score"] == 71, "score = passed / total")


# ---------------------------------------------------------------------------
# 4. API: new business, gaps, teach, self-test; and the webhook files gaps
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
        industries = (await client.get("/api/industries")).json()
        check(len(industries) == 12 and industries[0]["key"] == "hospitality", "the industry list is available to the form")

        res = await client.post("/api/tenants", json={
            "business_name": "City Care", "industry": "clinic", "whatsapp_phone_number_id": "test-123",
            "meta_access_token": "EAA_placeholder_token", "system_prompt": "Be kind.",
            "knowledge_base": "No business documentation has been uploaded yet."})
        check(res.status_code == 201 and res.json()["industry"] == "clinic", "a business is created in test mode")
        clinic_id = res.json()["id"]

        res = await client.post(f"/api/tenants/{clinic_id}/teach", json={"question": "Do you take walk-ins?", "answer": "Yes, until 6 PM."})
        gaps = (await client.get(f"/api/tenants/{clinic_id}/gaps")).json()
        check(res.status_code == 200 and gaps["answered"][0]["answer"] == "Yes, until 6 PM.", "teaching an answer directly works")
        kb = (await client.get(f"/api/tenants/{clinic_id}/documents")).json()["knowledge_base"]
        check("Q: Do you take walk-ins?" in kb and "No business documentation" not in kb,
              "a business with no documents yet still learns taught answers")

        res = await client.post(f"/api/tenants/{clinic_id}/self-test")
        check(res.status_code == 200, "the automatic test starts from the API")
        latest = (await client.get(f"/api/tenants/{clinic_id}/self-test")).json()["run"]
        check(latest["status"] == "done" and latest["total"] >= 3, "the test finished in the background with results")

        # The webhook: a question the documents don't cover lands in the owner's list.
        sent = []

        async def record_text(**kwargs):
            sent.append(kwargs["message_text"])
            return {}

        original = main.send_whatsapp_message
        main.send_whatsapp_message = record_text
        try:
            await client.post("/webhook", json={"entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "test-123"},
                "messages": [{"from": "94770009999", "id": "wamid.gap1", "type": "text",
                              "text": {"body": "Is there parking near the clinic?"}}]}}]}]})
        finally:
            main.send_whatsapp_message = original
        gaps = (await client.get(f"/api/tenants/{clinic_id}/gaps")).json()
        check(any(g["question"] == "Is there parking near the clinic?" for g in gaps["open"]),
              "a real chat question the bot couldn't answer appears in 'Questions to answer'")
        check(sent and all("NOT_IN" not in text for text in sent), "the customer never sees the internal tag")

        gap_id = gaps["open"][0]["id"]
        res = await client.post(f"/api/tenants/{clinic_id}/gaps/{gap_id}/answer", json={"answer": ""})
        check(res.status_code == 400, "an empty answer is refused")
        res = await client.post(f"/api/tenants/{clinic_id}/gaps/{gap_id}/answer",
                                json={"answer": "Yes, free parking behind the building.", "question": "Is there parking?"})
        gaps = (await client.get(f"/api/tenants/{clinic_id}/gaps")).json()
        check(res.status_code == 200 and not gaps["open"] and any(g["question"] == "Is there parking?" for g in gaps["answered"]),
              "answering moves it to the taught list, with the owner's tidied wording")


async def main_async():
    await init_db()
    async with AsyncSessionLocal() as db:
        tenant = await make_tenant(db)
    await test_unknown_flag(tenant)
    await test_learning(tenant)
    test_question_selection()
    await test_self_test_run(tenant)
    await test_api()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
