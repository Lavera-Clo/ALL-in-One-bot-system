import asyncio
from database import AsyncSessionLocal
from models import ChatHistory, Tenant
from sqlalchemy import select

async def view_history():
    async with AsyncSessionLocal() as session:
        stmt = select(ChatHistory).order_by(ChatHistory.id.desc()).limit(10)
        res = await session.execute(stmt)
        history = res.scalars().all()
        
        print("\n============================================================")
        print("LATEST CHAT HISTORY IN SQLITE DATABASE")
        print("============================================================")
        for msg in reversed(history):
            print(f"[{msg.created_at.strftime('%H:%M:%S')}] {msg.role.upper()} ({msg.customer_phone_number}): {msg.content}")
            print("-" * 60)

if __name__ == "__main__":
    asyncio.run(view_history())
