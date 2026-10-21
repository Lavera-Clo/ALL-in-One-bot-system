"""
Bring the Business Sender website's old messages (its Supabase `messages` and `users` tables,
written by vercel-webhook/app/api/webhook) into the common database, once, when switching
Meta's webhook over to this server.

    set SUPABASE_URL=https://xxxx.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=...          (Supabase -> Project Settings -> API -> service_role)
    python import_supabase_messages.py --days 21

  • Only the last --days of messages are imported (default 21, the history the platform keeps).
  • Each message goes to the business whose WhatsApp phone number id it arrived on. Numbers with
    no business on this server are listed and skipped: create the business (or connect its
    number) first, then run the import again.
  • Safe to run twice: a message already here (same Meta message id) is skipped.
  • Customer names from the `users` table become the contacts' WhatsApp names.
  • Supabase is only read, never changed.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
from sqlalchemy import func, select

PAGE = 1000


async def _fetch_all(client: httpx.AsyncClient, base: str, key: str, table: str, params: Dict) -> List[Dict]:
    rows, offset = [], 0
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    while True:
        r = await client.get(f"{base}/rest/v1/{table}", headers=headers,
                             params={**params, "limit": PAGE, "offset": offset})
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def _when(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        dt = datetime.fromisoformat(v[:26] + v[-6:]) if len(v) > 32 else datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def import_messages(supabase_url: str, service_key: str, days: int = 21,
                          client: Optional[httpx.AsyncClient] = None, quiet: bool = False) -> Dict:
    from database import AsyncSessionLocal, init_db
    from models import ChatHistory, Conversation, Tenant

    say = (lambda *a: None) if quiet else print
    await init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        messages = await _fetch_all(client, supabase_url.rstrip("/"), service_key, "messages",
                                    {"select": "*", "created_at": f"gte.{since}", "order": "created_at.asc"})
        users = await _fetch_all(client, supabase_url.rstrip("/"), service_key, "users", {"select": "phone_number,name"})
    finally:
        if own:
            await client.aclose()
    names = {u.get("phone_number"): u.get("name") for u in users if u.get("name") and u.get("name") != "Unknown"}

    report = {"imported": 0, "already_here": 0, "no_business": {}, "contacts": 0}
    async with AsyncSessionLocal() as db:
        tenants = {t.whatsapp_phone_number_id: t for t in (await db.execute(select(Tenant))).scalars().all()}
        touched = set()
        for m in messages:
            tenant = tenants.get(str(m.get("business_phone_id") or ""))
            phone = "".join(ch for ch in str(m.get("phone_number") or "") if ch.isdigit())
            if not phone:
                continue
            if tenant is None:
                key = str(m.get("business_phone_id") or "unknown")
                report["no_business"][key] = report["no_business"].get(key, 0) + 1
                continue
            wa_id = m.get("wa_message_id")
            if wa_id and (await db.execute(select(ChatHistory.id).where(
                    ChatHistory.tenant_id == tenant.id, ChatHistory.wa_message_id == wa_id))).first():
                report["already_here"] += 1
                continue
            incoming = (m.get("direction") or "incoming") == "incoming"
            when = _when(m.get("created_at"))
            db.add(ChatHistory(tenant_id=tenant.id, customer_phone_number=phone, role="user" if incoming else "assistant",
                               content=m.get("message_text") or f"[{m.get('message_type') or 'message'}]",
                               sender="customer" if incoming else "agent", message_type=m.get("message_type") or "text",
                               status="RECEIVED" if incoming else "SENT", wa_message_id=wa_id, created_at=when))
            touched.add((tenant.id, phone))
            report["imported"] += 1
        await db.flush()

        for tenant_id, phone in touched:
            conv = (await db.execute(select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.customer_phone == phone))).scalar_one_or_none()
            if conv is None:
                conv = Conversation(tenant_id=tenant_id, customer_phone=phone)
                db.add(conv)
                report["contacts"] += 1
            if names.get(phone) and not conv.customer_name:
                conv.customer_name = names[phone]
            count, last = (await db.execute(select(func.count(ChatHistory.id), func.max(ChatHistory.created_at)).where(
                ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone))).one()
            conv.message_count = count
            conv.last_message_at = last
        await db.commit()

    say(f"Imported {report['imported']} messages ({report['already_here']} were already here), "
        f"{report['contacts']} new contacts.")
    for phone_id, n in report["no_business"].items():
        say(f"  Skipped {n} messages for WhatsApp number id {phone_id}: no business uses it on this server yet.")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=21)
    args = parser.parse_args()
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first (see the top of this file).")
        return 2
    asyncio.run(import_messages(url, key, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
