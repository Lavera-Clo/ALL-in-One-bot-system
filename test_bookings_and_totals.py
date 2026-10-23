"""
Checks for exact order totals (order_math.py), bookings collected from chats (booking_service.py)
and the rule that only the manager's own number can confirm things.

Offline: every AI call is scripted; the database is a throwaway (SQLite, or PostgreSQL through
run_tests_postgres.py). WhatsApp sends are recorded, never delivered.

    python test_bookings_and_totals.py
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "booking_test.db").replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import booking_service  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
import order_math as om  # noqa: E402
import whatsapp_client  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, Booking, ChatHistory, Conversation, Order, Tenant  # noqa: E402
from sqlalchemy import select  # noqa: E402
from tenant_config import BotConfig  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
MANAGER, CUSTOMER, OTHER = "94779998877", "94771110000", "94772220000"


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


CATALOG = [om.CatalogItem("Ceylon Black Tea 200g", 950, 42), om.CatalogItem("Green Tea 100g", 780, 30),
           om.CatalogItem("Cinnamon Sticks 100g", 650, 18), om.CatalogItem("Cardamom 50g", 1200, 6),
           om.CatalogItem("Gift box (tea + cinnamon)", 2400, 9)]


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

async def test_totals():
    print("\nWho wants a total")
    for text in ["so total how much with delivery to Kandy?", "how much would it be?", "mulu mudala kiyada", "ඔක්කොම කීයද", "in total?"]:
        check(om.asks_total(text), f"asks for a total: {text}")
    for text in ["ok thanks", "do you have cardamom", "I want 3 green tea"]:
        check(not om.asks_total(text), f"does not: {text}")

    print("\nWhich products")
    check(om.fallback_lines("I want 3 green tea and 2 cinnamon", CATALOG) == {"Green Tea 100g": 3, "Cinnamon Sticks 100g": 2},
          "'3 green tea and 2 cinnamon' -> the right two products")
    check(om.fallback_lines("two black tea, 1 gift box", CATALOG) == {"Ceylon Black Tea 200g": 2, "Gift box (tea + cinnamon)": 1},
          "quantities in words, and a bundle whose name mentions other products")
    check(om.fallback_lines("3 tea", CATALOG) == {}, "'3 tea' fits green, black and the gift box: never guessed")
    check(om.fallback_lines("2 ceylon black tea 200g", CATALOG) == {"Ceylon Black Tea 200g": 2}, "a full product name is exact")

    print("\nThe arithmetic")
    quote = om.compute_quote({"Green Tea 100g": 3, "Cinnamon Sticks 100g": 2, "Cardamom 50g": 10}, CATALOG, "LKR")
    check(str(quote.subtotal) == "10840", "3 x 780 + 2 x 650 + 6 x 1,200 = 10,840 (to the rupee)")
    check(quote.has_shortage and quote.lines[2].quantity == 6 and quote.lines[2].short_by == 4,
          "10 cardamom wanted, 6 in stock: the total uses 6 and says so")
    check("(they wanted 10, only 6 in stock)" in om.quote_text(quote) and "Items total: LKR 10,840" in om.quote_text(quote),
          "the quote text shows the lines, the shortage and the items total")
    cents = om.compute_quote({"X": 3}, [om.CatalogItem("X", 19.99, 10)], "")
    check(str(cents.subtotal) == "59.97" and om.money(cents.subtotal) == "59.97", "prices with cents add up exactly (no float drift)")
    check(om.compute_quote({"Nope": 2}, CATALOG) is None, "a product that isn't in the catalogue gives no quote")

    print("\nWhat counts when the customer asks")
    real = (llm_router.complete_json, llm_router.role_candidates)
    asked = []

    async def fake_json(role, messages, **kw):
        text = messages[1]["content"].split("What the customer wrote, oldest first:\n")[-1]
        asked.append(text)
        lines = om.fallback_lines(text, CATALOG)
        return ({"items": [{"product": n, "quantity": q} for n, q in lines.items()]},
                llm_router.LLMResult(text="", provider="fake", model="m", latency=0.1))

    llm_router.complete_json, llm_router.role_candidates = fake_json, lambda role: [("fake", "m")]
    try:
        history = [{"role": "user", "content": "I want 3 green tea and 2 cinnamon"}, {"role": "assistant", "content": "Lovely."}]
        q = await om.build_quote(history, "so total how much?", CATALOG, "LKR")
        check(str(q.subtotal) == "3640", "a bare 'total?' adds up what they asked for earlier")
        q = await om.build_quote(history, "ok then 2 black tea and 1 gift box, total for those?", CATALOG, "LKR")
        check(str(q.subtotal) == "4300", "'for those' adds up only the items in that message")
        q = await om.build_quote(history, "also 2 black tea, total?", CATALOG, "LKR")
        check(str(q.subtotal) == "5540", "'also ...' adds to the earlier order (3,640 + 1,900)")
        check(await om.build_quote(history, "how much is the green tea", CATALOG) is None, "a price question is not a total")
        check(await om.build_quote(history, "total?", []) is None, "no product list: no quote")
    finally:
        llm_router.complete_json, llm_router.role_candidates = real

    print("\nThe reply uses the verified figures")
    seen = []

    async def fake_complete(role, messages, **kw):
        seen.append(messages[0]["content"])
        return SimpleNamespace(text="<thinking>x</thinking><reply>Your items come to *Rs 3,640*, and delivery to Kandy is Rs 450 on top.</reply>",
                               label="fake:m", latency=0.1)

    real2 = (llm_router.complete, llm_router.role_candidates, llm_router.complete_json)
    llm_router.complete, llm_router.role_candidates, llm_router.complete_json = fake_complete, lambda role: [("fake", "m")], fake_json
    try:
        config = BotConfig(tenant_id=1, business_name="Lanka Leaf", currency="LKR")
        config.catalog = CATALOG
        kb = "Delivery inside Colombo Rs 350, elsewhere Rs 450, free over Rs 5,000."
        reply = await ai_service.generate_bot_reply("Be kind.", kb, history, "so total how much with delivery to Kandy?", config)
        check("worked out by the system from the live product list" in seen[0] and "Items total: LKR 3,640" in seen[0],
              "the prompt carries the calculated total, marked as verified")
        check(len(seen) == 1 and "3,640" in reply.text, "a reply quoting the verified total is not rejected as an invented number")
        seen.clear()
        await ai_service.generate_bot_reply("Be kind.", kb, history, "do you deliver to Kandy", config)
        check("worked out by the system" not in seen[0] and "Items total:" not in seen[0], "no calculation block when no total was asked for")
    finally:
        llm_router.complete, llm_router.role_candidates, llm_router.complete_json = real2


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

sent = []                       # (recipient, text) for every WhatsApp send


async def record_send(pid, to, body, kind, ok, error, wa_id):
    sent.append((str(to), body))


whatsapp_client.add_send_listener(record_send)


def hook(phone, body, msg_id, name="Test", phone_id="6060"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id}, "contacts": [{"wa_id": phone, "profile": {"name": name}}],
        "messages": [{"from": phone, "id": msg_id, "type": "text", "text": {"body": body}}]}}]}]}


async def add_chat(tenant_id, phone, lines):
    async with AsyncSessionLocal() as db:
        for sender, text in lines:
            db.add(ChatHistory(tenant_id=tenant_id, customer_phone_number=phone, role="user" if sender == "customer" else "assistant",
                               content=text, sender=sender, status="SENT"))
        await db.commit()


async def test_bookings():
    print("\nSetting up a clinic")
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="City Care", whatsapp_phone_number_id="6060", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="Open 8 to 8.", human_handoff_number="+" + MANAGER)
        other = Tenant(business_name="Other", whatsapp_phone_number_id="7070", meta_access_token="EAA_placeholder_token",
                       system_prompt="x", knowledge_base="x")
        db.add_all([t, other])
        await db.flush()
        db.add(Conversation(tenant_id=t.id, customer_phone=CUSTOMER, customer_name="Sampath Perera"))
        tokens = {}
        for key, tid in (("a", t.id), ("b", other.id)):
            u = AdminUser(username=f"{key}@book.test", password_hash=hash_password("x"), role="client", tenant_id=tid)
            db.add(u)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=u.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        db.add(Order(tenant_id=t.id, order_number="ORD-CHECK1", customer_phone=CUSTOMER, product_name="Tea", quantity=1,
                     total_amount=950, status="payment_uploaded"))
        await db.commit()
        tenant_id, other_id = t.id, other.id

    answers = []

    async def fake_json(role, messages, **kw):
        return (answers.pop(0), llm_router.LLMResult(text="", provider="fake", model="m", latency=0.1))

    real = (llm_router.complete_json, llm_router.role_candidates, booking_service.CAPTURE_DELAY_SECONDS)
    llm_router.complete_json, llm_router.role_candidates = fake_json, lambda role: [("fake", "m")]
    booking_service.CAPTURE_DELAY_SECONDS = 0
    try:
        print("\nCollecting a booking from the chat")
        await add_chat(tenant_id, CUSTOMER, [("customer", "I want to see a skin doctor"), ("bot", "Which day works best?")])
        answers.append({"booking": True, "complete": False, "service": "dermatologist", "when": "", "people": None})
        check(await booking_service.capture(tenant_id, CUSTOMER) is None, "no day yet: nothing is recorded")
        await add_chat(tenant_id, CUSTOMER, [("customer", "tomorrow evening is ok, my name is Sampath"), ("bot", "Thanks, noted.")])
        answers.append({"booking": True, "complete": True, "service": "Dermatologist consultation", "when": "tomorrow evening",
                        "people": None, "name": "Sampath Perera", "notes": "skin rash"})
        sent.clear()
        booking = await booking_service.capture(tenant_id, CUSTOMER)
        check(booking and booking.status == "pending" and booking.service == "Dermatologist consultation" and booking.when_text == "tomorrow evening",
              "service and day collected: saved as a pending booking")
        alert = [b for to, b in sent if to == MANAGER]
        check(len(alert) == 1 and f"CONFIRM BK-{booking.id}" in alert[0] and "tomorrow evening" in alert[0] and "Sampath Perera" in alert[0],
              "the manager is messaged the details and told how to answer")
        async with AsyncSessionLocal() as db:
            conv = (await db.execute(select(Conversation).where(Conversation.customer_phone == CUSTOMER))).scalar_one()
        check(conv.needs_human, "the chat is flagged 'Needs you' in the inbox")

        sent.clear()
        answers.append({"booking": True, "complete": True, "service": "Dermatologist consultation", "when": "tomorrow evening",
                        "people": None, "name": "Sampath Perera", "notes": ""})
        check(await booking_service.capture(tenant_id, CUSTOMER) is None and not sent, "the same request again: no duplicate, no second alert")
        answers.append({"booking": True, "complete": True, "service": "Dermatologist consultation", "when": "Friday 5 PM",
                        "people": None, "name": "Sampath Perera", "notes": ""})
        changed = await booking_service.capture(tenant_id, CUSTOMER)
        async with AsyncSessionLocal() as db:
            count = len((await db.execute(select(Booking).where(Booking.tenant_id == tenant_id))).scalars().all())
        check(changed.id == booking.id and changed.when_text == "Friday 5 PM" and count == 1 and "BOOKING UPDATED" in sent[0][1],
              "a change of day updates the same booking and tells the manager it changed")
        answers.append({"booking": False, "complete": False})
        await add_chat(tenant_id, OTHER, [("customer", "what are your opening hours? do you take bookings?")])
        check(await booking_service.capture(tenant_id, OTHER) is None, "a question about booking is not a booking")
        check(await booking_service.capture(tenant_id, "94773330000") is None, "a chat with no talk of booking costs no AI call")

        print("\nThe manager answers on WhatsApp")
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sent.clear()
            await client.post("/webhook", json=hook(CUSTOMER, f"CONFIRM BK-{booking.id}", "c1"))
            async with AsyncSessionLocal() as db:
                still = await db.get(Booking, booking.id)
            check(still.status == "pending", "a CUSTOMER typing 'CONFIRM BK-n' does not confirm anything")

            await client.post("/webhook", json=hook(MANAGER, f"confirm bk-{booking.id}", "m1", name="Manager"))
            async with AsyncSessionLocal() as db:
                done = await db.get(Booking, booking.id)
            to_customer = [b for to, b in sent if to == CUSTOMER]
            to_manager = [b for to, b in sent if to == MANAGER]
            check(done.status == "confirmed" and "Manager" in (done.handled_by or ""), "the manager's 'CONFIRM BK-n' confirms it")
            check(any("confirmed" in b and "Friday 5 PM" in b and "Dermatologist" in b for b in to_customer), "the customer is told, with the service and time")
            check(to_manager and "confirmed" in to_manager[-1].lower(), "and the manager gets an acknowledgement")
            sent.clear()
            await client.post("/webhook", json=hook(MANAGER, f"CONFIRM BK-{booking.id}", "m2", name="Manager"))
            check(any("already confirmed" in b for to, b in sent if to == MANAGER), "answering the same booking twice is refused politely")
            await client.post("/webhook", json=hook(MANAGER, "CONFIRM BK-9999", "m3", name="Manager"))
            check(any("couldn't find" in b for to, b in sent if to == MANAGER), "an unknown booking number is reported")

            print("\nThe portal and website")
            A = {"Authorization": f"Bearer {tokens['a']}"}
            B = {"Authorization": f"Bearer {tokens['b']}"}
            answers.append({"booking": True, "complete": True, "service": "Dental check-up", "when": "Monday 10 AM", "people": 1,
                            "name": "Sampath Perera", "notes": ""})
            await add_chat(tenant_id, CUSTOMER, [("customer", "can I also book a dental check-up Monday 10 AM")])
            second = await booking_service.capture(tenant_id, CUSTOMER)
            listing = (await client.get(f"/api/tenants/{tenant_id}/bookings", headers=A)).json()["bookings"]
            check(len(listing) == 2 and listing[0]["id"] == second.id and listing[0]["status"] == "pending",
                  "after one is answered, a new request becomes its own booking, listed newest first")
            check([b["id"] for b in (await client.get(f"/api/tenants/{tenant_id}/bookings?status=pending", headers=A)).json()["bookings"]] == [second.id],
                  "the list can show only what's waiting for an answer")
            sent.clear()
            res = await client.patch(f"/api/tenants/{tenant_id}/bookings/{second.id}", headers=A,
                                     json={"status": "declined", "message": "Sorry, Monday is fully booked. Tuesday 10 AM?"})
            check(res.status_code == 200 and res.json()["booking"]["status"] == "declined"
                  and (CUSTOMER, "Sorry, Monday is fully booked. Tuesday 10 AM?") in sent,
                  "declining from the portal sends the customer the team's own words")
            check((await client.patch(f"/api/tenants/{tenant_id}/bookings/{second.id}", headers=A, json={"status": "confirmed"})).status_code == 400,
                  "a booking that was already answered can't be answered again")
            check((await client.patch(f"/api/tenants/{tenant_id}/bookings/{booking.id}", headers=A, json={"status": "pending"})).status_code == 400,
                  "'pending' is not an answer")
            check((await client.get(f"/api/tenants/{tenant_id}/bookings", headers=B)).status_code == 404
                  and (await client.patch(f"/api/tenants/{tenant_id}/bookings/{second.id}", headers=B, json={"status": "confirmed"})).status_code == 404,
                  "another business can neither see nor answer these bookings")

            print("\nOnly the manager can confirm an order")
            sent.clear()
            await client.post("/webhook", json=hook(CUSTOMER, "CONFIRM ORD-CHECK1", "o1"))
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.order_number == "ORD-CHECK1"))).scalar_one()
            check(order.status == "payment_uploaded", "a customer texting 'CONFIRM ORD-...' can no longer confirm their own payment")
            await client.post("/webhook", json=hook(MANAGER, "CONFIRM ORD-CHECK1", "o2", name="Manager"))
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.order_number == "ORD-CHECK1"))).scalar_one()
            check(order.status == "confirmed" and "Manager" in (order.confirmed_by or ""), "the manager's own number still can")
    finally:
        llm_router.complete_json, llm_router.role_candidates, booking_service.CAPTURE_DELAY_SECONDS = real


async def scripted_reply(system_prompt, knowledge_base, chat_history, user_message, config, **_):
    return ai_service.BotReply("Noted.")


main.generate_bot_reply = scripted_reply


async def main_async():
    await init_db()
    await test_totals()
    await test_bookings()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
