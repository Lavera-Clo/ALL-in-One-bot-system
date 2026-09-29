import asyncio
from database import AsyncSessionLocal
from sqlalchemy import select
from models import Tenant, Product
from main import get_dynamic_kb
from ai_service import generate_ai_response
from tenant_config import build_bot_config

async def run_test():
    print("--- ERP LIVE INVENTORY TEST ---")
    
    async with AsyncSessionLocal() as db:
        # 1. Get our test tenant
        stmt = select(Tenant).where(Tenant.id == 1)
        tenant = (await db.execute(stmt)).scalar_one_or_none()
        
        # 2. Add a test product to the ERP
        print("\n📦 [ERP DASHBOARD] Adding a product to inventory...")
        product = Product(
            tenant_id=tenant.id,
            name="Sony Headphones",
            description="Noise cancelling bluetooth headphones",
            price=120.00,
            stock_quantity=5, # 5 in stock!
            is_active=True
        )
        db.add(product)
        await db.commit()
        await db.refresh(product)
        
        # 3. Simulate WhatsApp Message
        print("\n🗣️ [WHATSAPP] Customer asks: 'Do you have Sony Headphones in stock?'")
        dynamic_kb = await get_dynamic_kb(tenant, db)
        config = build_bot_config(tenant, [])
        
        reply1, _ = await generate_ai_response(
            system_prompt=tenant.system_prompt or "You are a helpful assistant.",
            knowledge_base=dynamic_kb,
            chat_history=[],
            user_message="Do you have Sony Headphones in stock?",
            config=config
        )
        print(f"🤖 [AI BOT]: {reply1}")
        
        # 4. Simulate a business owner updating the ERP
        print("\n💻 [ERP DASHBOARD] Business Owner changes stock_quantity to 0...")
        product.stock_quantity = 0
        await db.commit()
        
        # 5. Simulate WhatsApp Message again
        print("\n🗣️ [WHATSAPP] Customer asks: 'Do you have Sony Headphones in stock?'")
        dynamic_kb2 = await get_dynamic_kb(tenant, db)
        
        reply2, _ = await generate_ai_response(
            system_prompt=tenant.system_prompt or "You are a helpful assistant.",
            knowledge_base=dynamic_kb2,
            chat_history=[],
            user_message="Do you have Sony Headphones in stock?",
            config=config
        )
        print(f"🤖 [AI BOT]: {reply2}")
        
        # Cleanup
        await db.delete(product)
        await db.commit()

if __name__ == "__main__":
    asyncio.run(run_test())
