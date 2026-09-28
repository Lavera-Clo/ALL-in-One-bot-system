"""
One-time migration from the old single-client setup to the per-tenant portal.

Run once, after pulling these changes:

    python migrate_to_portal.py

It is safe to run twice — every step checks before it writes.

What it does, per tenant:
  1. Fills in the bot identity fields that used to be hardcoded in ai_service.py
     (bot name, role, location, currency, persona template, handoff rules, quick menu).
  2. Converts the tenant's CURRENT knowledge_base text into a document row, so the
     knowledge base survives being rebuilt from the documents table. Without this step
     the first rebuild would leave the bot with nothing to answer from.
  3. Registers the PDFs already sitting in static/pdfs/ as sendable documents for
     tenant 1, with the trigger keywords the old hardcoded resolver used, so the bot
     keeps sending the same files to the same questions.
"""

import asyncio
import os

from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal, init_db
from models import Tenant, TenantDocument
from training_service import compose_knowledge_base

PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pdfs")

# The keyword lists the old get_pdfs_for_user_message() had baked into code, now attached
# to the documents they belong to. Filename -> (title, keywords, caption suffix).
LEGACY_PDF_KEYWORDS = {
    "Price_List.pdf": (
        "Wedding Price List",
        "price, prices, cost, rate, rates, quotation, budget, ganan, mila, ගණන්, මිල, ලැයිස්තුව, price list",
        "Official Wedding Price List",
    ),
    "Wasala_Banquets.pdf": (
        "Banquet & Menu Details",
        "menu, items, complimentary, compliment, terms, policy, policies, banquet, කෑම, මෙන්නු, කොන්දේසි",
        "Full Banquet & Menu Details",
    ),
    "REGI_2026.pdf": (
        "Registration Packages 2026",
        "registration, register, regi, civil, රෙජිස්ට්‍රේෂන්, රෙජිස්ටර්",
        "Registration Packages 2026",
    ),
}

# Defaults for the very first tenant, matching how the bot behaved before this change.
TENANT_ONE_DEFAULTS = {
    "bot_name": "Nethmi",
    "bot_role_title": "Senior Banquet & Events Consultant",
    "business_location": "Bentota, Sri Lanka",
    "currency": "LKR",
    "persona_template": "hospitality",
    "handoff_rules": (
        "Discounts, special prices, negotiation, bargaining, reducing price\n"
        "Custom menus, menu swaps, special dietary requests beyond standard packages\n"
        "Date holds, reservations, booking confirmations\n"
        "Complaints, refunds, cancellations, problems with service\n"
        "Anything over 200 guests\n"
        "Explicit requests to speak to a manager, human, or staff member"
    ),
    "quick_menu": (
        "Grand Ballroom | 150-650 Pax capacity | AC\n"
        "Starlight Ballroom | 100-350 Pax capacity | AC\n"
        "Garden Lawn Venue | Outdoor Lake View Event Area\n"
        "Registration Hall | Civil Registration & Small Events"
    ),
}

GENERIC_DEFAULTS = {
    "bot_name": "Assistant",
    "bot_role_title": "Customer Care Assistant",
    "persona_template": "generic",
}


async def migrate():
    await init_db()

    async with AsyncSessionLocal() as db:
        tenants = list((await db.execute(select(Tenant))).scalars().all())
        if not tenants:
            print("No tenants found — nothing to migrate.")
            return

        for tenant in tenants:
            print(f"\n--- Tenant {tenant.id}: {tenant.business_name} ---")

            # 1. Backfill identity fields, never overwriting anything already set.
            defaults = TENANT_ONE_DEFAULTS if tenant.id == 1 else GENERIC_DEFAULTS
            filled = []
            for field, value in defaults.items():
                if not getattr(tenant, field, None):
                    setattr(tenant, field, value)
                    filled.append(field)
            if filled:
                print(f"  Filled bot profile fields: {', '.join(filled)}")
            else:
                print("  Bot profile already configured — left alone.")

            existing = list(
                (await db.execute(select(TenantDocument).where(TenantDocument.tenant_id == tenant.id)))
                .scalars()
                .all()
            )
            existing_titles = {d.title for d in existing}

            # 2. Preserve the knowledge base the bot is answering from right now.
            current_kb = (tenant.knowledge_base or "").strip()
            has_legacy = any(d.title == "Existing knowledge base" for d in existing)
            if current_kb and not has_legacy and "===== SOURCE:" not in current_kb:
                db.add(TenantDocument(
                    tenant_id=tenant.id,
                    title="Existing knowledge base",
                    source="text",
                    doc_type="knowledge",
                    raw_text=current_kb,
                    summary=current_kb,
                    trigger_keywords="",
                    caption=None,
                    char_count=len(current_kb),
                    is_active=True,
                ))
                print(f"  Preserved existing knowledge base as a document ({len(current_kb)} chars).")
            else:
                print("  Knowledge base already document-backed — skipped.")

            # 3. Attach the legacy PDFs to tenant 1 so file sending keeps working.
            if tenant.id == 1 and os.path.isdir(PDF_DIR):
                for filename, (title, keywords, caption_suffix) in LEGACY_PDF_KEYWORDS.items():
                    path = os.path.join(PDF_DIR, filename)
                    if not os.path.exists(path):
                        print(f"  PDF not found, skipped: {filename}")
                        continue
                    if title in existing_titles:
                        print(f"  Already registered: {title}")
                        continue
                    db.add(TenantDocument(
                        tenant_id=tenant.id,
                        title=title,
                        source="pdf",
                        original_filename=filename,
                        stored_path=path,
                        file_url=f"{settings.BASE_URL.rstrip('/')}/static/pdfs/{filename}",
                        doc_type="sendable",   # its content is already in the preserved KB above
                        raw_text="",
                        summary="",
                        trigger_keywords=keywords,
                        caption=f"{tenant.business_name} - {caption_suffix}",
                        char_count=0,
                        is_active=True,
                    ))
                    print(f"  Registered sendable PDF: {title}")

            await db.commit()

            # 4. Rebuild the knowledge base from the documents we just created.
            documents = list(
                (await db.execute(select(TenantDocument).where(TenantDocument.tenant_id == tenant.id)))
                .scalars()
                .all()
            )
            rebuilt = compose_knowledge_base(documents)
            tenant.knowledge_base = rebuilt
            await db.commit()
            print(f"  Knowledge base rebuilt from {len(documents)} document(s), {len(rebuilt)} chars.")

    print("\nMigration complete. Open /train to manage documents and the bot profile.")


if __name__ == "__main__":
    asyncio.run(migrate())
