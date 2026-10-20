"""
Checks for the self-service platform: sign-up, business owners locked to their own business,
connecting WhatsApp, and the platform owner's overview.

The most important part is isolation: one business owner must never see or change another
business's documents, prices, orders or customers. Every API route that takes a business,
product or order id is checked automatically, so a new endpoint that forgets the check fails here.

Runs offline against a throwaway database. Nothing is sent to WhatsApp or Meta.

    python test_platform_accounts.py
"""

import asyncio
import inspect
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "platform_test.db").replace("\\", "/")

import httpx  # noqa: E402

import llm_router  # noqa: E402
import main  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from config import settings  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, Order, Product, Tenant  # noqa: E402
from whatsapp_client import WhatsAppCredentialsError  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


async def fake_complete(role, messages, **kwargs):
    return llm_router.LLMResult(text="Happy to help! Which day suits you?", provider="test", model="fake", latency=0.0)


async def fake_complete_json(role, messages, **kwargs):
    return {}, llm_router.LLMResult(text="", provider="test", model="fake", latency=0.0)


llm_router.complete = fake_complete
llm_router.complete_json = fake_complete_json
llm_router.role_candidates = lambda role: [("test", "fake")]


# ---------------------------------------------------------------------------
# 1. Static sweep: every route that takes an id must check whose it is
# ---------------------------------------------------------------------------

CHECKS = ("_get_tenant_or_404(", "ensure_tenant_access(", "can_access(")
PLATFORM_ONLY = ("require_platform_admin", "require_owner")


def test_every_route_checks_ownership():
    print("\nEvery route that takes a business, product or order id checks ownership")
    unchecked = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not any(p in path for p in ("{tenant_id}", "{product_id}", "{order_id}")):
            continue
        source = inspect.getsource(route.endpoint)
        if any(c in source for c in CHECKS) or any(p in source for p in PLATFORM_ONLY):
            continue
        unchecked.append(f"{','.join(sorted(getattr(route, 'methods', None) or {'WS'}))} {path}")
    check(not unchecked, f"all id routes are guarded{' - UNGUARDED: ' + str(unchecked) if unchecked else ''}")


# ---------------------------------------------------------------------------
# 2. Sign-up and real cross-business attempts
# ---------------------------------------------------------------------------

async def platform_owner_token() -> str:
    async with AsyncSessionLocal() as db:
        user = AdminUser(username="platform", password_hash=hash_password("not-used"), role="owner")
        db.add(user)
        await db.flush()
        token = generate_session_token()
        db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
        await db.commit()
    return token


