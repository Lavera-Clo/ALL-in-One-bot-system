"""
Checks for the live inbox (inbox_service.py): every message recorded, the bot switched off per
chat or for the whole business, replies typed by a person, hand-overs, and business isolation.

Runs offline against a throwaway database: the AI is replaced by a scripted reply, WhatsApp
sends go through the placeholder-token mock (or a faked Meta error), whatsapp_bot.db is never opened.

    python test_inbox.py
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "inbox_test.db").replace("\\", "/")
os.environ["CORS_ORIGINS"] = "https://sender.example"        # read when main is imported

import httpx  # noqa: E402

import ai_service  # noqa: E402
import main  # noqa: E402
import whatsapp_client  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, ChatHistory, Conversation, Product, Tenant  # noqa: E402
from realtime import inbox_manager  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
CUSTOMER, OTHER, MANAGER = "94770000001", "94770000002", "94779998877"


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


seen_histories = []


async def scripted_reply(system_prompt, knowledge_base, chat_history, user_message, config):
    seen_histories.append((user_message, [m["content"] for m in chat_history]))
    if "discount" in user_message.lower():
        return ai_service.BotReply("Let me pass you to our manager.", handoff=True)
    return ai_service.BotReply(f"Bot answer to: {user_message}")


main.generate_bot_reply = scripted_reply


def webhook(phone, message, msg_id, phone_id="1111", name="Nimal Perera"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id},
        "contacts": [{"wa_id": phone, "profile": {"name": name}}],
        "messages": [{"from": phone, "id": msg_id, **message}]}}]}]}


def text(body):
    return {"type": "text", "text": {"body": body}}


async def setup():
    async with AsyncSessionLocal() as db:
        a = Tenant(business_name="City Care", whatsapp_phone_number_id="1111", meta_access_token="EAA_placeholder_token",
                   system_prompt="Be kind.", knowledge_base="Open 8 to 9.", human_handoff_number="+" + MANAGER)
        b = Tenant(business_name="Other Shop", whatsapp_phone_number_id="2222", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="x")
        db.add_all([a, b])
        await db.flush()
        db.add(Product(tenant_id=a.id, name="Vitamin C", price=500, stock_quantity=10))
        tokens = {}
        for key, tenant_id in (("a", a.id), ("b", b.id)):
            user = AdminUser(username=f"{key}@x.lk", password_hash=hash_password("not-used"), role="client", tenant_id=tenant_id)
            db.add(user)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=user.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        await db.commit()
        return a.id, b.id, tokens


async def history(tenant_id, phone):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ChatHistory).where(ChatHistory.tenant_id == tenant_id,
                ChatHistory.customer_phone_number == phone).order_by(ChatHistory.id))).scalars().all()
        return [(r.sender, r.content, r.status) for r in rows]


async def test_all():
    a_id, b_id, tokens = await setup()
    transport = httpx.ASGITransport(app=main.app)
    A = {"Authorization": f"Bearer {tokens['a']}"}
    B = {"Authorization": f"Bearer {tokens['b']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\nEvery message is recorded")
        await client.post("/webhook", json=webhook(CUSTOMER, text("What time do you open?"), "w1"))
        rows = await history(a_id, CUSTOMER)
        check(rows[0] == ("customer", "What time do you open?", "RECEIVED"), "the customer's message is saved")
        check(rows[1] == ("bot", "Bot answer to: What time do you open?", "SENT"), "the bot's reply is saved, as the bot's")
        async with AsyncSessionLocal() as db:
            conv = (await db.execute(select(Conversation).where(Conversation.customer_phone == CUSTOMER))).scalar_one()
            check(conv.customer_name == "Nimal Perera", "the customer's WhatsApp name is kept")

        await client.post("/webhook", json=webhook(CUSTOMER, text("and on Sunday?"), "w2"))
        check(seen_histories[-1][0] == "and on Sunday?" and "and on Sunday?" not in seen_histories[-1][1]
              and "What time do you open?" in seen_histories[-1][1],
              "the AI sees earlier messages, and the one it's answering only once")

        await client.post("/webhook", json=webhook(CUSTOMER, text("cart"), "w3"))
        rows = await history(a_id, CUSTOMER)
        check(rows[-2][1] == "cart" and rows[-1][0] == "bot" and "cart" in rows[-1][1].lower(),
              "command replies (the cart) are saved too, not just AI answers")

        await client.post("/webhook", json=webhook(CUSTOMER, {"type": "image", "image": {"id": "img1", "caption": "this one?"}}, "w4"))
        rows = await history(a_id, CUSTOMER)
        check(any(r == ("customer", "[Image] this one?", "RECEIVED") for r in rows), "photos appear as '[Image] caption'")

        chats = (await client.get(f"/api/tenants/{a_id}/inbox/chats", headers=A)).json()
        first = chats["chats"][0]
        check(first["phone"] == CUSTOMER and first["name"] == "Nimal Perera" and first["botEnabled"] and first["botActive"],
              "the chat list shows the customer, last message and that the bot is on")

        print("\nBot off for one chat")
        before = len(await history(a_id, CUSTOMER))
        res = await client.patch(f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}", headers=A, json={"bot_enabled": False})
        check(res.status_code == 200 and res.json()["bot_enabled"] is False, "the bot is switched off for this chat")
        await client.post("/webhook", json=webhook(CUSTOMER, text("Is the doctor in today?"), "w5"))
        rows = await history(a_id, CUSTOMER)
        check(len(rows) == before + 1 and rows[-1] == ("customer", "Is the doctor in today?", "RECEIVED"),
              "a message arriving now is saved, and the bot sends nothing")
        await client.post("/webhook", json=webhook(OTHER, text("hello there friend"), "w6", name="Kasun"))
        check((await history(a_id, OTHER))[-1][0] == "bot", "other customers still get the bot")

        print("\nA person replies")
        res = await client.post(f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", headers=A, json={"text": "Yes, from 4 PM."})
        body = res.json()
        rows = await history(a_id, CUSTOMER)
        check(res.status_code == 200 and body["ok"] and rows[-1] == ("agent", "Yes, from 4 PM.", "SENT"),
              "the reply is sent and saved as the person's, not the bot's")
        msgs = (await client.get(f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", headers=A)).json()["messages"]
        check(msgs[-1]["sender"] == "agent" and msgs[-1]["isOutgoing"] and msgs[-1]["id"] == str(body["message_id"]),
              "the chat shows it, with its id")
        check((await client.post(f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", headers=A, json={"text": "  "})).status_code == 400,
              "an empty reply is refused")

        print("\nWhatsApp refuses a reply (24-hour window), then a retry")
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, a_id)
            tenant.meta_access_token = "EAAreal-looking-token"
            await db.commit()
        real_post = httpx.AsyncClient.post
        meta_error = {"error": {"code": 131047, "message": "Re-engagement message"}}

        async def meta_refuses(self, url, *args, **kwargs):
            if "graph.facebook.com" in str(url):
                req = httpx.Request("POST", url)
                raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, json=meta_error, request=req))
            return await real_post(self, url, *args, **kwargs)

        async def meta_accepts(self, url, *args, **kwargs):
            if "graph.facebook.com" in str(url):
                req = httpx.Request("POST", url)
                return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]}, request=req)
            return await real_post(self, url, *args, **kwargs)

        httpx.AsyncClient.post = meta_refuses
        try:
            res = await client.post(f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", headers=A, json={"text": "Back in stock!"})
        finally:
            httpx.AsyncClient.post = real_post
        body = res.json()
        check(res.status_code == 200 and body["ok"] is False and "24-hour window" in body["error"],
              "the refusal comes back in plain words")
        rows = await history(a_id, CUSTOMER)
        check(rows[-1][0] == "agent" and rows[-1][2] == "FAILED", "the message is kept, marked failed")

        httpx.AsyncClient.post = meta_accepts
        try:
            res = await client.post(f"/api/tenants/{a_id}/inbox/messages/{body['message_id']}/retry", headers=A)
        finally:
            httpx.AsyncClient.post = real_post
        rows_after = await history(a_id, CUSTOMER)
        check(res.json()["ok"] and len(rows_after) == len(rows) and rows_after[-1][2] == "SENT",
              "retrying updates the same message instead of adding another")
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, a_id)
            tenant.meta_access_token = "EAA_placeholder_token"
            await db.commit()

        print("\nHand-over")
        await client.patch(f"/api/tenants/{a_id}/inbox/chats/{OTHER}", headers=A, json={"bot_enabled": True})
        await client.post("/webhook", json=webhook(OTHER, text("Can I get a discount please?"), "w7", name="Kasun"))
        chats = {c["phone"]: c for c in (await client.get(f"/api/tenants/{a_id}/inbox/chats", headers=A)).json()["chats"]}
        check(chats[OTHER]["needsHuman"] and chats[OTHER]["label"] == "HANDOFF", "a hand-over flags the chat 'needs you'")
        async with AsyncSessionLocal() as db:
            manager_rows = (await db.execute(select(func.count(ChatHistory.id)).where(
                ChatHistory.customer_phone_number == MANAGER))).scalar()
        check(manager_rows == 0, "the alert to the manager isn't mixed into anyone's chat")
        await client.post(f"/api/tenants/{a_id}/inbox/chats/{OTHER}/messages", headers=A, json={"text": "Hi Kasun, I can help."})
        chats = {c["phone"]: c for c in (await client.get(f"/api/tenants/{a_id}/inbox/chats", headers=A)).json()["chats"]}
        check(not chats[OTHER]["needsHuman"], "a person's reply clears the flag")

        print("\nMaster switch")
        res = await client.patch(f"/api/tenants/{a_id}/bot", headers=A, json={"enabled": False})
        check(res.json()["bot_enabled"] is False, "the bot is switched off for the whole business")
        before = len(await history(a_id, OTHER))
        await client.post("/webhook", json=webhook(OTHER, text("anyone there?"), "w8", name="Kasun"))
        check(len(await history(a_id, OTHER)) == before + 1, "nobody gets an automatic reply")
        to_manager = []

        async def watch_manager(pid, to, body, *rest):
            if str(to).endswith(MANAGER):
                to_manager.append(body)

        whatsapp_client.add_send_listener(watch_manager)
        await client.post("/webhook", json=webhook(MANAGER, text("CONFIRM ORD-NOPE00"), "w9", name="Manager"))
        check(any("Could not find" in b for b in to_manager), "the manager's order commands still work while the bot is off")
        profile = (await client.get(f"/api/tenants/{a_id}/bot-profile", headers=A)).json()
        check(profile["bot_enabled"] is False, "the bot profile reports the switch")
        await client.patch(f"/api/tenants/{a_id}/bot", headers=A, json={"enabled": True})

        print("\nLive updates")

        class Sock:
            def __init__(self):
                self.sent, self.closed, self.accepted = [], None, False

            async def send_json(self, m):
                self.sent.append(m)

            async def accept(self):
                self.accepted = True

            async def close(self, code=1000):
                self.closed = code

            async def receive_text(self):
                from starlette.websockets import WebSocketDisconnect
                raise WebSocketDisconnect(1000)

        sock = Sock()
        inbox_manager.register(a_id, sock)
        await client.post("/webhook", json=webhook(CUSTOMER, text("live?"), "w10"))
        inbox_manager.disconnect(a_id, sock)
        check(any(m["type"] == "chats.changed" and m["phone"] == CUSTOMER and m["incoming"] for m in sock.sent),
              "an open inbox hears about a new message at once")
        theirs = Sock()
        await main.inbox_websocket(theirs, tenant_id=a_id, token=tokens["b"])
        check(theirs.closed == 4404 and not theirs.accepted, "another business can't listen to this inbox")

        print("\nSeparation between businesses")
        for method, url, body in (("GET", f"/api/tenants/{a_id}/inbox/chats", None),
                                  ("GET", f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", None),
                                  ("POST", f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}/messages", {"text": "hi"}),
                                  ("PATCH", f"/api/tenants/{a_id}/inbox/chats/{CUSTOMER}", {"bot_enabled": True}),
                                  ("PATCH", f"/api/tenants/{a_id}/bot", {"enabled": False})):
            res = await client.request(method, url, headers=B, json=body)
            check(res.status_code == 404, f"another business gets 'not found' for {method} {url.replace(f'/api/tenants/{a_id}', '')}")

        print("\nManual orders")
        async with AsyncSessionLocal() as db:
            product = (await db.execute(select(Product).where(Product.tenant_id == a_id))).scalar_one()
        res = await client.post(f"/api/tenants/{a_id}/orders", headers=A,
                                json={"customer_phone": "+94 77 123 4567", "items": [{"product_id": product.id, "quantity": 3}]})
        check(res.status_code == 201 and res.json()["total_amount"] == 1500 and res.json()["customer_phone"] == "94771234567",
              "an order entered by hand is created with its total")
        async with AsyncSessionLocal() as db:
            check((await db.get(Product, product.id)).stock_quantity == 7, "and takes its stock")
        res = await client.post(f"/api/tenants/{a_id}/orders", headers=A, json={"items": [{"product_id": product.id, "quantity": 99}]})
        check(res.status_code == 400 and "in stock" in res.json()["detail"], "more than is in stock is refused")

        print("\nAnother website may call the API only when listed")
        res = await client.options(f"/api/tenants/{a_id}/inbox/chats", headers={
            "Origin": "https://sender.example", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"})
        check(res.headers.get("access-control-allow-origin") == "https://sender.example", "the listed website is allowed")
        res = await client.options(f"/api/tenants/{a_id}/inbox/chats", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        check(res.headers.get("access-control-allow-origin") is None, "an unlisted website is not")


async def main_async():
    await init_db()
    await test_all()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
