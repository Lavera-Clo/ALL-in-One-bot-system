"""
The shared database: one place for every business's chats, used by the website, the Android
app and the bot alike.

    Meta ──webhook──► this server ──► database ◄── website (Business Sender, AI bot sign-in)
                                         ▲
                                         └──────── Android app (sync below)

What's kept:
  • Messages, for each business's `history_days` (21 by default), then deleted by the daily
    cleanup. Before deleting, they're counted into DailyMessageStat, kept for good.
  • Contacts (Conversation rows) are kept: number, WhatsApp name, the name the business gave
    them, notes, first contact, last message, message count.

How devices stay in step:
  Every message and contact carries `changed_us`, a stamp that only grows, renewed on every
  change (new message, delivery tick, retry, contact rename). A device asks for "everything
  changed after the last stamp I have"; the answer includes a new cursor. Because two saves can
  finish in a different order than they were stamped, a device that has caught up asks again
  from SYNC_OVERLAP_US before its cursor and ignores what it already has (ids never repeat).

Messages a phone sent by itself (photos, or anything sent with its own WhatsApp token) are
recorded with record_device_message(); the device's own message id makes a repeated upload
harmless.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from inbox_service import _ms, announce, digits, message_payload, touch_conversation
from models import ChatHistory, Conversation, DailyMessageStat, Tenant, utc_now

logger = logging.getLogger("uvicorn.error")

SYNC_OVERLAP_US = 10_000_000          # 10 s: what a caught-up device re-asks for
SYNC_PAGE = 500
HISTORY_DAYS_ALLOWED = (7, 14, 21, 30, 60, 90, 180, 365)
DEFAULT_HISTORY_DAYS = 21

# Meta's delivery receipts, in the order they happen. A late "delivered" never undoes "read".
_STATUS_RANK = {"SENDING": 0, "SENT": 1, "DELIVERED": 2, "READ": 3}
_META_STATUS = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def contact_payload(conv: Conversation) -> Dict:
    return {
        "phone": conv.customer_phone,
        "name": conv.display_name,
        "whatsappName": conv.customer_name or "",
        "savedName": conv.saved_name or "",
        "notes": conv.notes or "",
        "firstContact": _ms(conv.created_at),
        "lastMessageAt": _ms(conv.last_message_at),
        "messageCount": conv.message_count or 0,
        "botEnabled": bool(conv.bot_enabled),
        "needsHuman": bool(conv.needs_human),
        "changedAt": conv.changed_us or 0,
    }


async def list_contacts(db: AsyncSession, tenant_id: int, query: str = "", limit: int = 500) -> List[Dict]:
    stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
    q = (query or "").strip().lower()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(func.lower(Conversation.saved_name).like(like),
                              func.lower(Conversation.customer_name).like(like),
                              Conversation.customer_phone.like(f"%{digits(q) or q}%")))
    stmt = stmt.order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc()).limit(max(1, min(limit, 2000)))
    return [contact_payload(c) for c in (await db.execute(stmt)).scalars().all()]


async def update_contact(db: AsyncSession, tenant_id: int, phone: str, saved_name: Optional[str] = None,
                         notes: Optional[str] = None) -> Conversation:
    conv = await touch_conversation(db, tenant_id, phone)
    if saved_name is not None:
        conv.saved_name = " ".join(saved_name.split())[:150] or None
    if notes is not None:
        conv.notes = notes.strip()[:5000] or None
    conv.updated_at = utc_now()          # always a real change, so the stamp renews
    await db.commit()
    await announce(tenant_id, phone)
    return conv


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def sync_changes(db: AsyncSession, tenant: Tenant, since: int = 0, limit: int = SYNC_PAGE) -> Dict:
    """
    Messages and contacts changed after `since`, oldest change first.
    `cursor` is the stamp to ask from next; `hasMore` means ask again straight away.
    """
    limit = max(1, min(limit, 2000))
    since = max(0, int(since or 0))
    msgs = list((await db.execute(
        select(ChatHistory).where(ChatHistory.tenant_id == tenant.id, ChatHistory.changed_us > since)
        .order_by(ChatHistory.changed_us, ChatHistory.id).limit(limit + 1))).scalars().all())
    has_more = len(msgs) > limit
    msgs = msgs[:limit]
    # Contacts up to the same point, so a page never shows a message before its contact.
    upto = msgs[-1].changed_us if has_more else None
    contact_stmt = select(Conversation).where(Conversation.tenant_id == tenant.id, Conversation.changed_us > since)
    if upto is not None:
        contact_stmt = contact_stmt.where(Conversation.changed_us <= upto)
    contacts = list((await db.execute(contact_stmt.order_by(Conversation.changed_us))).scalars().all())

    stamps = [m.changed_us or 0 for m in msgs] + [c.changed_us or 0 for c in contacts]
    cursor = max(stamps) if stamps else since
    if has_more and upto is not None:
        cursor = upto
    return {
        "cursor": cursor,
        "hasMore": has_more,
        "overlapUs": SYNC_OVERLAP_US,
        "historyDays": tenant.history_days or DEFAULT_HISTORY_DAYS,
        "botEnabled": bool(tenant.bot_enabled),
        "messages": [{**message_payload(m), "changedAt": m.changed_us or 0} for m in msgs],
        "contacts": [contact_payload(c) for c in contacts],
    }


async def find_by_client_id(db: AsyncSession, tenant_id: int, client_msg_id: Optional[str]) -> Optional[ChatHistory]:
    if not client_msg_id:
        return None
    return (await db.execute(select(ChatHistory).where(
        ChatHistory.tenant_id == tenant_id, ChatHistory.client_msg_id == client_msg_id))).scalars().first()


async def record_device_message(db: AsyncSession, tenant: Tenant, phone: str, text: str, kind: str = "text",
                                wa_message_id: Optional[str] = None, client_msg_id: Optional[str] = None,
                                status: str = "SENT", error: Optional[str] = None,
                                sent_at_ms: Optional[int] = None) -> ChatHistory:
    """A message a phone sent by itself (its own WhatsApp token). Saved once, however often uploaded."""
    existing = await find_by_client_id(db, tenant.id, client_msg_id)
    if existing is None and wa_message_id:
        existing = (await db.execute(select(ChatHistory).where(
            ChatHistory.tenant_id == tenant.id, ChatHistory.wa_message_id == wa_message_id))).scalars().first()
    status = status if status in ("SENDING", "SENT", "DELIVERED", "READ", "FAILED") else "SENT"
    if existing is not None:
        # The upload again, maybe with news (now accepted by Meta, or failed).
        changed = False
        if wa_message_id and not existing.wa_message_id:
            existing.wa_message_id, changed = wa_message_id, True
        if status != existing.status and (status == "FAILED" or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(existing.status or "", 0)):
            existing.status, existing.error, changed = status, error if status == "FAILED" else None, True
        if changed:
            await db.commit()
            await announce(tenant.id, phone)
        return existing

    when = utc_now()
    if sent_at_ms:
        candidate = datetime.fromtimestamp(sent_at_ms / 1000, tz=timezone.utc)
        if abs((candidate - when).total_seconds()) < 7 * 86400:     # a sane device clock only
            when = candidate
    conv = await touch_conversation(db, tenant.id, phone)
    row = ChatHistory(tenant_id=tenant.id, customer_phone_number=phone, role="assistant", content=text or f"[{kind}]",
                      sender="agent", message_type=kind, status=status, error=error if status == "FAILED" else None,
                      wa_message_id=wa_message_id, client_msg_id=client_msg_id, created_at=when)
    db.add(row)
    conv.last_message_at = max(filter(None, [_aware(conv.last_message_at), when]))
    conv.message_count = (conv.message_count or 0) + 1
    if status != "FAILED":
        conv.needs_human = False
    await db.commit()
    await db.refresh(row)
    await announce(tenant.id, phone)
    if status != "FAILED" and kind == "text":
        from learning_service import learn_from_chat_later
        learn_from_chat_later(tenant.id, phone)     # a reply from the phone app teaches too
    return row


def _aware(when: Optional[datetime]) -> Optional[datetime]:
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


# ---------------------------------------------------------------------------
# Delivery and read receipts from Meta
# ---------------------------------------------------------------------------

async def apply_statuses(db: AsyncSession, tenant: Tenant, statuses: List[dict]) -> int:
    """Meta's 'sent / delivered / read / failed' receipts, onto the messages they belong to."""
    updated, phones = 0, set()
    for st in statuses or []:
        wa_id, new = st.get("id"), _META_STATUS.get((st.get("status") or "").lower())
        if not wa_id or not new:
            continue
        row = (await db.execute(select(ChatHistory).where(
            ChatHistory.tenant_id == tenant.id, ChatHistory.wa_message_id == wa_id))).scalars().first()
        if row is None or row.sender == "customer":
            continue
        if new == "FAILED":
            err = (st.get("errors") or [{}])[0]
            detail = err.get("message") or err.get("title") or "WhatsApp could not deliver this message."
            if err.get("code") in (131047, 131026):
                detail = "Outside the 24-hour window: WhatsApp needs an approved template to reach this customer now."
            if row.status != "FAILED" or row.error != detail:
                row.status, row.error = "FAILED", detail
                updated += 1
                phones.add(row.customer_phone_number)
        elif row.status != "FAILED" and _STATUS_RANK.get(new, 0) > _STATUS_RANK.get(row.status or "", 0):
            row.status = new
            updated += 1
            phones.add(row.customer_phone_number)
    if updated:
        await db.commit()
        for phone in phones:
            await announce(tenant.id, phone)
    return updated