async def test_platform():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\nSign-up")
        check((await client.get("/api/auth/options")).json() == {"signup_open": True}, "sign-ups are open")
        res = await client.post("/api/signup", json={"full_name": "Nimal", "email": "Nimal@CityCare.lk",
                                                     "password": "clinic-pass-1", "business_name": "City Care"})
        check(res.status_code == 201 and res.json()["role"] == "client", "a business owner signs up and is signed in")
        a = res.json()
        A = {"Authorization": f"Bearer {a['token']}"}
        b = (await client.post("/api/signup", json={"email": "owner@spicegarden.lk", "password": "garden-pass-2",
                                                    "business_name": "Spice Garden"})).json()
        B = {"Authorization": f"Bearer {b['token']}"}
        a_id, b_id = a["tenant_id"], b["tenant_id"]

        check((await client.post("/api/signup", json={"email": "nimal@citycare.lk", "password": "another-pass",
                                                      "business_name": "Copy"})).status_code == 400,
              "the same email can't sign up twice (in any letter case)")
        check((await client.post("/api/signup", json={"email": "x@y.lk", "password": "short", "business_name": "X"})).status_code == 400,
              "a short password is refused")
        check((await client.post("/api/signup", json={"email": "not-an-email", "password": "long-enough-1",
                                                      "business_name": "X"})).status_code == 400, "a bad email is refused")
        res = await client.post("/api/admin/login", json={"username": "NIMAL@citycare.lk", "password": "clinic-pass-1"})
        check(res.status_code == 200 and res.json()["tenant_id"] == a_id, "signing in with the email works in any letter case")
        me = (await client.get("/api/admin/me", headers=A)).json()
        check(me["role"] == "client" and me["tenant_id"] == a_id and me["full_name"] == "Nimal", "the account knows its business")

        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, a_id)
            check(tenant.whatsapp_phone_number_id.startswith("test-") and "placeholder" in tenant.meta_access_token
                  and tenant.industry_source == "auto", "the new business starts in test mode, type to be worked out")
            # Some of B's data for A to try to reach.
            product = Product(tenant_id=b_id, name="Kottu", price=1100, stock_quantity=5)
            order = Order(tenant_id=b_id, order_number="ORD-SECRET", customer_phone="94770000009", customer_name="B's customer",
                          product_name="Kottu", quantity=1, total_amount=1100)
            db.add_all([product, order])
            await db.commit()
            product_id, order_id = product.id, order.id

        print("\nA business owner sees only their own business")
        listed = (await client.get("/api/tenants", headers=A)).json()
        check([t["id"] for t in listed] == [a_id], "the business list shows only their own")

        attempts = [
            ("GET", f"/api/tenants/{b_id}", None), ("PUT", f"/api/tenants/{b_id}", {"system_prompt": "hacked"}),
            ("GET", f"/api/tenants/{b_id}/bot-profile", None), ("PATCH", f"/api/tenants/{b_id}/bot-profile", {"bot_name": "x"}),
            ("GET", f"/api/tenants/{b_id}/documents", None), ("POST", f"/api/tenants/{b_id}/preview", {"message": "hi"}),
            ("GET", f"/api/tenants/{b_id}/gaps", None), ("POST", f"/api/tenants/{b_id}/teach", {"question": "q?", "answer": "a"}),
            ("POST", f"/api/tenants/{b_id}/self-test", None), ("GET", f"/api/tenants/{b_id}/understanding", None),
            ("GET", f"/api/tenants/{b_id}/whatsapp", None),
            ("PUT", f"/api/tenants/{b_id}/whatsapp", {"phone_number_id": "123456789", "access_token": "EAAx"}),
            ("GET", f"/api/tenants/{b_id}/products", None),
            ("POST", f"/api/tenants/{b_id}/products", {"name": "x", "price": 1, "stock_quantity": 1}),
            ("PUT", f"/api/products/{product_id}", {"price": 1}), ("DELETE", f"/api/products/{product_id}", None),
            ("GET", f"/api/tenants/{b_id}/orders", None), ("GET", f"/api/orders/{order_id}", None),
            ("PUT", f"/api/orders/{order_id}/status", {"status": "cancelled"}), ("GET", f"/api/orders/{order_id}/invoice", None),
            ("GET", f"/api/tenants/{b_id}/analytics", None), ("GET", f"/api/tenants/{b_id}/customers", None),
            ("POST", f"/api/tenants/{b_id}/broadcast", {"message": "spam"}),
            ("GET", f"/api/tenants/{b_id}/orders/export", None), ("GET", f"/api/tenants/{b_id}/customers/export", None),
        ]
        leaks = []
        for method, url, body in attempts:
            res = await client.request(method, url, headers=A, json=body)
            if res.status_code != 404 or "B's customer" in res.text or "Kottu" in res.text:
                leaks.append(f"{method} {url} -> {res.status_code}")
        check(not leaks, f"all {len(attempts)} attempts on another business answer 'not found'"
              f"{' - LEAKS: ' + str(leaks) if leaks else ''}")
        async with AsyncSessionLocal() as db:
            check((await db.get(Product, product_id)).price == 1100 and (await db.get(Order, order_id)).status == "pending_payment"
                  and (await db.get(Tenant, b_id)).system_prompt != "hacked", "and nothing of theirs was changed")

        for method, url in (("POST", "/api/tenants"), ("GET", "/api/admin/users"), ("GET", "/api/platform/businesses"),
                            ("DELETE", f"/api/tenants/{a_id}")):
            res = await client.request(method, url, headers=A, json={"business_name": "x", "whatsapp_phone_number_id": "1",
                                                                     "meta_access_token": "x"} if method == "POST" else None)
            check(res.status_code == 403, f"a business owner can't use platform-only {method} {url}")

        print("\nA business owner runs their own business")
        res = await client.post(f"/api/tenants/{a_id}/products", headers=A, json={"name": "Consultation", "price": 2500, "stock_quantity": 99})
        check(res.status_code == 201, "adds products")
        res = await client.patch(f"/api/tenants/{a_id}/bot-profile", headers=A, json={"bot_name": "Nila", "industry": "clinic"})
        check(res.status_code == 200 and (await client.get(f"/api/tenants/{a_id}/bot-profile", headers=A)).json()["bot_name"] == "Nila",
              "edits the bot profile")
        res = await client.post(f"/api/tenants/{a_id}/teach", headers=A, json={"question": "Do you have parking?", "answer": "Yes, free."})
        check(res.status_code == 200, "teaches answers")
        res = await client.post(f"/api/tenants/{a_id}/preview", headers=A, json={"message": "Do you have parking?"})
        check(res.status_code == 200 and res.json()["reply"], "tests the bot")

        print("\nConnecting WhatsApp")
        wa = (await client.get(f"/api/tenants/{a_id}/whatsapp", headers=A)).json()
        check(wa["connected"] is False and wa["webhook_url"].endswith("/webhook"), "starts not connected, with the webhook address to give Meta")

        async def meta_says_no(phone_id, token):
            raise WhatsAppCredentialsError("Meta did not accept these details: Invalid OAuth access token.")

        async def meta_says_yes(phone_id, token):
            return {"display_phone_number": "+94 11 222 3344", "verified_name": "City Care"}

        main.verify_whatsapp_number = meta_says_no
        res = await client.put(f"/api/tenants/{a_id}/whatsapp", headers=A, json={"phone_number_id": "109876543210987", "access_token": "EAAbad"})
        check(res.status_code == 400 and "Invalid OAuth" in res.json()["detail"], "wrong details are refused with Meta's reason")
        res = await client.put(f"/api/tenants/{a_id}/whatsapp", headers=A, json={"phone_number_id": "abc", "access_token": "EAAx"})
        check(res.status_code == 400, "a Phone Number ID that isn't digits is refused before asking Meta")

        main.verify_whatsapp_number = meta_says_yes
        res = await client.put(f"/api/tenants/{a_id}/whatsapp", headers=A, json={"phone_number_id": "109876543210987", "access_token": "EAAgood-token"})
        body = res.json()
        check(res.status_code == 200 and body["connected"] and body["display_phone_number"] == "+94 11 222 3344",
              "correct details connect the number")
        check("EAAgood-token" not in res.text and "EAAgood-token" not in (await client.get(f"/api/tenants/{a_id}/whatsapp", headers=A)).text,
              "the access token is never sent back to the browser")
        res = await client.put(f"/api/tenants/{b_id}/whatsapp", headers=B, json={"phone_number_id": "109876543210987", "access_token": "EAAgood"})
        check(res.status_code == 400 and "another business" in res.json()["detail"], "one number can't be connected to two businesses")

        print("\nThe platform owner")
        P = {"Authorization": f"Bearer {await platform_owner_token()}"}
        rows = {r["id"]: r for r in (await client.get("/api/platform/businesses", headers=P)).json()["businesses"]}
        check(rows[a_id]["owners"] == ["nimal@citycare.lk"] and rows[a_id]["whatsapp_connected"]
              and not rows[b_id]["whatsapp_connected"] and rows[a_id]["industry"] == "Clinics, dental & healthcare",
              "sees every business: owner, industry, whether it's live")
        check((await client.get(f"/api/tenants/{b_id}/products", headers=P)).status_code == 200, "can open any business")
        res = await client.delete(f"/api/tenants/{b_id}", headers=P)
        res = await client.post("/api/admin/login", json={"username": "owner@spicegarden.lk", "password": "garden-pass-2"})
        check(res.status_code == 401, "deleting a business also removes its owner's account")

        res = await client.post("/api/signup", json={"email": "sixth@x.lk", "password": "long-enough-1", "business_name": "Sixth"})
        check(res.status_code == 429, "sign-up attempts are rate limited (5 per 10 minutes from one address)")

        # Called directly: the rate limiter above would answer first over HTTP.
        from fastapi import HTTPException
        from schemas import SignupRequest
        settings.ALLOW_SIGNUP = False
        try:
            async with AsyncSessionLocal() as db:
                await main.signup(SignupRequest(email="late@x.lk", password="long-enough-1", business_name="Late"), db)
            check(False, "sign-ups can be closed with ALLOW_SIGNUP=false")
        except HTTPException as e:
            check(e.status_code == 403 and (await client.get("/api/auth/options")).json()["signup_open"] is False,
                  "sign-ups can be closed with ALLOW_SIGNUP=false")
        finally:
            settings.ALLOW_SIGNUP = True


