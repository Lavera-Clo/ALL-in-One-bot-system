"""
Checks for the shared database (sync_service.py): contacts kept for good, messages kept for each
business's history period, devices syncing by change stamps, sends that can't double up,
messages a phone sent by itself, WhatsApp's delivery/read ticks, and business separation.

Offline, on a throwaway database (SQLite, or PostgreSQL via run_tests_postgres.py).
whatsapp_bot.db is never opened.

    python test_sync_and_retention.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "sync_test.db").replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import main  # noqa: E402
import whatsapp_client  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, ChatHistory, Conversation, DailyMessageStat, Tenant  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sync_service import cleanup_old_messages  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
NIMAL, KASUN = "94771234567", "94770000002"


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


async def scripted_reply(system_prompt, knowledge_base, chat_history, user_message, config, **_):
    return ai_service.BotReply(f"Bot: {user_message}")


main.generate_bot_reply = scripted_reply
sends = []


async def count_sends(pid, to, body, kind, ok, *rest):
    sends.append((to, body, ok))


whatsapp_client.add_send_listener(count_sends)


def webhook(phone, body, msg_id, name="Nimal Perera", phone_id="7777"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id},
        "contacts": [{"wa_id": phone, "profile": {"name": name}}],
        "messages": [{"from": phone, "id": msg_id, "type": "text", "text": {"body": body}}]}}]}]}


def receipts(*items, phone_id="7777"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id},
        "statuses": [dict(zip(("id", "status", "errors"), i)) for i in items]}}]}]}


async def setup():
    async with AsyncSessionLocal() as db:
        a = Tenant(business_name="Lanka Leaf", whatsapp_phone_number_id="7777", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="x")
        b = Tenant(business_name="Other", whatsapp_phone_number_id="8888", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="x")
        db.add_all([a, b])
        await db.flush()
        tokens = {}
        for key, tid in (("a", a.id), ("b", b.id)):
            user = AdminUser(username=f"{key}@sync.test", password_hash=hash_password("x"), role="client", tenant_id=tid)
            db.add(user)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=user.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        await db.commit()
        return a.id, b.id, tokens


async def test_all():
    a_id, b_id, tokens = await setup()
    A = {"Authorization": f"Bearer {tokens['a']}"}
    B = {"Authorization": f"Bearer {tokens['b']}"}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        base = f"/api/tenants/{a_id}"

        print("\nContacts")
        await c.post("/webhook", json=webhook(NIMAL, "Hi, do you have Ceylon tea?", "m1"))
        await c.post("/webhook", json=webhook(KASUN, "hello there friend", "m2", name="Kasun"))
        contacts = {x["phone"]: x for x in (await c.get(f"{base}/contacts", headers=A)).json()["contacts"]}
        check(contacts[NIMAL]["whatsappName"] == "Nimal Perera" and contacts[NIMAL]["messageCount"] == 2
              and contacts[NIMAL]["firstContact"] > 0, "a customer becomes a contact: name, first contact, message count")
        r = await c.patch(f"{base}/contacts/{NIMAL}", headers=A, json={"saved_name": "Nimal (wholesale)", "notes": "Pays cash"})
        check(r.json()["name"] == "Nimal (wholesale)" and r.json()["notes"] == "Pays cash", "the business can name a contact and add notes")
        chats = {x["phone"]: x for x in (await c.get(f"{base}/inbox/chats", headers=A)).json()["chats"]}
        check(chats[NIMAL]["name"] == "Nimal (wholesale)", "the business's name for them is shown before their WhatsApp name")
        await c.post("/webhook", json=webhook(NIMAL, "and green tea?", "m3", name="Nimal P."))
        contacts = {x["phone"]: x for x in (await c.get(f"{base}/contacts", headers=A)).json()["contacts"]}
        check(contacts[NIMAL]["name"] == "Nimal (wholesale)" and contacts[NIMAL]["whatsappName"] == "Nimal P.",
              "a new WhatsApp name is recorded without overwriting the business's name")
        found = (await c.get(f"{base}/contacts?q=wholesale", headers=A)).json()["contacts"]
        check([x["phone"] for x in found] == [NIMAL], "contacts can be searched by name")

        print("\nSyncing a device")
        first = (await c.get(f"{base}/sync?since=0", headers=A)).json()
        check(len(first["messages"]) == 6 and {x["phone"] for x in first["contacts"]} == {NIMAL, KASUN}
              and first["historyDays"] == 21, "a new device gets every kept message and every contact")
        cursor = first["cursor"]
        again = (await c.get(f"{base}/sync?since={cursor}", headers=A)).json()
        check(again["messages"] == [] and again["contacts"] == [] and again["cursor"] == cursor,
              "asking again with the cursor: nothing new, cursor unchanged")
        await c.post("/webhook", json=webhook(KASUN, "price list please", "m4", name="Kasun"))
        news = (await c.get(f"{base}/sync?since={cursor}", headers=A)).json()
        check([m["text"] for m in news["messages"]] == ["price list please", "Bot: price list please"]
              and [x["phone"] for x in news["contacts"]] == [KASUN], "then only what's new: the message, the reply, the contact")
        page = (await c.get(f"{base}/sync?since=0&limit=3", headers=A)).json()
        rest = (await c.get(f"{base}/sync?since={page['cursor']}&limit=100", headers=A)).json()
        check(page["hasMore"] and len(page["messages"]) == 3 and len(rest["messages"]) == 5 and not rest["hasMore"]
              and not ({m["id"] for m in page["messages"]} & {m["id"] for m in rest["messages"]}),
              "big histories come in pages, nothing missed or repeated")
        overlap = (await c.get(f"{base}/sync?since={max(0, news['cursor'] - news['overlapUs'])}", headers=A)).json()
        check({m["id"] for m in news["messages"]} <= {m["id"] for m in overlap["messages"]},
              "a caught-up device re-asks a few seconds back (saves can finish out of order)")

        print("\nSending without doubling up")
        before = len(sends)
        body = {"text": "Yes, Rs 950.", "client_msg_id": "phone-msg-1"}
        r1 = (await c.post(f"{base}/inbox/chats/{NIMAL}/messages", headers=A, json=body)).json()
        r2 = (await c.post(f"{base}/inbox/chats/{NIMAL}/messages", headers=A, json=body)).json()
        check(r1["ok"] and r1["message_id"] == r2["message_id"] and len(sends) == before + 1,
              "the same send repeated (lost connection) reaches the customer once")
        synced = (await c.get(f"{base}/sync?since={news['cursor']}", headers=A)).json()["messages"]
        check(any(m["clientMsgId"] == "phone-msg-1" and m["sender"] == "agent" for m in synced),
              "and the phone recognises its own message when it syncs")

        print("\nMessages a phone sent by itself")
        rec = {"text": "[Image] New stock", "type": "image", "client_msg_id": "phone-img-1", "status": "SENDING"}
        x1 = (await c.post(f"{base}/inbox/chats/{NIMAL}/record", headers=A, json=rec)).json()
        x2 = (await c.post(f"{base}/inbox/chats/{NIMAL}/record", headers=A,
                           json={**rec, "status": "SENT", "wa_message_id": "wamid.img1"})).json()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ChatHistory).where(ChatHistory.client_msg_id == "phone-img-1"))).scalars().all()
        check(x1["message_id"] == x2["message_id"] and len(rows) == 1 and rows[0].status == "SENT"
              and rows[0].wa_message_id == "wamid.img1", "a photo sent from the phone is saved once, then updated as WhatsApp accepts it")
        old = (await c.post(f"{base}/inbox/chats/{NIMAL}/record", headers=A,
                            json={"text": "sent offline yesterday", "client_msg_id": "phone-old",
                                  "sent_at": int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)})).json()
        msgs = (await c.get(f"{base}/inbox/chats/{NIMAL}/messages", headers=A)).json()["messages"]
        when = next(m["timestamp"] for m in msgs if m["id"] == str(old["message_id"]))
        check(abs(when / 1000 - (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()) < 5,
              "a message sent while offline keeps the time it was really sent")

        print("\nDelivery and read ticks from WhatsApp")
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(ChatHistory).where(ChatHistory.client_msg_id == "phone-msg-1"))).scalar_one()
            row.wa_message_id = "wamid.reply1"
            await db.commit()
            reply_id = row.id
        cur = (await c.get(f"{base}/sync?since=0", headers=A)).json()["cursor"]
        await c.post("/webhook", json=receipts(("wamid.reply1", "delivered"), ("wamid.reply1", "read")))
        await c.post("/webhook", json=receipts(("wamid.reply1", "delivered")))
        async with AsyncSessionLocal() as db:
            check((await db.get(ChatHistory, reply_id)).status == "READ", "delivered, then read; a late 'delivered' doesn't undo 'read'")
        ticked = (await c.get(f"{base}/sync?since={cur}", headers=A)).json()["messages"]
        check(any(m["id"] == str(reply_id) and m["status"] == "READ" for m in ticked), "devices get the new tick through sync")
        await c.post("/webhook", json=receipts(("wamid.img1", "failed", [{"code": 131047, "title": "Re-engagement"}])))
        async with AsyncSessionLocal() as db:
            img = (await db.execute(select(ChatHistory).where(ChatHistory.wa_message_id == "wamid.img1"))).scalar_one()
        check(img.status == "FAILED" and "24-hour window" in img.error, "a delivery failure is shown, in plain words")

        print("\nKeeping only recent history")
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            for days_ago, sender in ((30, "customer"), (30, "bot"), (25, "customer"), (22, "agent"), (20, "customer")):
                db.add(ChatHistory(tenant_id=a_id, customer_phone_number=KASUN, role="user" if sender == "customer" else "assistant",
                                   content=f"{days_ago} days old", sender=sender, status="SENT",
                                   created_at=now - timedelta(days=days_ago)))
            db.add(ChatHistory(tenant_id=b_id, customer_phone_number=KASUN, role="user", content="10 days old",
                               sender="customer", created_at=now - timedelta(days=10)))
            tenant_b = await db.get(Tenant, b_id)
            tenant_b.history_days = 7
            await db.commit()
        async with AsyncSessionLocal() as db:
            deleted = await cleanup_old_messages(db)
        check(deleted == {a_id: 4, b_id: 1}, "messages past each business's period are deleted (21 days; 7 for another)")
        async with AsyncSessionLocal() as db:
            left = (await db.execute(select(ChatHistory.content).where(
                ChatHistory.tenant_id == a_id, ChatHistory.content.like("%days old")))).scalars().all()
            conv = (await db.execute(select(Conversation).where(
                Conversation.tenant_id == a_id, Conversation.customer_phone == KASUN))).scalar_one()
            stats = {s.day: (s.incoming, s.outgoing) for s in (await db.execute(
                select(DailyMessageStat).where(DailyMessageStat.tenant_id == a_id))).scalars().all()}
        check(left == ["20 days old"], "newer messages stay")
        check(conv is not None and conv.customer_name == "Kasun", "the contact stays")
        day30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        check(stats.get(day30) == (1, 1) and sum(i + o for i, o in stats.values()) == 4,
              "what was deleted is still counted, day by day")
        async with AsyncSessionLocal() as db:
            check(await cleanup_old_messages(db) == {}, "running the cleanup again changes nothing")
        series = {d["day"]: d for d in (await c.get(f"{base}/message-stats?days=40", headers=A)).json()["days"]}
        today = now.strftime("%Y-%m-%d")
        check(series[day30]["incoming"] == 1 and series[today]["incoming"] >= 4,
              "the activity chart covers cleaned-up days and today alike")
        check((await c.patch(f"{base}/inbox/settings", headers=A, json={"history_days": 10})).status_code == 400
              and (await c.patch(f"{base}/inbox/settings", headers=A, json={"history_days": 30})).json()["history_days"] == 30,
              "each business picks how long to keep messages, from sensible choices")

        print("\nSeparation between businesses")
        for method, url, body in (("GET", f"{base}/sync", None), ("GET", f"{base}/contacts", None),
                                  ("PATCH", f"{base}/contacts/{NIMAL}", {"saved_name": "x"}),
                                  ("POST", f"{base}/inbox/chats/{NIMAL}/record", {"client_msg_id": "z", "text": "x"}),
                                  ("PATCH", f"{base}/inbox/settings", {"history_days": 7}),
                                  ("GET", f"{base}/message-stats", None)):
            r = await c.request(method, url, headers=B, json=body)
            check(r.status_code == 404, f"another business gets 'not found' for {method} {url.replace(base, '')}")
        other = (await c.get(f"/api/tenants/{b_id}/sync?since=0", headers=B)).json()
        check(all(m["text"] != "Hi, do you have Ceylon tea?" for m in other["messages"]), "and syncs only its own messages")


async def test_upgrade_existing_database():
    """A database from before this change: new columns are added and filled in on startup."""
    print("\nUpgrading an existing database")
    async with engine.begin() as conn:
        for index in ("ix_chat_history_changed_us", "ix_chat_history_client_msg_id", "ix_conversations_changed_us"):
            await conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
        for table, column in (("chat_history", "changed_us"), ("chat_history", "client_msg_id"),
                              ("conversations", "changed_us"), ("conversations", "message_count"),
                              ("conversations", "saved_name"), ("conversations", "notes"), ("tenants", "history_days")):
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    await init_db()
    async with AsyncSessionLocal() as db:
        unstamped = (await db.execute(select(func.count(ChatHistory.id)).where(ChatHistory.changed_us.is_(None)))).scalar()
        conv = (await db.execute(select(Conversation).where(Conversation.customer_phone == NIMAL))).scalars().first()
        real = (await db.execute(select(func.count(ChatHistory.id)).where(
            ChatHistory.tenant_id == conv.tenant_id, ChatHistory.customer_phone_number == NIMAL))).scalar()
        tenant = (await db.execute(select(Tenant))).scalars().first()
    check(unstamped == 0, "existing messages are stamped, so devices can sync them")
    check(conv.message_count == real and real > 0, "message counts are filled in from existing history")
    check(tenant.history_days == 21, "existing businesses keep 3 weeks of history by default")


async def main_async():
    await init_db()
    await test_all()
    await test_upgrade_existing_database()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
