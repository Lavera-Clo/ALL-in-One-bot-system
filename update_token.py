import os
import asyncio
import logging
from database import AsyncSessionLocal
from models import Tenant
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("update_token")

TOKEN = os.getenv("META_ACCESS_TOKEN", "")  # set in .env, never in code
PHONE_ID = "1157440684128924"

async def update_token():
    async with AsyncSessionLocal() as session:
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == PHONE_ID)
        tenants = (await session.execute(stmt)).scalars().all()

        for tenant in tenants:
            tenant.meta_access_token = TOKEN
            logger.info(f"SUCCESS: Active Meta Access Token updated for tenant '{tenant.business_name}' (ID: {tenant.id})!")
        await session.commit()

if __name__ == "__main__":
    asyncio.run(update_token())
