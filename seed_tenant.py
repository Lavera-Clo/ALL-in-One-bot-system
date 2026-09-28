import asyncio
import logging
from database import AsyncSessionLocal, init_db
from models import Tenant
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed")

async def seed_data():
    await init_db()

    async with AsyncSessionLocal() as session:
        # Check if tenant 1 exists
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == "109876543210987")
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing:
            logger.info("Demo tenant already exists in database.")
            return

        tenant1 = Tenant(
            business_name="Kandy Spice Restaurant",
            whatsapp_phone_number_id="109876543210987",
            meta_access_token="EAA_placeholder_token_for_kandy_spice",
            system_prompt=(
                "You are an AI customer support assistant for Kandy Spice Restaurant in Kandy, Sri Lanka. "
                "Be polite, warm, and helpful. Automatically respond in the user's language "
                "(English, Sinhala, or Singlish). Ask for delivery location if they order."
            ),
            knowledge_base=(
                "Restaurant Name: Kandy Spice Restaurant\n"
                "Opening Hours: Daily 8:00 AM - 10:00 PM\n"
                "Location: No. 45, Peradeniya Road, Kandy\n"
                "Contact: +94812223344\n"
                "Delivery Areas: Kandy Town, Peradeniya, Katugastota\n"
                "Menu & Prices:\n"
                "- Chicken Rice & Curry: Rs. 850\n"
                "- Fish Rice & Curry: Rs. 800\n"
                "- Mixed Kottu (Large): Rs. 1,200\n"
                "- Chicken Cheese Kottu: Rs. 1,400\n"
                "- Fresh Mango Juice: Rs. 350\n"
                "- Ginger Tea: Rs. 150\n"
                "Payment options: Cash on Delivery or Card payment on delivery."
            ),
            human_handoff_number="+94771234567"
        )

        tenant2 = Tenant(
            business_name="Lanka Tech Care",
            whatsapp_phone_number_id="998877665544332",
            meta_access_token="EAA_placeholder_token_for_lanka_tech",
            system_prompt=(
                "You are a technical support bot for Lanka Tech Care electronics repair shop. "
                "Help customers check repair pricing, laptop status, and warranty info in English or Singlish."
            ),
            knowledge_base=(
                "Services:\n"
                "- Laptop Screen Replacement: Rs. 18,000 (1 year warranty)\n"
                "- iPhone Battery Change: Rs. 12,000\n"
                "- Data Recovery: starting from Rs. 8,000\n"
                "Turnaround time: 1-2 working days."
            ),
            human_handoff_number="+94719998877"
        )

        session.add_all([tenant1, tenant2])
        await session.commit()
        logger.info("Successfully seeded 2 demo tenants into SQLite database!")

if __name__ == "__main__":
    asyncio.run(seed_data())
