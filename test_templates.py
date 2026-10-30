"""
Checks for WhatsApp template messages (template_service.py): reaching a customer more than 24
hours after they last wrote. Meta is scripted; the database is a throwaway.

    python test_templates.py
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "tpl_test.db").replace("\\", "/")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

import main  # noqa: E402
import template_service as ts  # noqa: E402
import whatsapp_client  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, ChatHistory, Tenant  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
CUSTOMER = "94771110000"


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


META_PAGES = {
    "first": {"data": [
        {"name": "order_ready", "language": "en_US", "category": "UTILITY", "status": "APPROVED",
         "components": [{"type": "HEADER", "format": "TEXT", "text": "Order {{1}}"},
                        {"type": "BODY", "text": "Hi {{1}}, your order {{2}} is ready to collect."},
                        {"type": "FOOTER", "text": "Lavera Clo"},
                        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Thanks"}]}]},
        {"name": "draft_one", "language": "en_US", "category": "MARKETING", "status": "PENDING",
         "components": [{"type": "BODY", "text": "Not approved yet"}]}],
        "paging": {"next": "https://graph.facebook.com/next-page"}},
    "second": {"data": [
        {"name": "promo_photo", "language": "si", "category": "MARKETING", "status": "APPROVED",
         "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "අලුත් තොගය ආවා {{name}}!"}]}]},
}
sent = []


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        page = META_PAGES["second" if "next-page" in url else "first"]
        return httpx.Response(200, json=page, request=httpx.Request("GET", url))

    async def post(self, url, json=None, headers=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"messages": [{"id": "wamid.tpl1"}]}, request=httpx.Request("POST", url))


async def main_async():
    await init_db()
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lavera Clo", whatsapp_phone_number_id="7171", meta_access_token="EAA_real_looking_token",
                   system_prompt="x", knowledge_base="x")
        mock = Tenant(business_name="Demo", whatsapp_phone_number_id="7272", meta_access_token="EAA_placeholder_token",
                      system_prompt="x", knowledge_base="x")
        db.add_all([t, mock])
        await db.flush()
        tokens = {}
        for key, tid in (("a", t.id), ("b", mock.id)):
            u = AdminUser(username=f"{key}@tpl.test", password_hash=hash_password("x"), role="client", tenant_id=tid)
            db.add(u)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=u.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        await db.commit()
        tid, mid = t.id, mock.id
    A = {"Authorization": f"Bearer {tokens['a']}"}
    B = {"Authorization": f"Bearer {tokens['b']}"}

    print("\nPieces")
    check(ts.placeholders("Hi {{1}}, order {{2}} - thanks {{1}}") == ["1", "2"], "blanks found in order, each once")
    check(ts.placeholders("Hi {{ name }}") == ["name"], "named blanks too")

    real_client = httpx.AsyncClient
    transport = httpx.ASGITransport(app=main.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Meta from here on (the test's own client above is already made).
            httpx.AsyncClient = FakeClient
            print("\nThe account ID")
            r = await client.get(f"/api/tenants/{tid}/templates", headers=A)
            check(r.status_code == 400 and "Business Account ID" in r.json()["detail"], "not known yet: a clear message what to do")
            hook = {"object": "whatsapp_business_account", "entry": [{"id": "555666777", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "7171"}, "statuses": [{"id": "wamid.x", "status": "read", "recipient_id": CUSTOMER}]}}]}]}
            await client.post("/webhook", json=hook)
            async with AsyncSessionLocal() as db:
                check((await db.get(Tenant, tid)).wa_business_account_id == "555666777",
                      "picked up by itself from the next webhook Meta sends")

            print("\nListing")
            r = await client.get(f"/api/tenants/{tid}/templates", headers=A)
            names = [x["name"] for x in r.json()["templates"]]
            check(names == ["order_ready", "promo_photo"], "approved templates only, from every page Meta returns")
            order = r.json()["templates"][0]
            check(order["header"] == "Order {{1}}" and order["header_params"] == ["1"] and order["body_params"] == ["1", "2"]
                  and order["footer"] == "Lavera Clo" and order["buttons"] == ["Thanks"], "texts, blanks, footer and buttons")
            check(r.json()["templates"][1]["header_media"] == "image", "an image header is marked (not sendable from here yet)")
            r = await client.get(f"/api/tenants/{tid}/templates", headers=B)
            check(r.status_code in (403, 404), "another business can't list them")

            print("\nSending")
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/template", headers=A,
                                  json={"name": "order_ready", "language": "en_US", "body": {"1": "Nimal", "2": "ORD-12"},
                                        "header": {"1": "ORD-12"}})
            check(r.status_code == 200 and r.json()["ok"], "sent")
            msg = sent[-1]["template"]
            check(sent[-1]["type"] == "template" and msg["name"] == "order_ready" and msg["language"] == {"code": "en_US"},
                  "as a template, by name and language")
            check(msg["components"] == [{"type": "header", "parameters": [{"type": "text", "text": "ORD-12"}]},
                                        {"type": "body", "parameters": [{"type": "text", "text": "Nimal"},
                                                                        {"type": "text", "text": "ORD-12"}]}],
                  "with each blank filled in the order Meta expects")
            async with AsyncSessionLocal() as db:
                row = (await db.execute(select(ChatHistory).where(ChatHistory.tenant_id == tid)
                                        .order_by(ChatHistory.id.desc()))).scalars().first()
            check(row.sender == "agent" and row.message_type == "template"
                  and "Hi Nimal, your order ORD-12 is ready to collect." in row.content and row.status == "SENT",
                  "kept in the chat as the words the customer read")
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/template", headers=A,
                                  json={"name": "order_ready", "language": "en_US", "body": {"1": "Nimal"}, "header": {"1": "x"}})
            check(r.status_code == 400 and "{{2}}" in r.json()["detail"], "an empty blank is refused before anything is sent")
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/template", headers=A,
                                  json={"name": "draft_one", "language": "en_US"})
            check(r.status_code == 400 and "isn't approved" in r.json()["detail"], "a template that isn't approved is refused")
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/template", headers=A,
                                  json={"name": "promo_photo", "language": "si", "body": {"name": "Nimal"}})
            check(r.status_code == 400 and "image" in r.json()["detail"], "an image-header template says why it can't be sent")
            n = len(sent)
            for _ in range(2):
                await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/template", headers=A,
                                  json={"name": "order_ready", "language": "en_US", "body": {"1": "A", "2": "B"},
                                        "header": {"1": "B"}, "client_msg_id": "tpl-dup"})
            check(len(sent) == n + 1, "the same send repeated (dropped connection) goes once")

            print("\nA business without a live number (test mode)")
            r = await client.get(f"/api/tenants/{mid}/templates", headers=B)
            check(r.status_code == 200 and {x["name"] for x in r.json()["templates"]} == {"order_update", "follow_up"},
                  "sample templates, so the screens can be tried")
    finally:
        httpx.AsyncClient = real_client
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
