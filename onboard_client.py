import asyncio
import sys
import logging
from database import AsyncSessionLocal, init_db
from models import Tenant
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("onboard_client")

async def onboard_hotel_tenant(
    business_name: str = "Grand Royal Hotel & Spa",
    whatsapp_phone_number_id: str = "109876543219999",
    meta_access_token: str = "EAA_placeholder_token_grand_royal_hotel",
    human_handoff_number: str = "+94779998877"
):
    """
    Onboards a new Hotel Tenant entry into the SQLite database.
    """
    # Ensure database schema exists
    await init_db()

    system_prompt = (
        f"You are the official 24/7 AI Concierge for {business_name}. "
        "Your role is to assist guests and prospective visitors with room bookings, hotel amenities, "
        "restaurant dining, check-in/check-out policies, and local Sri Lankan travel tips. "
        "Automatically detect and reply in the guest's language (English, Sinhala, or Singlish). "
        "Be refined, welcoming, and polite at all times. If a guest asks for urgent human assistance, "
        "a front desk manager, or custom booking arrangements, provide helpful instructions and flag for human handoff."
    )

    knowledge_base = (
        f"--- {business_name} KNOWLEDGE BASE ---\n"
        "Property Details:\n"
        "- Location: No. 12, Beachfront Drive, Bentota, Sri Lanka\n"
        "- Front Desk / Reception: Open 24/7\n"
        "- Check-In Time: 2:00 PM | Check-Out Time: 12:00 PM\n"
        "- Airport Transfer: Available upon request (Bandaranaike Intl Airport to Hotel: $60 USD)\n\n"
        "Room Types & Rates (per night, includes breakfast):\n"
        "1. Deluxe Ocean View Room: Rs. 35,000 (King Bed, Balcony, Sea View, Max 2 Adults)\n"
        "2. Executive Suite: Rs. 55,000 (Living room, Jacuzzi, Oceanfront, Max 3 Adults)\n"
        "3. Family Garden Villa: Rs. 70,000 (2 Bedrooms, Private Garden & Plunge Pool, Max 4 Adults)\n\n"
        "Dining & Restaurants:\n"
        "- 'The Wave' Oceanfront Seafood Buffet: Breakfast 7 AM - 10:30 AM | Dinner 7 PM - 10:30 PM\n"
        "- 'Spice & Palms' Authentic Sri Lankan & Asian Cuisine: Lunch 12 PM - 3 PM | Dinner 6:30 PM - 10 PM\n"
        "- 24/7 Room Service available via WhatsApp or dialing extension 0 from room.\n\n"
        "Facilities & Wellness:\n"
        "- Swimming Pool: Open 6:00 AM - 8:00 PM\n"
        "- Ayubowan Spa & Wellness: Massage therapies, Ayurveda treatments (9 AM - 7 PM)\n"
        "- Free High-Speed Wi-Fi throughout property (Network: RoyalGuest_WiFi)\n\n"
        "Policies & Payment:\n"
        "- Cancellation: Free up to 48 hours prior to check-in.\n"
        "- Accepted Payments: Credit Cards (Visa, MasterCard, Amex), Cash (LKR / USD / EUR).\n"
    )

    async with AsyncSessionLocal() as session:
        # Check if phone_number_id already registered
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == whatsapp_phone_number_id)
        result = await session.execute(stmt)
        existing_tenant = result.scalar_one_or_none()

        if existing_tenant:
            logger.warning(
                f"Tenant with whatsapp_phone_number_id='{whatsapp_phone_number_id}' already exists! "
                f"(Business Name: '{existing_tenant.business_name}', Tenant ID: {existing_tenant.id})"
            )
            return existing_tenant

        new_tenant = Tenant(
            business_name=business_name,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
            meta_access_token=meta_access_token,
            system_prompt=system_prompt,
            knowledge_base=knowledge_base,
            human_handoff_number=human_handoff_number
        )

        session.add(new_tenant)
        await session.commit()
        await session.refresh(new_tenant)

        logger.info("=" * 60)
        logger.info(f"SUCCESS: Onboarded New Hotel Tenant successfully!")
        logger.info(f"  Tenant ID               : {new_tenant.id}")
        logger.info(f"  Business Name           : {new_tenant.business_name}")
        logger.info(f"  WhatsApp Phone Number ID: {new_tenant.whatsapp_phone_number_id}")
        logger.info(f"  Human Handoff Number    : {new_tenant.human_handoff_number}")
        logger.info("=" * 60)
        return new_tenant

if __name__ == "__main__":
    # Can accept optional command line arguments: python onboard_client.py "Hotel Name" "Phone_ID"
    b_name = sys.argv[1] if len(sys.argv) > 1 else "Grand Royal Hotel & Spa"
    p_id = sys.argv[2] if len(sys.argv) > 2 else "109876543219999"
    asyncio.run(onboard_hotel_tenant(business_name=b_name, whatsapp_phone_number_id=p_id))
