"""End-to-end checks of the running app: startup, logins, the WhatsApp webhook, shop flow, tenant isolation."""
import hashlib
import hmac
import itertools
import json

from config import settings

SEED_PHONE_ID = settings.SEED_PHONE_NUMBER_ID
CUSTOMER = "94771234567"
_message_ids = itertools.count(1)


def whatsapp_text(body: str, phone_number_id: str = SEED_PHONE_ID, customer: str = CUSTOMER) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"id": f"wamid.test{next(_message_ids)}", "from": customer, "timestamp": "0",
                      "type": "text", "text": {"body": body}}],
    }}]}]}


def texts(outbox) -> str:
    return "\n".join(m.get("message_text", "") or m.get("body_text", "") for m in outbox)


def test_startup_seeds_first_tenant_without_a_real_token(client, auth):
    tenants = client.get("/api/tenants", headers=auth).json()
    assert [t["business_name"] for t in tenants] == ["Wasala Nature Resort - Banquet & Events"]
    assert tenants[0]["has_access_token"] is False
    assert "meta_access_token" not in tenants[0]


def test_api_needs_a_login(client):
    assert client.get("/api/tenants").status_code == 401
    assert client.post("/api/admin/login", json={"username": "admin", "password": "wrong"}).status_code == 401


def test_pages_are_served(client):
    for path in ("/admin", "/train", "/orders", "/health"):
        assert client.get(path).status_code == 200, path


def test_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}).status_code == 403


def test_webhook_signature_is_enforced_when_app_secret_is_set(client, outbox, monkeypatch):
    monkeypatch.setattr(settings, "META_APP_SECRET", "app-secret")
    raw = json.dumps(whatsapp_text("hello")).encode()

    assert client.post("/webhook", content=raw).status_code == 401
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    assert outbox == []

    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": signature}).status_code == 200
    assert outbox, "a correctly signed message should be answered"


def test_customer_message_gets_a_reply(client, outbox):
    assert client.post("/webhook", json=whatsapp_text("hello")).status_code == 200
    first = outbox[0]
    assert first["kind"] == "text" and first["recipient_phone"] == CUSTOMER
    assert "Wasala Nature Resort" in first["message_text"]


def test_meta_retry_is_answered_once(client, outbox):
    payload = whatsapp_text("hello again")
    client.post("/webhook", json=payload)
    answered = len(outbox)
    client.post("/webhook", json=payload)
    assert answered > 0 and len(outbox) == answered


def test_message_to_unknown_number_is_ignored(client, outbox):
    client.post("/webhook", json=whatsapp_text("hello", phone_number_id="000000"))
    assert outbox == []


def test_preview_answers_without_whatsapp(client, auth, outbox):
    tenant_id = client.get("/api/tenants", headers=auth).json()[0]["id"]
    res = client.post(f"/api/tenants/{tenant_id}/preview", json={"message": "can I get a discount?"}, headers=auth)
    assert res.status_code == 200
    assert res.json()["handoff"] is True and res.json()["reply"]
    assert outbox == []


def test_second_business_is_kept_separate(client, auth, outbox):
    created = client.post("/api/tenants", headers=auth, json={
        "business_name": "Kandy Spice Restaurant", "whatsapp_phone_number_id": "222333444",
        "meta_access_token": "EAA_placeholder_kandy", "system_prompt": "You help restaurant customers.",
        "knowledge_base": "Hours: 8 AM - 10 PM.", "front_office_number": "+94812223344",
    })
    assert created.status_code == 201, created.text

    client.post("/webhook", json=whatsapp_text("hello", phone_number_id="222333444", customer="94770000001"))
    reply = texts(outbox)
    assert "Kandy Spice Restaurant" in reply and "Wasala" not in reply

    outbox.clear()
    client.post("/webhook", json=whatsapp_text("what is your phone number", phone_number_id="222333444", customer="94770000001"))
    reply = texts(outbox)
    assert "+94812223344" in reply and "+94342275000" not in reply


def test_shop_cart_and_checkout(client, auth, outbox):
    tenant = next(t for t in client.get("/api/tenants", headers=auth).json() if t["business_name"].startswith("Kandy"))
    product = client.post(f"/api/tenants/{tenant['id']}/products", headers=auth, json={
        "name": "Chicken Kottu", "price": 1200, "stock_quantity": 10,
    })
    assert product.status_code == 201, product.text
    shopper = {"phone_number_id": "222333444", "customer": "94770000002"}

    client.post("/webhook", json=whatsapp_text("shop", **shopper))
    assert "Chicken Kottu" in texts(outbox)

    outbox.clear()
    client.post("/webhook", json=whatsapp_text("buy 2 chicken kottu", **shopper))
    assert "Added *Chicken Kottu* x2" in texts(outbox)

    outbox.clear()
    client.post("/webhook", json=whatsapp_text("checkout", **shopper))
    assert "Order Placed" in texts(outbox) and "2,400.00" in texts(outbox)

    orders = client.get(f"/api/tenants/{tenant['id']}/orders", headers=auth).json()
    assert len(orders) == 1 and orders[0]["total_amount"] == 2400 and orders[0]["status"] == "pending_payment"
    products = client.get(f"/api/tenants/{tenant['id']}/products", headers=auth).json()
    assert products[0]["stock_quantity"] == 8