# ---------------------------------------------------------------------------
# Keeping only recent history
# ---------------------------------------------------------------------------

async def cleanup_old_messages(db: AsyncSession, now: Optional[datetime] = None) -> Dict[int, int]:
    """
    Deletes each business's messages older than its history_days, after counting them into
    DailyMessageStat. Contacts stay. Returns {tenant_id: messages deleted}.
    """
    now = now or datetime.now(timezone.utc)
    deleted: Dict[int, int] = {}
    tenants = (await db.execute(select(Tenant.id, Tenant.history_days))).all()
    for tenant_id, days in tenants:
        days = days or DEFAULT_HISTORY_DAYS
        cutoff = now - timedelta(days=days)
        # SQLite stores naive UTC; compare like with like.
        cutoff_cmp = cutoff.replace(tzinfo=None) if _naive_storage(db) else cutoff
        old = (await db.execute(select(ChatHistory.created_at, ChatHistory.sender, ChatHistory.role).where(
            ChatHistory.tenant_id == tenant_id, ChatHistory.created_at < cutoff_cmp))).all()
        if not old:
            continue
        counts: Dict[str, List[int]] = {}
        for created_at, sender, role in old:
            day = (_aware(created_at) or now).strftime("%Y-%m-%d")
            incoming = (sender or ("customer" if role == "user" else "bot")) == "customer"
            counts.setdefault(day, [0, 0])[0 if incoming else 1] += 1
        existing = {s.day: s for s in (await db.execute(select(DailyMessageStat).where(
            DailyMessageStat.tenant_id == tenant_id, DailyMessageStat.day.in_(list(counts))))).scalars().all()}
        for day, (inc, out) in counts.items():
            stat = existing.get(day) or DailyMessageStat(tenant_id=tenant_id, day=day, incoming=0, outgoing=0)
            stat.incoming += inc
            stat.outgoing += out
            db.add(stat)
        result = await db.execute(delete(ChatHistory).where(
            ChatHistory.tenant_id == tenant_id, ChatHistory.created_at < cutoff_cmp))
        await db.commit()
        deleted[tenant_id] = result.rowcount or len(old)
        logger.info(f"History cleanup: tenant {tenant_id} - deleted {deleted[tenant_id]} messages older than {days} days.")
    return deleted


