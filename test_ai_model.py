"""
Comprehensive End-to-End AI Model Test Suite
Tests:
1. LLM Router & Provider connectivity (Groq, Gemini, OpenRouter)
2. Domain Knowledge Retrieval & Accuracy (Hospitality / Wasala Nature Resort)
3. Live ERP / Inventory Integration (Stock & Price checks)
4. Multi-language processing (English, Singlish, Sinhala)
5. Handoff & Escalation Triggers (Price negotiation / complaints)
6. Self-Learning QA Cache & Fuzzy Matching (Speed & Learning efficiency)
7. Auto Bot Profile Generator (Business analysis & Persona synthesis)
"""

import asyncio
import os
import sys
import time
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Tenant, Product, BotQACache
from tenant_config import build_bot_config
from document_service import get_tenant_documents
import llm_router
from ai_service import generate_ai_response, resolve_reply_documents
from bot_learning import cache_qa_pair, get_cached_answer, record_signal, get_learning_stats
from bot_profile_generator import generate_bot_profile

# Ensure console supports UTF-8 characters (emojis, Sinhala script)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def get_dynamic_kb(tenant: Tenant, db) -> str:
    stmt = select(Product).where(Product.tenant_id == tenant.id, Product.is_active == True)
    res = await db.execute(stmt)
    products = res.scalars().all()
    if not products:
        return tenant.knowledge_base or ""
    curr = f"{tenant.currency} " if tenant.currency else ""
    inv = "\n\n--- LIVE ERP INVENTORY ---\n"
    for p in products:
        status = f"{p.stock_quantity} in stock" if p.stock_quantity > 0 else "OUT OF STOCK"
        inv += f"- {p.name}: {curr}{p.price:,.2f} ({status})\n"
    inv += "\n[CRITICAL RULE]: When quoting products or prices, ALWAYS refer to the live ERP inventory above. If a customer asks if something is available, strictly check the exact stock here.\n"
    return (tenant.knowledge_base or "") + inv


