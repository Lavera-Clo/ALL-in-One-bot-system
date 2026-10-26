"""
The live inbox: every WhatsApp conversation as the business sees it, with a person able to
reply and to switch the bot off chat by chat.

    customer writes ──► webhook ──► record_incoming()          (always, bot on or off)
                                    bot may reply? ── no ──► nothing sent; a person replies
                                          │ yes
                                          ▼
                         bot answers ──► WhatsApp send ──► on_whatsapp_sent() records it
    person replies in the inbox ──► send_as_agent() ──► WhatsApp send ──► recorded as 'agent'

Outgoing messages are recorded in ONE place, a listener on whatsapp_client's send functions,
so bot answers, order updates, cart reminders, broadcasts and inbox replies all appear without
each of the many send calls having to remember to save itself.

Who may reply: the bot answers only when the business's master switch (Tenant.bot_enabled)
AND the chat's own switch (Conversation.bot_enabled) are on. The manager's own number is always
served, so CONFIRM / REJECT order commands keep working while the bot is paused.
"""

import contextvars
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
from models import ChatHistory, Conversation, Tenant, utc_now
from realtime import inbox_manager

logger = logging.getLogger("uvicorn.error")

MAX_REPLY_CHARS = 4096          # WhatsApp's limit for a text message
DEFAULT_PAGE = 300              # messages returned for one chat

# Set while a person's reply (or a retry) is being sent, so the send listener records it as
# theirs instead of the bot's, and hands back the row it wrote.
_outgoing: contextvars.ContextVar[Optional[Dict]] = contextvars.ContextVar("inbox_outgoing", default=None)


def digits(phone: Optional[str]) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _ms(when: Optional[datetime]) -> int:
    """Epoch milliseconds. SQLite returns naive datetimes; they are always written as UTC."""
    if not when:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


def describe_incoming(message: dict) -> Tuple[str, str]:
    """(text the inbox shows, message type) for one message from Meta's webhook."""
    kind = message.get("type") or "unknown"
    from media_service import describe as describe_media
    special = describe_media(message)
    if special is not None:
        return special, kind
    if kind == "text":
        return (message.get("text") or {}).get("body", ""), kind
    if kind == "interactive":
        i = message.get("interactive") or {}
        title = (i.get("button_reply") or {}).get("title") or (i.get("list_reply") or {}).get("title")
        return title or "[Tapped an option]", kind
    if kind == "button":
        return (message.get("button") or {}).get("text") or "[Tapped a button]", kind
    if kind == "image":
        return f"[Image] {(message.get('image') or {}).get('caption') or ''}".strip(), kind
    if kind == "video":
        return f"[Video] {(message.get('video') or {}).get('caption') or ''}".strip(), kind
    if kind == "audio":
        return "[Voice note]", kind
    if kind == "document":
        return f"[Document] {(message.get('document') or {}).get('filename') or ''}".strip(), kind
    if kind == "location":
        loc = message.get("location") or {}
        return f"[Location] {loc.get('latitude')}, {loc.get('longitude')}", kind
    if kind == "sticker":
        return "[Sticker]", kind
    if kind == "contacts":
        return "[Contact shared]", kind
    return f"[{kind} message]", kind


# ---------------------------------------------------------------------------
# Conversations and switches
# ---------------------------------------------------------------------------

async def get_conversation(db: AsyncSession, tenant_id: int, phone: str) -> Optional[Conversation]:
    stmt = select(Conversation).where(Conversation.tenant_id == tenant_id, Conversation.customer_phone == phone)
    return (await db.execute(stmt)).scalar_one_or_none()


async def touch_conversation(db: AsyncSession, tenant_id: int, phone: str, name: Optional[str] = None) -> Conversation:
    """The chat for this customer, created on first contact; keeps their WhatsApp name current."""
    conv = await get_conversation(db, tenant_id, phone)
    if conv is None:
        conv = Conversation(tenant_id=tenant_id, customer_phone=phone, customer_name=(name or None))
        db.add(conv)
        await db.flush()
    elif name and name != conv.customer_name:
        conv.customer_name = name
    return conv


def bot_may_reply(tenant: Tenant, conv: Optional[Conversation]) -> bool:
    return bool(tenant.bot_enabled) and (conv is None or bool(conv.bot_enabled))


def is_manager(tenant: Tenant, phone: str) -> bool:
    manager = digits(tenant.human_handoff_number)
    return bool(manager) and digits(phone) == manager