def _naive_storage(db: AsyncSession) -> bool:
    return db.bind.dialect.name == "sqlite" if db.bind is not None else True


async def message_stats(db: AsyncSession, tenant_id: int, days: int = 30) -> List[Dict]:
    """Messages per day for the last `days`: kept counts for cleaned-up days, live counts for the rest."""
    days = max(1, min(days, 730))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    out = {(start + timedelta(days=i)).isoformat(): {"incoming": 0, "outgoing": 0} for i in range(days)}
    for s in (await db.execute(select(DailyMessageStat).where(
            DailyMessageStat.tenant_id == tenant_id, DailyMessageStat.day >= start.isoformat()))).scalars().all():
        if s.day in out:
            out[s.day]["incoming"] += s.incoming
            out[s.day]["outgoing"] += s.outgoing
    since = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    since_cmp = since.replace(tzinfo=None) if _naive_storage(db) else since
    for created_at, sender, role in (await db.execute(select(ChatHistory.created_at, ChatHistory.sender, ChatHistory.role)
                                                       .where(ChatHistory.tenant_id == tenant_id, ChatHistory.created_at >= since_cmp))).all():
        day = _aware(created_at).date().isoformat()
        if day in out:
            incoming = (sender or ("customer" if role == "user" else "bot")) == "customer"
            out[day]["incoming" if incoming else "outgoing"] += 1
    return [{"day": d, **v} for d, v in out.items()]
