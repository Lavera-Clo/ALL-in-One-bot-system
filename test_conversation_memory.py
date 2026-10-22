"""
Checks for conversation memory (conversation_memory.py): the bot knows who it is talking to,
how long since they last wrote, what they want and what they already said, and continues the
chat instead of starting over.

Offline: every AI call is a scripted fake; the database is a throwaway file (or PostgreSQL via
run_tests_postgres.py). whatsapp_bot.db is never opened.

    python test_conversation_memory.py
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "memory_test.db").replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import conversation_memory as cm  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, ChatHistory, Conversation, Tenant  # noqa: E402
from sqlalchemy import select  # noqa: E402
from tenant_config import BotConfig  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def row(minutes_ago, sender, text="x"):
    return SimpleNamespace(created_at=NOW - timedelta(minutes=minutes_ago), sender=sender,
                           role="user" if sender == "customer" else "assistant", content=text)


def conv(name="Nimal Perera", memory=None, upto=None):
    return SimpleNamespace(tenant_id=1, customer_phone="94771234567", display_name=name,
                           memory=json.dumps(memory) if memory else None, memory_upto_id=upto)


def test_context():
    print("\nWho they are and how long since they wrote")
    check(cm.usable_name("Nimal Perera") == "Nimal", "a WhatsApp name gives a first name to use")
    check(cm.usable_name("94771234567") == "" and cm.usable_name("😀✨") == "",
          "numbers and emoji are not used as a name")

    first = cm.build_context(conv(), [], newest_id=1, now=NOW)
    check(first.first_contact and first.customer_name == "Nimal", "a first message is recognised as first contact")
    text = cm.context_prompt(first)
    check("FIRST message" in text and "welcome back" in text.lower() and "Nimal" in text,
          "first contact: welcome them, by name, and don't say 'welcome back'")

    ongoing = cm.build_context(conv(), [row(30, "customer"), row(29, "bot"), row(3, "customer")], now=NOW)
    text = cm.context_prompt(ongoing)
    check(not ongoing.first_contact and not ongoing.returning_after_gap
          and "3 minutes ago" in text and "Don't greet them again" in text,
          "a chat in progress (3 minutes ago): continue, don't greet again")

    back = cm.build_context(conv(), [row(4 * 24 * 60, "customer"), row(4 * 24 * 60 - 1, "bot")], now=NOW)
    text = cm.context_prompt(back)
    check(back.returning_after_gap and "4 days ago" in text and "returning customer" in text,
          "after 4 days away: greet them as a returning customer")
    check("Local time now:" in text and "October" in text, "the bot knows the local date and time")

    print("\nWhat the bot remembers")
    brief = {"summary": "Asked about wedding packages for December.", "goal": "Book a wedding reception",
             "stage": "booking", "details": {"date": "14 December", "guests": "120"},
             "open_questions": ["Is there parking?"], "next_step": "Confirm the package they prefer",
             "tone": "excited"}
    text = cm.context_prompt(cm.build_context(conv(memory=brief), [row(5, "customer")], now=NOW))
    check("Book a wedding reception" in text and "booking" in text and "collect only what is still missing" in text,
          "their goal and stage are given, with how to handle that stage")
    check("NEVER ask for these again" in text and "guests: 120" in text and "date: 14 December" in text,
          "details they already gave are listed so they're never asked again")
    check("Is there parking?" in text and "Confirm the package" in text and "excited" in text,
          "open questions, the next step and their mood are included")
    check("Prices, availability and policies still come only from the knowledge base" in text,
          "the notes never become a source of business facts")

    print("\nKeeping the notes tidy")
    cleaned = cm.clean_brief({"summary": "x" * 900, "stage": "Ready To Buy", "evil": "drop me",
                              "details": {f"k{i}": f"v{i}" for i in range(15)}, "open_questions": "not a list",
                              "tone": 5})
    check(len(cleaned["summary"]) == 400 and cleaned["stage"] == "ready_to_buy" and "evil" not in cleaned
          and len(cleaned["details"]) == 10 and "open_questions" not in cleaned,
          "only the expected fields survive, short and well-formed")
    check(cm.clean_brief("nonsense") == {} and cm.clean_brief({"stage": "dancing"}) == {},
          "an unusable answer is ignored")
    check(not cm.should_refresh(cm.build_context(conv(), [row(1, "customer")], newest_id=2, now=NOW)),
          "no notes for a 2-message chat")
    check(cm.should_refresh(cm.build_context(conv(), [row(3, "customer"), row(2, "bot")], newest_id=3, now=NOW)),
          "notes are written once the chat has a few messages")
    fresh = cm.build_context(conv(memory=brief, upto=10), [row(3, "customer")] * 6, newest_id=11, now=NOW)
    stale = cm.build_context(conv(memory=brief, upto=10), [row(3, "customer")] * 6, newest_id=14, now=NOW)
    check(not cm.should_refresh(fresh) and cm.should_refresh(stale),
          "notes are refreshed every few messages, not on every one")


async def test_reply_pipeline():
    print("\nThe reply model gets the context")
    seen = []

    async def fake_complete(role, messages, **kw):
        seen.append((role, messages))
        return llm_router.LLMResult(text="For your *120 guests* on the 14th, the Grand Ballroom fits nicely.",
                                    provider="fake", model="m", latency=0.1)

    refreshed = []

    async def fake_refresh(tenant_id, phone, business_name):
        refreshed.append((tenant_id, phone, business_name))

    real = (llm_router.complete, llm_router.role_candidates, cm.refresh_brief, cm.REFRESH_DELAY_SECONDS)
    llm_router.complete = fake_complete
    llm_router.role_candidates = lambda role: [("fake", "m")]
    cm.refresh_brief = fake_refresh
    cm.REFRESH_DELAY_SECONDS = 0
    try:
        brief = {"goal": "Book a wedding reception", "stage": "booking", "details": {"guests": "120", "date": "14 December"}}
        config = BotConfig(tenant_id=1, business_name="Wasala Resort")
        config.conversation = cm.build_context(conv(memory=brief), [row(5, "customer"), row(4, "bot")], newest_id=9, now=NOW)
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(30)]
        reply = await ai_service.generate_bot_reply("Be kind.", "Grand Ballroom for 150 to 650 guests.", history,
                                                    "which hall would suit us?", config)
        await asyncio.sleep(0.05)
        system = seen[0][1][0]["content"]
        check("THIS CUSTOMER AND THIS CONVERSATION" in system and "guests: 120" in system and "Nimal" in system,
              "the system prompt carries who they are and what they told us")
        check("CONTINUING THE CONVERSATION LIKE A PERSON" in system and "Never ask for something they already told you" in system,
              "and the rules for continuing a chat like a person")
        check(len(seen[0][1]) == 1 + 16 + 1, "the model reads the last 16 messages (was 10)")
        check(len(seen) == 1 and "120 guests" in reply.text,
              "a number the customer gave earlier ('120 guests', kept in the notes) isn't treated as invented")
        check(refreshed == [(1, "94771234567", "Wasala Resort")], "the notes are refreshed in the background after replying")

        seen.clear()
        plain = BotConfig(tenant_id=1, business_name="Wasala Resort")
        await ai_service.generate_bot_reply("Be kind.", "Grand Ballroom for 150 to 650 guests.", [], "hi", plain)
        check("THIS CUSTOMER AND THIS CONVERSATION" not in seen[0][1][0]["content"],
              "without a conversation (a portal preview), no customer notes are added")
    finally:
        llm_router.complete, llm_router.role_candidates, cm.refresh_brief, cm.REFRESH_DELAY_SECONDS = real


async def test_refresh_and_webhook():
    print("\nWriting and storing the notes")
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lanka Leaf", whatsapp_phone_number_id="4242", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="Ceylon tea Rs 950.")
        db.add(t)
        await db.flush()
        db.add(Conversation(tenant_id=t.id, customer_phone="94770001111", customer_name="Kasun"))
        for i, (role, text) in enumerate([("user", "Hi, do you deliver to Kandy?"), ("assistant", "Yes we do."),
                                          ("user", "I need 3 packs of Ceylon tea for my mother"), ("assistant", "Lovely.")]):
            db.add(ChatHistory(tenant_id=t.id, customer_phone_number="94770001111", role=role, content=text,
                               sender="customer" if role == "user" else "bot"))
        user = AdminUser(username="leaf@memory.test", password_hash=hash_password("x"), role="client", tenant_id=t.id)
        db.add(user)
        await db.flush()
        token = generate_session_token()
        db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
        await db.commit()
        tenant_id = t.id

    asked = []

    async def fake_json(role, messages, **kw):
        asked.append((role, messages))
        return ({"summary": "Wants tea delivered to Kandy.", "goal": "Order 3 packs of Ceylon tea", "stage": "ready_to_buy",
                 "details": {"location": "Kandy", "quantity": "3 packs"}, "tone": "friendly", "junk": 1},
                llm_router.LLMResult(text="", provider="fake", model="m", latency=0.1))

    real = (llm_router.complete_json, llm_router.role_candidates)
    llm_router.complete_json = fake_json
    llm_router.role_candidates = lambda role: [("fake", "m")]
    try:
        brief = await cm.refresh_brief(tenant_id, "94770001111", "Lanka Leaf")
        async with AsyncSessionLocal() as db:
            c = (await db.execute(select(Conversation).where(Conversation.customer_phone == "94770001111"))).scalar_one()
            last_id = (await db.execute(select(ChatHistory.id).order_by(ChatHistory.id.desc()))).scalars().first()
        stored = json.loads(c.memory)
        check(stored["goal"] == "Order 3 packs of Ceylon tea" and stored["details"]["location"] == "Kandy"
              and "junk" not in stored and c.memory_upto_id == last_id,
              "the notes are written from the chat and saved on the contact")
        check(asked[0][0] == "fast" and "3 packs of Ceylon tea" in asked[0][1][1]["content"],
              "they're written by the fast model from the real chat")

        async def broken(role, messages, **kw):
            raise llm_router.LLMUnavailable("down")

        llm_router.complete_json = broken
        check(await cm.refresh_brief(tenant_id, "94770001111", "Lanka Leaf") is None, "an AI outage changes nothing")
        async with AsyncSessionLocal() as db:
            c = (await db.execute(select(Conversation).where(Conversation.customer_phone == "94770001111"))).scalar_one()
        check(json.loads(c.memory)["goal"] == "Order 3 packs of Ceylon tea", "and the earlier notes are kept")
    finally:
        llm_router.complete_json, llm_router.role_candidates = real

    print("\nThrough the webhook")
    contexts = []

    async def scripted(system_prompt, knowledge_base, chat_history, user_message, config):
        contexts.append(config.conversation)
        return ai_service.BotReply(f"Reply to {user_message}")

    real_reply = main.generate_bot_reply
    main.generate_bot_reply = scripted
    try:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            def hook(phone, body, mid, name):
                return {"entry": [{"changes": [{"value": {
                    "metadata": {"phone_number_id": "4242"},
                    "contacts": [{"wa_id": phone, "profile": {"name": name}}],
                    "messages": [{"from": phone, "id": mid, "type": "text", "text": {"body": body}}]}}]}]}

            await client.post("/webhook", json=hook("94770002222", "do you have green tea please", "mem1", "Dilani Jayasuriya"))
            first = contexts[-1]
            check(first.first_contact and first.customer_name == "Dilani" and first.tenant_id == tenant_id,
                  "a new customer: first contact, addressed by their WhatsApp name")
            await client.post("/webhook", json=hook("94770002222", "and how much is it", "mem2", "Dilani Jayasuriya"))
            second = contexts[-1]
            check(not second.first_contact and not second.returning_after_gap and second.previous_messages == 2,
                  "her next message continues the same chat")
            await client.post("/webhook", json=hook("94770001111", "can it come by friday", "mem3", "Kasun"))
            check(contexts[-1].brief.get("goal") == "Order 3 packs of Ceylon tea",
                  "a known customer's notes are handed to the reply")
            res = await client.get(f"/api/tenants/{tenant_id}/inbox/chats/94770001111/messages",
                                   headers={"Authorization": f"Bearer {token}"})
            check(res.json()["brief"].get("stage") == "ready_to_buy",
                  "staff see the notes in the inbox when they take over")
    finally:
        main.generate_bot_reply = real_reply


async def main_async():
    await init_db()
    test_context()
    await test_reply_pipeline()
    await test_refresh_and_webhook()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