class FakeSocket:
    """Stands in for a browser's live-orders connection. (Starlette's TestClient can hang on
    Windows when a websocket using an aiosqlite session closes, so the handler is called directly.)"""

    def __init__(self):
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_code = code

    async def receive_text(self):
        from starlette.websockets import WebSocketDisconnect
        raise WebSocketDisconnect(1000)      # the browser leaves straight away


async def test_websocket_isolation():
    print("\nLive order updates")

    async def make():
        async with AsyncSessionLocal() as db:
            t1 = Tenant(business_name="W1", whatsapp_phone_number_id="test-w1", meta_access_token="EAA_placeholder_token",
                        system_prompt="x", knowledge_base="x")
            t2 = Tenant(business_name="W2", whatsapp_phone_number_id="test-w2", meta_access_token="EAA_placeholder_token",
                        system_prompt="x", knowledge_base="x")
            db.add_all([t1, t2])
            await db.flush()
            user = AdminUser(username="w1@x.lk", password_hash=hash_password("x" * 8), role="client", tenant_id=t1.id)
            db.add(user)
            await db.flush()
            token = generate_session_token()
            db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
            await db.commit()
            return token, t1.id, t2.id

    token, own, other = await make()
    async with AsyncSessionLocal() as db:
        theirs = FakeSocket()
        await main.orders_websocket(theirs, tenant_id=other, token=token, db=db)
        check(theirs.closed_code == 4404 and not theirs.accepted, "an owner can't watch another business's live orders")
        mine = FakeSocket()
        await main.orders_websocket(mine, tenant_id=own, token=token, db=db)
        check(mine.accepted and mine.closed_code is None, "...but can watch their own")


async def main_async():
    await init_db()
    test_every_route_checks_ownership()
    await test_platform()
    await test_websocket_isolation()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
