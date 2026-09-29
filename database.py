import logging
from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

logger = logging.getLogger("uvicorn.error")

# SQLAlchemy Async Engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
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
]


async def init_db() -> None:
    """Initialize database tables and apply lightweight column migrations for SQLite."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if "sqlite" in settings.DATABASE_URL:
            for table, column, column_def in _COLUMN_MIGRATIONS:
                result = await conn.execute(text(f"PRAGMA table_info({table})"))
                existing_columns = {row[1] for row in result.fetchall()}
                if column not in existing_columns:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}"))
                    logger.info(f"Migrated {table} table: added {column} column.")

    logger.info("Database initialized successfully.")
