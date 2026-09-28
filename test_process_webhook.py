import asyncio
import traceback
from main import process_webhook
from database import AsyncSessionLocal

class MockRequest:
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
                                        "id": "wamid.TEST12345",
                                        "timestamp": "1722500000",
                                        "text": {"body": "Hi, what are your room rates at Wasala Nature Resort?"},
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

async def run():
    async with AsyncSessionLocal() as session:
        try:
            res = await process_webhook(MockRequest(), session)
            print(f"Status Code: {res.status_code}")
        except Exception as e:
            print("EXCEPTION THROWN:")
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run())
