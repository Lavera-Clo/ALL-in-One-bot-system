"""
Checks migrate_to_postgres.py: a SQLite database with real-looking data is copied into an empty
PostgreSQL database, nothing is lost, new rows get fresh ids, and copying twice is refused.

Needs the embedded PostgreSQL (pip install pgserver asyncpg); skipped without it.
Uses throwaway databases only.

    python test_migrate_to_postgres.py
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
SQLITE_URL = "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "old_platform.db").replace("\\", "/")
os.environ["DATABASE_URL"] = SQLITE_URL

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


async def build_sqlite():
    from auth_service import hash_password
    from database import AsyncSessionLocal, engine, init_db
    from models import AdminUser, ChatHistory, Conversation, Product, Tenant
    from order_service import create_manual_order

    await init_db()
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lanka Leaf", whatsapp_phone_number_id="9999", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="Tea shop", bot_enabled=False)
        db.add(t)
        await db.flush()
        db.add(AdminUser(username="owner@leaf.test", password_hash=hash_password("pw-123456"), role="client", tenant_id=t.id))
        p = Product(tenant_id=t.id, name="Ceylon Tea", price=950, stock_quantity=10)
        db.add(p)
        for i in range(1205):                                 # more than one copy batch
            db.add(ChatHistory(tenant_id=t.id, customer_phone_number="94771234567", role="user" if i % 2 else "assistant",
                               content=f"message {i}", sender="customer" if i % 2 else "bot", status="SENT"))
        db.add(Conversation(tenant_id=t.id, customer_phone="94771234567", customer_name="Nimal", saved_name="Nimal W.",
                            bot_enabled=False, message_count=1205))
        await db.commit()
        await create_manual_order(db, t, "94771234567", "Nimal", [(p.id, 2)], status="confirmed")
    await engine.dispose()


async def run():
    try:
        import pgserver
    except ImportError:
        print("  --  pgserver not installed: skipped (pip install pgserver asyncpg)")
        return
    from migrate_to_postgres import migrate
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    print("\nCopying a SQLite database into PostgreSQL")
    await build_sqlite()
    server = pgserver.get_server(os.path.join(tempfile.gettempdir(), "whatsapp_bot_pg_tests"), cleanup_mode="stop")
    server.psql("DROP DATABASE IF EXISTS t_migrate;")
    server.psql("CREATE DATABASE t_migrate;")
    pg_url = server.get_uri().rsplit("/", 1)[0] + "/t_migrate"

    copied = await migrate(SQLITE_URL, pg_url, quiet=True)
    check(copied["chat_history"] == 1205 and copied["tenants"] == 1 and copied["orders"] == 1
          and copied["order_items"] == 1 and copied["conversations"] == 1, "every row arrives, across several batches")

    from models import AdminUser, ChatHistory, Conversation, Product, Tenant
    from auth_service import verify_password
    pg = create_async_engine("postgresql+asyncpg://" + pg_url.split("://", 1)[1])
    async with AsyncSession(pg) as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        conv = (await db.execute(select(Conversation))).scalar_one()
        user = (await db.execute(select(AdminUser).where(AdminUser.username == "owner@leaf.test"))).scalar_one()
        product = (await db.execute(select(Product))).scalar_one()
        check(tenant.bot_enabled is False and conv.bot_enabled is False and conv.saved_name == "Nimal W.",
              "switches and contact names come across as they were")
        check(verify_password("pw-123456", user.password_hash), "owners sign in with their same password")
        check(product.stock_quantity == 8, "stock already taken by orders is kept")
        db.add(ChatHistory(tenant_id=tenant.id, customer_phone_number="94770000000", role="user", content="new"))
        db.add(Tenant(business_name="Second", whatsapp_phone_number_id="1", meta_access_token="EAA_placeholder_token",
                      system_prompt="x", knowledge_base="x"))
        await db.commit()
        top = (await db.execute(select(func.max(ChatHistory.id)))).scalar()
        check(top == 1206, "new messages get the next id, no collision with copied ones")
    await pg.dispose()

    try:
        await migrate(SQLITE_URL, pg_url, quiet=True)
        refused = False
    except SystemExit as stop:
        refused = "already has rows" in str(stop)
    check(refused, "copying into a database that already has data is refused (no duplicates)")


if __name__ == "__main__":
    try:
        asyncio.run(run())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
