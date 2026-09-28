import asyncio
import logging
from fastapi.testclient import TestClient
from main import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verification")

client = TestClient(app)

def run_tests():
    print("=" * 60)
    print("RUNNING END-TO-END VERIFICATION TESTS FOR WHATSAPP AI BOT")
    print("=" * 60)

    # 1. Health check
    response = client.get("/health")
    assert response.status_code == 200, f"Health check failed: {response.text}"
    print(f"[OK] Health Check PASSED: {response.json()}")

    # 2. GET /webhook Verification test
    verify_params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "my_secure_meta_verify_token_123",
        "hub.challenge": "1122334455"
    }
    response = client.get("/webhook", params=verify_params)
    assert response.status_code == 200, f"Webhook verification failed: {response.text}"
    assert response.text == "1122334455", f"Challenge mismatch: {response.text}"
    print("[OK] GET /webhook Meta Verification PASSED")

    # 3. GET /webhook Verification Invalid Token test
    bad_params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong_token",
        "hub.challenge": "123"
    }
    response = client.get("/webhook", params=bad_params)
    assert response.status_code == 403, "Invalid token test failed"
    print("[OK] GET /webhook Invalid Token Security PASSED")

    # 4. POST /webhook - Simulate Incoming English Message
    payload_english = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550255874",
                                "phone_number_id": "109876543210987"
                            },
                            "contacts": [{"wa_id": "94770000000", "profile": {"name": "Kamal"}}],
                            "messages": [
                                {
                                    "from": "94770000000",
                                    "id": "wamid.HBgL...",
                                    "timestamp": "1722500000",
                                    "text": {"body": "Hi, what time do you open and what are your Kottu prices?"},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload_english)
    assert response.status_code == 200, f"POST webhook failed: {response.text}"
    print("[OK] POST /webhook (English Message Handling) PASSED")

    # 5. POST /webhook - Simulate Incoming Singlish Message
    payload_singlish = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550255874",
                                "phone_number_id": "109876543210987"
                            },
                            "messages": [
                                {
                                    "from": "94770000000",
                                    "id": "wamid.HBgL...",
                                    "timestamp": "1722500001",
                                    "text": {"body": "Macho chicken cheese kottu ekak thiyenawada?"},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload_singlish)
    assert response.status_code == 200, f"POST webhook failed: {response.text}"
    print("[OK] POST /webhook (Singlish Message Handling) PASSED")

    # 6. POST /webhook - Simulate Human Handoff Request
    payload_handoff = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550255874",
                                "phone_number_id": "109876543210987"
                            },
                            "messages": [
                                {
                                    "from": "94770000000",
                                    "id": "wamid.HBgL...",
                                    "timestamp": "1722500002",
                                    "text": {"body": "I need to talk to a human manager please."},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload_handoff)
    assert response.status_code == 200, f"POST webhook handoff failed: {response.text}"
    print("[OK] POST /webhook (Human Handoff Trigger) PASSED")

    # 7. POST /webhook - Simulate Incoming Hotel Inquiry Message for Grand Royal Hotel
    payload_hotel = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550259999",
                                "phone_number_id": "109876543219999"
                            },
                            "messages": [
                                {
                                    "from": "94771112233",
                                    "id": "wamid.HBgL...",
                                    "timestamp": "1722500003",
                                    "text": {"body": "Hi, what are your room rates for Deluxe Ocean View and check-in time?"},
                                    "type": "text"
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload_hotel)
    assert response.status_code == 200, f"POST webhook hotel failed: {response.text}"
    print("[OK] POST /webhook (Hotel Tenant Message Handling) PASSED")

    # 8. List Tenants API
    response = client.get("/api/tenants")
    assert response.status_code == 200, "List tenants failed"
    tenants = response.json()
    assert len(tenants) >= 3, "Tenant count unexpected"
    print(f"[OK] GET /api/tenants PASSED (Found {len(tenants)} active tenants)")

    print("=" * 60)
    print("ALL VERIFICATION TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
