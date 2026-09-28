import httpx
import json

BASE_URL = "http://127.0.0.1:8000"

def test_webhook_message(user_text: str, phone_number_id: str = "1157440684128924", from_phone: str = "94770000000"):
    """
    Simulates an incoming WhatsApp message payload from Meta to your local FastAPI backend.
    """
    print(f"\n--- Sending Test WhatsApp Message ---")
    print(f"From Phone : {from_phone}")
    print(f"Phone ID   : {phone_number_id} (Wasala Nature Resort)")
    print(f"Message    : \"{user_text}\"")

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "TEST_ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550255874",
                                "phone_number_id": phone_number_id
                            },
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": "wamid.HBgLTEST12345",
                                    "timestamp": "1722500000",
                                    "text": {"body": user_text},
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

    try:
        response = httpx.post(f"{BASE_URL}/webhook", json=payload, timeout=10.0)
        print(f"Webhook Status Code: {response.status_code}")
        print(f"Webhook Response   : {response.text}")
    except Exception as e:
        print(f"Error connecting to backend server: {e}")

if __name__ == "__main__":
    print("============================================================")
    print("WHATSAPP BOT LOCAL TEST SIMULATOR")
    print("============================================================")
    
    # Test 1: Room rate inquiry in English
    test_webhook_message("Hi! What are your room rates and check-in time at Wasala Nature Resort?")
    
    # Test 2: Singlish inquiry
    test_webhook_message("Macho room booking thiyenawada? Prices kohomada?")

    # Test 3: Human handoff request
    test_webhook_message("I need to speak with a human front desk manager please.")
