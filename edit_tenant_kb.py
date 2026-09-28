import asyncio
import logging
import httpx
from database import AsyncSessionLocal, init_db
from models import Tenant
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("edit_kb")

# ==============================================================================
# OFFICIAL WASALA NATURE RESORT BANQUET PACKAGE PRICING MATRIX:
# ==============================================================================
OFFICIAL_KNOWLEDGE_BASE = """=== WASALA NATURE RESORT - BANQUET & EVENTS MASTER KNOWLEDGE BASE ===

🏛️ BANQUET HALLS AVAILABLE:
1. Grand Ballroom (Capacity 150-650 guests)
2. Starlight Ballroom (Capacity 100-350 guests)

📊 OFFICIAL BANQUET PACKAGE PRICING MATRIX (TOTAL LKR):

SILVER MENU PACKAGES:
- 100 Pax (Grand Ballroom): Rs. 539,500
- 100 Pax (Starlight Ballroom): Rs. 479,500
- 150 Pax (Grand Ballroom): Rs. 708,000
- 150 Pax (Starlight Ballroom): Rs. 678,000
- 200 Pax: Rs. 876,500
- 250 Pax: Rs. 1,075,000
- 300 Pax: Rs. 1,273,500

GOLD MENU PACKAGES:
- 100 Pax (Grand Ballroom): Rs. 589,500
- 100 Pax (Starlight Ballroom): Rs. 529,500
- 150 Pax (Grand Ballroom): Rs. 783,000
- 150 Pax (Starlight Ballroom): Rs. 753,000
- 200 Pax: Rs. 976,500
- 250 Pax: Rs. 1,200,000
- 300 Pax: Rs. 1,423,500

PREMIER MENU PACKAGES:
- 100 Pax (Grand Ballroom): Rs. 709,000
- 100 Pax (Starlight Ballroom): Rs. 649,000
- 150 Pax (Grand Ballroom): Rs. 917,500
- 150 Pax (Starlight Ballroom): Rs. 887,500
- 200 Pax: Rs. 1,143,500
- 250 Pax: Rs. 1,397,500
- 300 Pax: Rs. 1,647,500

🍽️ EXTRA PLATE RATES (PER EXTRA GUEST):
- Silver Package Extra Plate: Rs. 4,170.00
- Gold Package Extra Plate: Rs. 4,770.00
- Premier Menu Extra Plate: Rs. 5,370.00

👥 PER-HEAD BREAKDOWN RATES:
1. Starlight Ballroom:
   - Silver Menu: Rs. 3,395.00 / head
   - Gold Menu: Rs. 3,895.00 / head
   - Premier Menu: Rs. 4,795.00 / head

2. Grand Ballroom (150 Pax - 200 Pax):
   - Silver Menu: Rs. 3,595.00 / head
   - Gold Menu: Rs. 4,095.00 / head

3. More Than 200 Pax (All Halls):
   - Silver Menu: Rs. 3,395.00 / head
   - Gold Menu: Rs. 3,895.00 / head

📄 OFFICIAL BROCHURES & DOCUMENTS:
- 2026 Wedding Packages Brochure PDF: https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf

📋 POLICIES & DEPOSIT:
- Advance Booking Deposit: 25% to hold date, balance due 7 days prior to event.
- Free Corkage for hard liquor if soft drinks purchased from hotel.
- Music Cut-off Time: 11:30 PM as per local noise regulations.
"""

CLOUD_RENDER_URL = "https://whatsapp-ai-bot-3f6n.onrender.com/api/tenants/1"

async def update_knowledge_base():
    """Updates local SQLite database and live Render cloud API."""
    await init_db()

    # 1. Update Local Database
    async with AsyncSessionLocal() as session:
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == "1157440684128924")
        tenants = (await session.execute(stmt)).scalars().all()

        for tenant in tenants:
            tenant.knowledge_base = OFFICIAL_KNOWLEDGE_BASE
            logger.info(f"✅ Local DB updated for tenant '{tenant.business_name}' (ID: {tenant.id})")
        await session.commit()

    # 2. Update Render Cloud API
    async with httpx.AsyncClient() as client:
        for tid in [1, 5]:
            try:
                payload = {"knowledge_base": OFFICIAL_KNOWLEDGE_BASE}
                res = await client.put(f"https://whatsapp-ai-bot-3f6n.onrender.com/api/tenants/{tid}", json=payload, timeout=30.0)
                if res.status_code == 200:
                    logger.info(f"🚀 LIVE RENDER CLOUD SERVER UPDATED FOR TENANT {tid} WITH OFFICIAL PRICE MATRIX!")
            except Exception as e:
                logger.error(f"Could not update Render API for tenant {tid}: {e}")

if __name__ == "__main__":
    asyncio.run(update_knowledge_base())
