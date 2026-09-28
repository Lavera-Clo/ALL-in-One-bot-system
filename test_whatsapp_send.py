import asyncio
import logging
from database import AsyncSessionLocal
from models import Tenant
from sqlalchemy import select
from whatsapp_client import send_whatsapp_message

logging.basicConfig(level=logging.INFO)

async def test_send():
    async with AsyncSessionLocal() as session:
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == "1157440684128924")
        tenant = (await session.execute(stmt)).scalars().first()

        if not tenant:
            print("Tenant not found!")
            return

        print(f"Testing Tenant: {tenant.business_name}")
        print(f"Token Prefix: {tenant.meta_access_token[:25]}...")

        try:
            res = await send_whatsapp_message(
                phone_number_id=tenant.whatsapp_phone_number_id,
                recipient_phone="94762997280",
                message_text="Test message from your live Banquet AI Bot!",
                meta_access_token=tenant.meta_access_token
            )
            print("SUCCESS! Result:", res)
        except Exception as e:
            print("FAILED WITH ERROR:", e)

if __name__ == "__main__":
    asyncio.run(test_send())