async def set_chat_bot(db: AsyncSession, tenant_id: int, phone: str, enabled: bool) -> Conversation:
    conv = await touch_conversation(db, tenant_id, phone)
    conv.bot_enabled = bool(enabled)
    if enabled:
        conv.needs_human = False       # handing the chat back to the bot closes the hand-over
    await db.commit()
    await announce(tenant_id, phone)
    return conv


async def set_business_bot(db: AsyncSession, tenant: Tenant, enabled: bool) -> None:
    tenant.bot_enabled = bool(enabled)
    await db.commit()
    await announce(tenant.id, None, kind="session.changed")


async def mark_needs_human(db: AsyncSession, tenant_id: int, phone: str) -> None:
    conv = await touch_conversation(db, tenant_id, phone)
    if not conv.needs_human:
        conv.needs_human = True
        await db.commit()
        await announce(tenant_id, phone)


async def announce(tenant_id: int, phone: Optional[str], kind: str = "chats.changed", incoming: bool = False) -> None:
    """Tell every open inbox of this business that something changed. Never raises."""
    try:
        await inbox_manager.broadcast(tenant_id, {"type": kind, "phone": phone, "incoming": incoming})
    except Exception:  # noqa: BLE001
        logger.exception("Inbox broadcast failed")


# ---------------------------------------------------------------------------
# Recording messages
# ---------------------------------------------------------------------------

async def record_incoming(db: AsyncSession, tenant_id: int, phone: str, text: str, kind: str = "text",
                          wa_message_id: Optional[str] = None, name: Optional[str] = None) -> ChatHistory:
    conv = await touch_conversation(db, tenant_id, phone, name)
    row = ChatHistory(tenant_id=tenant_id, customer_phone_number=phone, role="user", content=text or f"[{kind}]",
                      sender="customer", message_type=kind, status="RECEIVED", wa_message_id=wa_message_id)
    db.add(row)
    conv.last_message_at = utc_now()
    conv.message_count = (conv.message_count or 0) + 1
    await db.commit()
    await db.refresh(row)
    await announce(tenant_id, phone, incoming=True)
    return row


async def update_incoming_text(db: AsyncSession, row: ChatHistory, text: str) -> None:
    """Replace a placeholder ("[Voice note]") once the real words are known."""
    row.content = text
    await db.commit()
    await announce(row.tenant_id, row.customer_phone_number)


async def on_whatsapp_sent(phone_number_id: str, recipient: str, text: str, kind: str, ok: bool,
                           error: Optional[str], wa_message_id: Optional[str]) -> None:
    """Send listener (whatsapp_client.add_send_listener): records every message sent to a customer."""
    ctx = _outgoing.get()
    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id))).scalar_one_or_none()
        if tenant is None:
            return
        # Hand-over alerts to the manager are notes for staff, not part of the customer's chat.
        if ctx is None and is_manager(tenant, recipient):
            return

        if ctx and ctx.get("retry_of"):
            row = await db.get(ChatHistory, ctx["retry_of"])
            if row is not None and row.tenant_id == tenant.id:
                row.status = "SENT" if ok else "FAILED"
                row.error = None if ok else error
                row.wa_message_id = wa_message_id or row.wa_message_id
                await db.commit()
                ctx["message_id"] = row.id
                await announce(tenant.id, recipient)
            return

        sender = (ctx or {}).get("sender", "bot")
        conv = await touch_conversation(db, tenant.id, recipient)
        row = ChatHistory(tenant_id=tenant.id, customer_phone_number=recipient, role="assistant",
                          content=text or f"[{kind}]", sender=sender, message_type=kind,
                          status="SENT" if ok else "FAILED", error=None if ok else error, wa_message_id=wa_message_id,
                          client_msg_id=(ctx or {}).get("client_msg_id"))
        media = (ctx or {}).get("media") or {}
        row.media_url, row.media_mime, row.media_name = media.get("url"), media.get("mime"), media.get("name")
        db.add(row)
        conv.last_message_at = utc_now()
        conv.message_count = (conv.message_count or 0) + 1
        if sender == "agent" and ok:
            conv.needs_human = False   # a person has answered
        await db.commit()
        if ctx is not None:
            ctx["message_id"] = row.id
        await announce(tenant.id, recipient)


