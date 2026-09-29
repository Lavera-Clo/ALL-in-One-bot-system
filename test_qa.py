import asyncio
import requests
from database import AsyncSessionLocal
from sqlalchemy import select
from models import Tenant
from ai_service import generate_ai_response

BASE_URL = "http://127.0.0.1:8000"

async def run_test():
    print("--- Q&A AI Training Test ---")
    
    # 1. Train the bot with new business documentation
    print("\n1. Training the bot with new business data...")
    dummy_business_doc = """
    Welcome to The Grand Ocean Hotel. 
    - Check-in time is 2:00 PM. Check-out is 11:00 AM.
    - Breakfast is served from 7:00 AM to 10:30 AM at the Blue Marina Restaurant.
    - Our pool is open from 6:00 AM to 10:00 PM.
    - The Wi-Fi password for all guests is 'grandocean2026'.
    - We offer airport shuttles for $45 per trip.
    """
    
    # Send to the training API
    res = requests.post(f"{BASE_URL}/api/tenants/1/train", data={"text": dummy_business_doc})
    if res.status_code != 200:
        print(f"Failed to train bot: {res.text}")
        return
        
    print("✅ Training successful!")
    print("Generated Knowledge Base:")
    print(res.json().get("knowledge_base", ""))
    
    # 2. Test the AI's Q&A capabilities directly
    print("\n2. Testing Customer Q&A Responses...")
    
    # Fetch the tenant directly from the DB to get the updated knowledge base
    async with AsyncSessionLocal() as db:
        stmt = select(Tenant).where(Tenant.id == 1)
        tenant = (await db.execute(stmt)).scalar_one_or_none()
        
    test_questions = [
        "What time can I check in?",
        "Do you have a pool? When is it open?",
        "What is the wifi password?",
        "Can I get a ride to the airport?"
    ]
    
    from unittest.mock import patch
    from config import settings

    for q in test_questions:
        print(f"\n🗣️ Customer asks: '{q}'")
        
        ai_reply, _ = await generate_ai_response(
            system_prompt=tenant.system_prompt or "You are a helpful assistant.",
            knowledge_base=tenant.knowledge_base or "",
            chat_history=[],
            user_message=q
        )
        print(f"🤖 Bot replies: {ai_reply}")

if __name__ == "__main__":
    asyncio.run(run_test())
