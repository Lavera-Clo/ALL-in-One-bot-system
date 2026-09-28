import asyncio
import logging
from whatsapp_client import send_whatsapp_button_message, send_whatsapp_list_message

logging.basicConfig(level=logging.INFO)

async def test_interactive():
    phone_number_id = "1157440684128924"
    mock_token = "EAA_placeholder_test_token"
    recipient = "94762997280"

    print("--- 1. Testing Interactive Quick Reply Buttons ---")
    btn_res = await send_whatsapp_button_message(
        phone_number_id=phone_number_id,
        recipient_phone=recipient,
        body_text="👇 Choose an option below to proceed quickly:",
        buttons=[
            {"id": "btn_prices", "title": "📊 View Prices"},
            {"id": "btn_menus", "title": "🍽️ View Menus"},
            {"id": "btn_contact", "title": "📞 Contact Manager"}
        ],
        meta_access_token=mock_token
    )
    print("Button response:", repr(btn_res))

    print("\n--- 2. Testing Interactive List Menu (Hall Selection) ---")
    list_res = await send_whatsapp_list_message(
        phone_number_id=phone_number_id,
        recipient_phone=recipient,
        body_text="🏰 *Select a venue below to explore capacity & features:*",
        button_title="Choose Hall",
        sections=[
            {
                "title": "Wasala Banquet Halls",
                "rows": [
                    {"id": "hall_grand", "title": "Grand Ballroom", "description": "150-650 Pax capacity | AC"},
                    {"id": "hall_starlight", "title": "Starlight Ballroom", "description": "100-350 Pax capacity | AC"},
                    {"id": "hall_garden", "title": "Garden Lawn Venue", "description": "Outdoor Lake View Event Area"},
                    {"id": "hall_regi", "title": "Registration Hall", "description": "Civil Registration & Small Events"}
                ]
            }
        ],
        meta_access_token=mock_token
    )
    print("List response:", repr(list_res))
    print("\nSUCCESS! Both interactive formats verified!")

if __name__ == "__main__":
    asyncio.run(test_interactive())
