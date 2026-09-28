import asyncio
import logging
from database import AsyncSessionLocal, init_db
from models import Tenant
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("onboard_banquet")

async def onboard_banquet_tenant(
    business_name: str = "Wasala Nature Resort - Banquet & Events",
    whatsapp_phone_number_id: str = "1157440684128924",
    meta_access_token: str = "EAAjfYY4wqiYBSFUDPvPZAcBBEdDXNI8bEAlvLqMGsALEtFAVKniNFPLU5jdOodS53bYOyOettApsQQIxflC0Ca4ywHRayUnbZBtnHfxiKJ5DJdEg5ZBsafyZBLrbw4byqJPhqJdAwPiCQiX2HqCAZCSUekdKcrqz2yiAzPPCNV3JECNZBXVNmZBx9CxsxSSxY54PZCGQStST56bFbEgggTCRFDkh5HWg8MbO5ECctF2SPeVpSZAXETkg3e2p608BdizzYKkWnREDXAoPTydVgJWNWZCIQG",
    human_handoff_number: str = "+94779998877"
):
    """
    Onboards a dedicated Banquet & Events AI Bot Tenant for Wasala Nature Resort.
    """
    await init_db()

    system_prompt = (
        f"You are the senior Banquet & Event Planning AI Consultant for {business_name} in Bentota, Sri Lanka. "
        "Your expertise covers Weddings, Engagement Functions, Corporate Conferences, Birthday Galas, and Dinner Dances. "
        "Automatically detect and reply warmly in the guest's language (English, Sinhala, or Singlish). "
        "Provide clear information regarding hall capacity, menu pricing per head, seating layouts, and decoration rules. "
        "If a customer requests wedding brochure PDFs or hall photos, share the official download link or photo link clearly. "
        "If they request a custom quote for over 200 guests or want to reserve dates, notify front desk and request human handoff."
    )

    knowledge_base = (
        f"=== {business_name} - BANQUET & EVENTS MASTER KNOWLEDGE BASE ===\n\n"
        "🏛️ BANQUET HALLS & SEATING CAPACITY:\n"
        "1. Grand Royal Ballroom:\n"
        "   - Seating Capacity: Up to 650 guests (Banquet setup) | 800 guests (Theater setup)\n"
        "   - Features: Centralized AC, Crystal Chandeliers, LED Wall, Full Stage, Private Bridal Suite.\n"
        "   - Photo: https://images.unsplash.com/photo-1519167758481-83f550bb49b3?w=800 (Grand Ballroom Setup)\n\n"
        "2. Lotus Pavilion (Outdoor Lakefront Venue):\n"
        "   - Seating Capacity: Up to 350 guests\n"
        "   - Features: Open-air lakefront view, natural breeze, garden illumination, wooden porch stage.\n"
        "   - Photo: https://images.unsplash.com/photo-1527529482837-4698179dc6ce?w=800 (Outdoor Pavilion Setup)\n\n"
        "🍽️ WEDDING & BANQUET PACKAGES (Per Head Pricing):\n"
        "- Gold Buffet Package: LKR 5,500 net per head\n"
        "  (Includes: Welcome Drink, 2 Salads, 2 Rice items, Chicken/Fish/Pork, 3 Veg, 3 Desserts, Complementary Bridal Room)\n\n"
        "- Platinum Luxury Package: LKR 7,500 net per head\n"
        "  (Includes: International Seafood & Meat Buffet, Welcome Mocktails, Live Action Stations, Carving Station, Complementary Honeymoon Suite with Breakfast)\n\n"
        "- Corporate Conference Package: LKR 3,800 net per head\n"
        "  (Includes: Morning Tea & Snacks, Buffet Lunch, Afternoon Tea, Projector, Sound System, Stationary)\n\n"
        "📄 OFFICIAL BROCHURES & DOCUMENTS:\n"
        "- 2026 Wedding Packages & Menu PDF: https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf (Wedding_Package_Brochure_2026.pdf)\n\n"
        "📋 POLICIES & ADVANCE DEPOSIT:\n"
        "- Advance Booking Deposit: 25% to hold date, balance due 7 days prior to event.\n"
        "- Corkage Fee: Free corkage for hard liquor if soft drinks purchased from hotel.\n"
        "- Music Cut-off Time: 11:30 PM as per local council noise regulations.\n"
    )

    async with AsyncSessionLocal() as session:
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == whatsapp_phone_number_id)
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing:
            # Update existing tenant with enriched banquet details
            existing.business_name = business_name
            existing.system_prompt = system_prompt
            existing.knowledge_base = knowledge_base
            existing.meta_access_token = meta_access_token
            existing.human_handoff_number = human_handoff_number
            await session.commit()
            logger.info(f"SUCCESS: Upgraded existing Tenant (ID: {existing.id}) into Dedicated Banquet & Events Bot!")
            return existing

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
        logger.info(f"SUCCESS: Onboarded Dedicated Banquet & Events Bot!")
        logger.info(f"  Tenant ID               : {new_tenant.id}")
        logger.info(f"  Business Name           : {new_tenant.business_name}")
        logger.info(f"  WhatsApp Phone Number ID: {new_tenant.whatsapp_phone_number_id}")
        logger.info("=" * 60)
        return new_tenant

if __name__ == "__main__":
    asyncio.run(onboard_banquet_tenant())