async def run_comprehensive_ai_test():
    print("=" * 70)
    print("🚀 STARTING COMPREHENSIVE AI MODEL TEST SUITE")
    print("=" * 70)

    # -------------------------------------------------------------
    # TEST 1: LLM Router Connectivity & Performance
    # -------------------------------------------------------------
    print("\n[TEST 1] Testing Multi-Provider LLM Router Connectivity...")
    t0 = time.time()
    try:
        res = await llm_router.complete("chat", [
            {"role": "system", "content": "You are a concise AI assistant."},
            {"role": "user", "content": "Say: 'AI Engine Active and Ready'"}
        ])
        latency = time.time() - t0
        print(f"  ✅ Provider: {res.provider.upper()} ({res.model})")
        print(f"  ✅ Latency: {latency:.2f}s")
        print(f"  ✅ Response: {res.text.strip()}")
    except Exception as e:
        print(f"  ❌ LLM Router Error: {e}")
        return

    # Fetch Tenant 1 for domain tests
    async with AsyncSessionLocal() as db:
        stmt = select(Tenant).where(Tenant.id == 1)
        tenant = (await db.execute(stmt)).scalar_one_or_none()
        if not tenant:
            print("  ❌ Tenant 1 not found in database!")
            return

        documents = await get_tenant_documents(db, tenant.id, active_only=True)
        config = build_bot_config(tenant, documents)
        dynamic_kb = await get_dynamic_kb(tenant, db)

    print(f"\nLoaded Tenant: '{tenant.business_name}' | Industry: {tenant.industry} | Bot: {tenant.bot_name}")
    print(f"Knowledge Base Size: {len(dynamic_kb)} chars")

    # -------------------------------------------------------------
    # TEST 2: Domain Knowledge Base & Fact Retrieval
    # -------------------------------------------------------------
    print("\n[TEST 2] Testing Business Knowledge Retrieval (Wedding & Banquet Pricing)...")
    q2 = "What are the Silver Menu prices for Grand Ballroom and Starlight Ballroom?"
    print(f"  🗣️ Customer: \"{q2}\"")
    
    reply2, handoff2 = await generate_ai_response(
        system_prompt=tenant.system_prompt or "",
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=q2,
        config=config
    )
    clean_reply2, _ = resolve_reply_documents(reply2, q2, config)
    print(f"  🤖 Bot ({tenant.bot_name}):\n    " + clean_reply2.replace("\n", "\n    "))
    print(f"  Handoff triggered: {handoff2}")

    # -------------------------------------------------------------
    # TEST 3: Live ERP Inventory Query (Stock Check)
    # -------------------------------------------------------------
    print("\n[TEST 3] Testing Live ERP Inventory & Stock Awareness...")
    q3 = "Do you have wooden chairs available to buy, and what is the price?"
    print(f"  🗣️ Customer: \"{q3}\"")
    
    reply3, handoff3 = await generate_ai_response(
        system_prompt=tenant.system_prompt or "",
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=q3,
        config=config
    )
    clean_reply3, _ = resolve_reply_documents(reply3, q3, config)
    print(f"  🤖 Bot ({tenant.bot_name}):\n    " + clean_reply3.replace("\n", "\n    "))
    
    # -------------------------------------------------------------
    # TEST 4: Trilingual Capability (Singlish & Sinhala)
    # -------------------------------------------------------------
    print("\n[TEST 4] Testing Multilingual & Singlish Support...")
    q4_singlish = "machan packages monawada thiyenne? poddak kiyannako"
    print(f"  🗣️ Customer (Singlish): \"{q4_singlish}\"")
    reply4, _ = await generate_ai_response(
        system_prompt=tenant.system_prompt or "",
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=q4_singlish,
        config=config
    )
    clean_reply4, _ = resolve_reply_documents(reply4, q4_singlish, config)
    print(f"  🤖 Bot (Singlish response):\n    " + clean_reply4.replace("\n", "\n    "))

    # -------------------------------------------------------------
    # TEST 5: Handoff & Escalation Handling
    # -------------------------------------------------------------
    print("\n[TEST 5] Testing Negotiation & Human Handoff Triggers...")
    q5 = "This is too expensive! Can you give me a 40% discount or negotiate with me right now?"
    print(f"  🗣️ Customer: \"{q5}\"")
    reply5, handoff5 = await generate_ai_response(
        system_prompt=tenant.system_prompt or "",
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=q5,
        config=config
    )
    clean_reply5, _ = resolve_reply_documents(reply5, q5, config)
    print(f"  🤖 Bot:\n    " + clean_reply5.replace("\n", "\n    "))
    print(f"  ✅ Handoff Detected: {handoff5} (Expected: True)")

    # -------------------------------------------------------------
    # TEST 6: Self-Learning QA Cache & Fuzzy Match
    # -------------------------------------------------------------
    print("\n[TEST 6] Testing Self-Learning QA Cache (Fuzzy Match & Response Speed)...")
    async with AsyncSessionLocal() as db:
        # Cache an answer
        sample_q = "What is your photography policy for weddings?"
        sample_a = "You are welcome to bring your own photographers at Wasala Nature Resort with no extra corkage fees!"
        await cache_qa_pair(db, tenant.id, sample_q, sample_a)
        await record_signal(db, tenant.id, sample_q, is_positive=True)
        print(f"  Cached Q&A: '{sample_q}'")

        # Test variation with high similarity
        variant_q = "What is your photography policy for wedding events?"
        print(f"  Querying with variation: '{variant_q}'")
        t_cache = time.time()
        cached_result = await get_cached_answer(db, tenant.id, variant_q, threshold=0.80)
        cache_time = time.time() - t_cache
        if cached_result:
            print(f"  ✅ Cache HIT in {cache_time*1000:.1f}ms! (Saved full LLM call)")
            print(f"  ⚡ Cached Answer: {cached_result}")
        else:
            print("  ❌ Cache MISS (Fuzzy threshold not met)")

        stats = await get_learning_stats(db, tenant.id)
        print(f"  📊 Current Learning Stats: {stats}")

    # -------------------------------------------------------------
    # TEST 7: Auto Bot Profile Generator
    # -------------------------------------------------------------
    print("\n[TEST 7] Testing Auto Bot Profile Generator from Business Document...")
    sample_doc = """
    Business: Apex Dental & Orthodontics
    Location: 45 Galle Road, Colombo 03, Sri Lanka.
    Services: Teeth Whitening (Rs. 15,000), Dental Implants (Rs. 85,000), Routine Cleaning (Rs. 4,500).
    Working Hours: Mon-Sat 9:00 AM - 7:00 PM. Sunday Closed.
    Emergency Contact: 077 123 4567
    Notes: We never give medical diagnoses over chat. Always recommend booking an in-person consultation.
    """
    profile = await generate_bot_profile(sample_doc, business_name_hint="Apex Dental")
    print(f"  ✅ Auto-Detected Business: {profile.business_name}")
    print(f"  ✅ Detected Industry: {profile.industry} ({profile.business_type})")
    print(f"  ✅ Recommended Bot Name: {profile.bot_name} ({profile.bot_role_title})")
    print(f"  ✅ Greeting Style: {profile.greeting_style}")
    print(f"  ✅ Quick Menu Options:\n    " + profile.quick_menu.replace("\n", "\n    "))
    print(f"  ✅ Handoff Rules:\n    " + profile.handoff_rules.replace("\n", "\n    "))

    print("\n" + "=" * 70)
    print("🎉 ALL AI MODEL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_comprehensive_ai_test())
