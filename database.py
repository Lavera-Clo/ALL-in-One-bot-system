import logging
from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings
from industry_brain import detect_industry, normalise_industry

logger = logging.getLogger("uvicorn.error")

def database_url(raw: str) -> str:
    """
    Accepts the URL as Supabase / Postgres hosts print it ("postgresql://...", "postgres://...")
    and points it at the async driver. SQLite URLs pass through unchanged.
    """
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix):]
    return raw


DATABASE_URL = database_url(settings.DATABASE_URL)
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# SQLAlchemy Async Engine.
# PostgreSQL (e.g. Supabase) is the shared database the website, the Android app and the bot
# all use in production; SQLite stays for local development and the offline tests.
# statement_cache_size=0 keeps asyncpg working behind Supabase's connection pooler (pgbouncer).
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    **({"connect_args": {"check_same_thread": False}} if IS_SQLITE else {
        "pool_pre_ping": True, "pool_size": 5, "max_overflow": 10, "pool_recycle": 1800,
        "connect_args": {"statement_cache_size": 0},
    }),
)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# Base class for SQLAlchemy ORM models
class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that yields database sessions for API endpoints."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            await session.close()


# Columns added after a table already existed in production DBs. Each entry is
# (table, column, "ALTER TABLE ... ADD COLUMN ..." fragment applied after the column name).
_COLUMN_MIGRATIONS = [
    ("products", "low_stock_threshold", "INTEGER NOT NULL DEFAULT 5"),
    ("cart_items", "reminded_at", "DATETIME"),
    ("orders", "payment_provider", "VARCHAR(30)"),
    ("orders", "payment_link_url", "TEXT"),
    ("orders", "payment_intent_id", "VARCHAR(255)"),
    ("orders", "paid_at", "DATETIME"),
    ("tenants", "bot_name", "VARCHAR(100)"),
    ("tenants", "bot_role_title", "VARCHAR(150)"),
    ("tenants", "business_location", "VARCHAR(255)"),
    ("tenants", "currency", "VARCHAR(10)"),
    ("tenants", "persona_template", "VARCHAR(50)"),
    ("tenants", "handoff_rules", "TEXT"),
    ("tenants", "quick_menu", "TEXT"),
    ("tenant_documents", "status", "VARCHAR(20) NOT NULL DEFAULT 'ready'"),
    ("tenant_documents", "training_stage", "TEXT"),
    ("tenant_documents", "training_progress", "INTEGER NOT NULL DEFAULT 0"),
    ("tenant_documents", "training_report", "TEXT"),
    ("tenant_documents", "extraction_method", "VARCHAR(30)"),
    ("tenant_documents", "public_path", "TEXT"),
    ("tenants", "industry", "VARCHAR(50)"),
    ("tenants", "quick_buttons", "TEXT"),
    ("tenants", "industry_source", "VARCHAR(20)"),
    ("tenants", "understanding", "TEXT"),
    ("admin_users", "tenant_id", "INTEGER REFERENCES tenants(id) ON DELETE CASCADE"),
    ("admin_users", "full_name", "VARCHAR(150)"),
    ("tenants", "bot_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("chat_history", "sender", "VARCHAR(20)"),
    ("chat_history", "message_type", "VARCHAR(20)"),
    ("chat_history", "status", "VARCHAR(20)"),
    ("chat_history", "error", "TEXT"),
    ("chat_history", "wa_message_id", "VARCHAR(128)"),
    ("tenants", "history_days", "INTEGER NOT NULL DEFAULT 21"),
    ("chat_history", "client_msg_id", "VARCHAR(64)"),
    ("chat_history", "changed_us", "BIGINT"),
    ("conversations", "saved_name", "VARCHAR(150)"),
    ("conversations", "notes", "TEXT"),
    ("conversations", "message_count", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "changed_us", "BIGINT"),
]

# Indexes for columns added by migration (create_all only indexes brand-new tables).
_INDEX_MIGRATIONS = [
    ("ix_chat_history_changed_us", "chat_history", "changed_us"),
    ("ix_chat_history_client_msg_id", "chat_history", "client_msg_id"),
    ("ix_conversations_changed_us", "conversations", "changed_us"),
]


async def _backfill_tenant_industry(conn) -> None:
    """
    Runs once, when tenants.industry is first added. Older rows carried the industry in
    persona_template ('hospitality', 'retail', 'services'); where that says nothing useful it
    is read from the business's own name and prompt. Unclear rows stay empty and run generic.
    """
    rows = (await conn.execute(text(
        "SELECT id, business_name, bot_role_title, system_prompt, persona_template FROM tenants"
    ))).fetchall()
    for tenant_id, name, role_title, system_prompt, persona in rows:
        industry = normalise_industry(persona)
        if industry in ("", "generic"):
            industry = detect_industry(f"{role_title or ''} {system_prompt or ''}", name or "") or industry
        if industry:
            await conn.execute(text("UPDATE tenants SET industry = :industry WHERE id = :id"),
                               {"industry": industry, "id": tenant_id})
            logger.info(f"Tenant {tenant_id} ({name}): industry set to '{industry}'.")


def _column_sql(column_def: str) -> str:
    """The migration fragments are written for SQLite; this is their PostgreSQL spelling."""
    if IS_SQLITE:
        return column_def
    out = column_def.replace("DATETIME", "TIMESTAMP")
    if out.startswith("BOOLEAN"):
        out = out.replace("DEFAULT 1", "DEFAULT TRUE").replace("DEFAULT 0", "DEFAULT FALSE")
    return out


async def init_db() -> None:
    """Create missing tables and add columns introduced since a database was created (SQLite or PostgreSQL)."""
    from sqlalchemy import inspect

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        existing = await conn.run_sync(
            lambda sync_conn: {t: {c["name"] for c in inspect(sync_conn).get_columns(t)}
                               for t in inspect(sync_conn).get_table_names()})
        for table, column, column_def in _COLUMN_MIGRATIONS:
            if table in existing and column not in existing[table]:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {_column_sql(column_def)}"))
                existing[table].add(column)
                logger.info(f"Migrated {table} table: added {column} column.")
                if (table, column) == ("tenants", "industry"):
                    await _backfill_tenant_industry(conn)
                if column == "changed_us":
                    # Existing rows sync too: stamp them in the order they were written.
                    await conn.execute(text(f"UPDATE {table} SET changed_us = id"))
                if (table, column) == ("conversations", "message_count"):
                    await conn.execute(text(
                        "UPDATE conversations SET message_count = (SELECT COUNT(*) FROM chat_history h "
                        "WHERE h.tenant_id = conversations.tenant_id "
                        "AND h.customer_phone_number = conversations.customer_phone)"))
        for index, table, column in _INDEX_MIGRATIONS:
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})"))

    logger.info(f"Database initialized successfully ({'SQLite' if IS_SQLITE else 'PostgreSQL'}).")
