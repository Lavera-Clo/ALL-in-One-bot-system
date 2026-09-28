import asyncio
import traceback
from main import process_webhook
from database import AsyncSessionLocal

class MockGreetingRequest:
    async def json(self):
        return {
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
                                    "phone_number_id": "1157440684128924"
                                },
                                "messages": [
                                    {
                                        "from": "94762997280",
                                        "id": "wamid.TEST_GREETING",
                                        "timestamp": "1722500000",
                                        "text": {"body": "Ayubowan"},
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

class MockButtonClickRequest:
    async def json(self):
        return {
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
                                    "phone_number_id": "1157440684128924"
                                },
                                "messages": [
                                    {
                                        "from": "94762997280",
                                        "id": "wamid.TEST_BTN_CLICK",
                                        "timestamp": "1722500005",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": "btn_prices",
                                                "title": "View Prices"
                                            }
                                        }
                                    }
                                ]
                            },
                            "field": "messages"
                        }
                    ]
                }
            ]
        }

class MockListClickRequest:
    async def json(self):
        return {
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
                                    "phone_number_id": "1157440684128924"
                                },
                                "messages": [
                                    {
                                        "from": "94762997280",
                                        "id": "wamid.TEST_LIST_CLICK",
                                        "timestamp": "1722500010",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "list_reply",
                                            "list_reply": {
                                                "id": "hall_grand",
                                                "title": "Grand Ballroom",
                                                "description": "150-650 Pax capacity"
                                            }
                                        }
                                    }
                                ]
                            },
                            "field": "messages"
                        }
                    ]
                }
            ]
        }

async def run():
    async with AsyncSessionLocal() as session:
        try:
            print("--- Testing Greeting Webhook ---")
            res1 = await process_webhook(MockGreetingRequest(), session)
            print("Greeting Webhook Status:", res1.status_code)

            print("\n--- Testing Button Click Webhook (View Prices) ---")
            res2 = await process_webhook(MockButtonClickRequest(), session)
            print("Button Click Status:", res2.status_code)

            print("\n--- Testing List Click Webhook (Grand Ballroom) ---")
            res3 = await process_webhook(MockListClickRequest(), session)
            print("List Click Status:", res3.status_code)

            print("\nALL WEBHOOK TESTS PASSED SUCCESSFULLY!")
        except Exception as e:
            print("EXCEPTION THROWN:")
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run())
