"""
Copy the platform's data from SQLite (whatsapp_bot.db) into the shared PostgreSQL database
(e.g. Supabase), once, when moving to the common database.

    python migrate_to_postgres.py --from sqlite+aiosqlite:///./whatsapp_bot.db --to "postgresql://USER:PASS@HOST:5432/postgres"

  • The source is only read. Stop the server first so nothing changes while copying.
  • The target must be EMPTY (a new Supabase project, or one you just reset); the script refuses
    to copy into tables that already hold rows, so it can't create duplicates.
  • Tables are created on the target first, then copied in dependency order, then id counters
    are moved past the copied ids so new rows don't collide.
  • Then point the server's DATABASE_URL at the target and start it.
"""

import argparse
import asyncio
import sys

from sqlalchemy import MetaData, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

BATCH = 1000


def _url(raw: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix):]
    return raw


async def migrate(source_url: str, target_url: str, quiet: bool = False) -> dict:
    import models  # noqa: F401  (registers every table on Base.metadata)
    from database import Base

    say = (lambda *a: None) if quiet else print
    source = create_async_engine(_url(source_url))
    target = create_async_engine(_url(target_url), connect_args={"statement_cache_size": 0}
                                 if "asyncpg" in _url(target_url) else {})
    copied = {}
    try:
        async with target.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table in Base.metadata.sorted_tables:
                if (await conn.execute(select(func.count()).select_from(table))).scalar():
                    raise SystemExit(f"Target table '{table.name}' already has rows. Use an empty database.")

        # What the source really has (an older database may lack newer columns or tables).
        source_meta = MetaData()
        async with source.connect() as conn:
            await conn.run_sync(source_meta.reflect)

        for table in Base.metadata.sorted_tables:
            src = source_meta.tables.get(table.name)
            if src is None:
                say(f"  {table.name}: not in the source, skipped")
                continue
            shared = [c.name for c in table.columns if c.name in src.columns]
            total, offset = 0, 0
            while True:
                async with source.connect() as conn:
                    rows = (await conn.execute(select(*[src.c[n] for n in shared])
                                               .order_by(*src.primary_key.columns).limit(BATCH).offset(offset))).all()
                if not rows:
                    break
                records = [dict(zip(shared, r)) for r in rows]
                for rec in records:          # SQLite keeps booleans as 0/1 and may hold NULL where a default applies
                    for col in table.columns:
                        if col.name in rec and rec[col.name] is None and not col.nullable and col.default is not None:
                            arg = col.default.arg
                            rec[col.name] = arg(None) if callable(arg) else arg
                        if col.name in rec and rec[col.name] is not None and str(col.type) == "BOOLEAN":
                            rec[col.name] = bool(rec[col.name])
                async with target.begin() as conn:
                    await conn.execute(table.insert(), records)
                total += len(records)
                offset += BATCH
            copied[table.name] = total
            say(f"  {table.name}: {total} rows")

        if target.dialect.name == "postgresql":
            async with target.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    pk = list(table.primary_key.columns)
                    if len(pk) == 1 and pk[0].autoincrement and str(pk[0].type) == "INTEGER":
                        await conn.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                            f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 0) + 1, false)"))
    finally:
        await source.dispose()
        await target.dispose()
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True, help="source database URL (SQLite)")
    parser.add_argument("--to", dest="target", required=True, help="target PostgreSQL URL (empty database)")
    args = parser.parse_args()
    if args.source == args.target:
        print("Source and target are the same database.")
        return 2
    print("Copying...")
    copied = asyncio.run(migrate(args.source, args.target))
    print(f"Done: {sum(copied.values())} rows in {len(copied)} tables. Now set DATABASE_URL to the target and start the server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