async def send_as_agent(tenant: Tenant, phone: str, text: str, retry_of: Optional[int] = None,
                        client_msg_id: Optional[str] = None) -> Dict:
    """
    Send a reply typed by a person in the inbox. Returns {"message_id", "ok", "error"}; a refusal
    by WhatsApp (outside the 24-hour window, say) comes back as ok=False with the reason, and the
    message is kept, marked FAILED, so it can be retried.
    """
    from whatsapp_client import describe_send_error, send_whatsapp_message

    # client_msg_id: the sending device's own id for this message (the Android app), so the
    # app recognises the saved copy when it syncs.
    ctx = {"sender": "agent", "retry_of": retry_of, "client_msg_id": client_msg_id}
    token = _outgoing.set(ctx)
    try:
        await send_whatsapp_message(phone_number_id=tenant.whatsapp_phone_number_id, recipient_phone=phone,
                                    message_text=text, meta_access_token=tenant.meta_access_token)
        return {"message_id": ctx.get("message_id"), "ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 — already recorded as FAILED by the listener
        return {"message_id": ctx.get("message_id"), "ok": False, "error": describe_send_error(exc)}
    finally:
        _outgoing.reset(token)


async def send_other_as_agent(tenant: Tenant, phone: str, send, media: Optional[Dict] = None,
                              client_msg_id: Optional[str] = None) -> Dict:
    """
    Like send_as_agent, for anything that isn't plain text (media_service.py): `send` is an async
    callable doing the WhatsApp send; `media` ({"url", "mime", "name"}) is kept on the inbox row.
    """
    from whatsapp_client import describe_send_error

    ctx = {"sender": "agent", "client_msg_id": client_msg_id, "media": media}
    token = _outgoing.set(ctx)
    try:
        await send()
        return {"message_id": ctx.get("message_id"), "ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 - already recorded as FAILED by the listener
        return {"message_id": ctx.get("message_id"), "ok": False, "error": describe_send_error(exc)}
    finally:
        _outgoing.reset(token)


# ---------------------------------------------------------------------------
# Reading the inbox
# ---------------------------------------------------------------------------

def _sender(row: ChatHistory) -> str:
    return row.sender or ("customer" if row.role == "user" else "bot")


def message_payload(row: ChatHistory) -> Dict:
    sender = _sender(row)
    return {
        "id": str(row.id),
        "phone": row.customer_phone_number,
        "text": row.content,
        "isOutgoing": sender != "customer",
        "sender": sender,                       # customer | bot | agent
        "type": row.message_type or "text",
        "timestamp": _ms(row.created_at),
        "status": row.status or ("SENT" if sender != "customer" else "RECEIVED"),
        "error": row.error,
        "label": None,
        "clientMsgId": row.client_msg_id,
        "waMessageId": row.wa_message_id,      # Meta's id: how a phone matches messages it already has
        "media": ({"url": row.media_url, "mime": row.media_mime, "name": row.media_name}
                  if row.media_url else None),   # photo / video / audio / document / sticker
    }


async def list_chats(db: AsyncSession, tenant: Tenant) -> List[Dict]:
    """One entry per customer, newest first, with the last message and the chat's switches."""
    last_ids = (select(func.max(ChatHistory.id).label("mid"))
                .where(ChatHistory.tenant_id == tenant.id)
                .group_by(ChatHistory.customer_phone_number)
                .subquery())
    rows = (await db.execute(select(ChatHistory).join(last_ids, ChatHistory.id == last_ids.c.mid))).scalars().all()
    convs = {c.customer_phone: c for c in (await db.execute(
        select(Conversation).where(Conversation.tenant_id == tenant.id))).scalars().all()}

    chats = []
    for row in rows:
        conv = convs.get(row.customer_phone_number)
        sender = _sender(row)
        needs_human = bool(conv and conv.needs_human)
        chats.append({
            "phone": row.customer_phone_number,
            "name": conv.display_name if conv else "",
            "lastText": row.content,
            "lastTime": _ms(row.created_at),
            "lastOutgoing": sender != "customer",
            "lastSender": sender,
            "lastStatus": row.status,
            "label": "HANDOFF" if needs_human else None,
            "needsHuman": needs_human,
            "botEnabled": conv.bot_enabled if conv else True,
            "botActive": bot_may_reply(tenant, conv),
        })
    chats.sort(key=lambda c: c["lastTime"], reverse=True)
    return chats


async def chat_messages(db: AsyncSession, tenant_id: int, phone: str, limit: int = DEFAULT_PAGE) -> List[Dict]:
    stmt = (select(ChatHistory)
            .where(ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone)
            .order_by(ChatHistory.id.desc())
            .limit(max(1, min(limit, 1000))))
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return [message_payload(r) for r in rows]
