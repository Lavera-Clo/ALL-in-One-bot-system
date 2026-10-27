"""
Bookings made in chat: a table, an appointment, a hall visit.

The bot cannot confirm a booking - only a person who can see the diary can. So it collects what the
team needs and this module makes sure that actually reaches them, instead of the details sitting in a
chat nobody is watching:

    customer asks to book ──► bot collects service + when (+ people, name, notes)
            │                                                   (the reply model; see ai_service.py)
            ▼  a few seconds after the reply (background, fast model)
    capture()  reads the chat, and when the booking is complete
            ├─► saves it as a PENDING Booking (one open request per customer; a change updates it)
            ├─► messages the manager on WhatsApp: the details + how to answer
            └─► flags the chat "Needs you" in the inbox

    manager replies "CONFIRM BK-12" / "DECLINE BK-12" on WhatsApp   (handle_manager_command)
    or presses Confirm / Decline in the portal or the website       (PATCH /bookings/{id})
            └─► the customer is told, in their own language, and it shows in their chat

Only the manager's own number can answer by WhatsApp; anyone else typing "CONFIRM BK-12" is just a
customer typing.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select

import llm_router
from ai_service import detect_language_intent
from database import AsyncSessionLocal
from models import Booking, ChatHistory, Conversation, Tenant, utc_now

logger = logging.getLogger("uvicorn.error")

BOOKING_WORDS = re.compile(
    r"\b(book(ing|ed|s)?|reserv(e|ed|ation|ations)|appointment|appoint|schedule|a slot|time slot|table for|"
    r"(see|meet|consult|visit) (a|an|the|my)\s+(\w+\s+){0,2}(doctor|dentist|dermatolog\w*|specialist|consultant|physician|"
    r"vet|therapist|stylist|barber|lawyer|surgeon)|check[- ]?up|come in|drop in|visit|"
    r"hold (a|the) (date|hall|table))\b"
    r"|book karanna|reserve karanna|appointment ekak|table ekak|වෙන් ?කර|බුක් කර|වෙලාවක්",
    re.I,
)
OPEN_STATUSES = ("pending",)
REUSE_WINDOW = timedelta(days=3)
CAPTURE_DELAY_SECONDS = 6.0
CHAT_WINDOW = 14

EXTRACT_PROMPT = (
    "You read a WhatsApp chat between a customer and a business and decide whether the customer has asked "
    "for a booking, reservation or appointment, and whether the details are complete enough to pass to the team.\n"
    "Answer with JSON only:\n"
    '{"booking": true or false, "complete": true or false, "service": "what they want to book", '
    '"when": "the date and/or time, in the customer\'s own words", "people": whole number or null, '
    '"name": "the customer\'s name if they gave it, else empty", "notes": "anything else the team needs, short"}\n'
    "Rules:\n"
    "- booking is true only if the CUSTOMER asked to book/reserve/arrange something. Questions about prices, "
    "hours or availability are not bookings.\n"
    "- complete is true when you know WHAT they want and WHEN (a day, date or time, even rough like "
    "'tomorrow evening'). Without a when, complete is false.\n"
    "- Use only what was said. Never invent a date, a name or a number. Unknown parts stay empty.\n"
    "- Write in English even if the chat is Sinhala or Singlish; keep the customer's words for 'when'."
)


def mentions_booking(texts: List[str]) -> bool:
    return any(BOOKING_WORDS.search(t or "") for t in texts)


def clean_extraction(data) -> Optional[Dict]:
    """The fields we trust, or None when this isn't a complete booking."""
    if not isinstance(data, dict) or not data.get("booking") or not data.get("complete"):
        return None
    text = lambda v, n: " ".join(str(v or "").split())[:n]  # noqa: E731
    service, when = text(data.get("service"), 255), text(data.get("when"), 255)
    if not service or not when:
        return None
    try:
        people = int(data.get("people")) if data.get("people") not in (None, "", "null") else None
        people = people if people and 0 < people <= 10_000 else None
    except (TypeError, ValueError):
        people = None
    return {"service": service, "when": when, "people": people, "name": text(data.get("name"), 150),
            "notes": text(data.get("notes"), 1000)}


def _same_request(booking: Booking, found: Dict) -> bool:
    def words(s):
        return set(re.findall(r"\w+", (s or "").lower()))
    a, b = words(booking.when_text), words(found["when"])
    c, d = words(booking.service), words(found["service"])
    close = lambda x, y: bool(x and y) and len(x & y) / max(len(x | y), 1) >= 0.6  # noqa: E731
    return close(a, b) and close(c, d) and (booking.party_size == found["people"] or not found["people"])


# ---------------------------------------------------------------------------
# Capturing a booking from the chat
# ---------------------------------------------------------------------------

async def capture(tenant_id: int, phone: str) -> Optional[Booking]:
    """Read this customer's recent chat; save and announce a complete booking. Never raises."""
    from inbox_service import announce, digits, mark_needs_human
    from whatsapp_client import send_whatsapp_message
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None:
                return None
            rows = list((await db.execute(
                select(ChatHistory).where(ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone)
                .order_by(ChatHistory.id.desc()).limit(CHAT_WINDOW))).scalars().all())
            rows.reverse()
            customer_texts = [r.content for r in rows if (r.sender or ("customer" if r.role == "user" else "bot")) == "customer"]
            if not mentions_booking(customer_texts[-8:]) or not llm_router.role_candidates("fast"):
                return None
            transcript = "\n".join(
                f"{'Customer' if (r.sender or ('customer' if r.role == 'user' else 'bot')) == 'customer' else tenant.business_name}: "
                f"{(r.content or '')[:400]}" for r in rows)
            try:
                data, _ = await llm_router.complete_json(
                    "fast", [{"role": "system", "content": EXTRACT_PROMPT},
                             {"role": "user", "content": f"Business: {tenant.business_name}\n\nChat, oldest first:\n{transcript}"}],
                    max_tokens=300, temperature=0.0, timeout=30)
            except Exception as exc:  # noqa: BLE001 - a missed capture must never affect the chat
                logger.info(f"[booking] not captured: {exc}")
                return None
            found = clean_extraction(data)
            if not found:
                return None

            conv = (await db.execute(select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.customer_phone == phone))).scalar_one_or_none()
            name = found["name"] or (conv.display_name if conv else "") or None
            since = utc_now() - REUSE_WINDOW
            open_ones = (await db.execute(select(Booking).where(
                Booking.tenant_id == tenant_id, Booking.customer_phone == phone, Booking.status.in_(OPEN_STATUSES)
            ).order_by(Booking.id.desc()))).scalars().all()
            existing = next((b for b in open_ones if (_aware(b.created_at) or since) >= since), None)
            if existing and _same_request(existing, found):
                return None                                    # already recorded, nothing changed
            verb = "updated" if existing else "new"
            if existing:
                booking = existing
                booking.service, booking.when_text, booking.party_size = found["service"], found["when"], found["people"]
                booking.notes = found["notes"] or booking.notes
                booking.customer_name = name or booking.customer_name
            else:
                booking = Booking(tenant_id=tenant_id, customer_phone=phone, customer_name=name, service=found["service"],
                                  when_text=found["when"], party_size=found["people"], notes=found["notes"] or None)
                db.add(booking)
            await db.commit()
            await db.refresh(booking)
            await mark_needs_human(db, tenant_id, phone)
            logger.info(f"[booking] tenant {tenant_id}: {verb} booking #{booking.id} for {phone[-4:]}: {booking.service} / {booking.when_text}")

            if tenant.human_handoff_number:
                try:
                    await send_whatsapp_message(
                        phone_number_id=tenant.whatsapp_phone_number_id, recipient_phone=digits(tenant.human_handoff_number),
                        message_text=manager_alert(booking, verb), meta_access_token=tenant.meta_access_token)
                except Exception as exc:  # noqa: BLE001 - the booking is saved and shows in the inbox either way
                    logger.warning(f"[booking] manager alert failed: {exc}")
            await announce(tenant_id, phone)
            return booking
    except Exception:  # noqa: BLE001
        logger.exception("[booking] capture failed")
        return None


def _aware(when: Optional[datetime]) -> Optional[datetime]:
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


def reference(booking: Booking) -> str:
    return f"BK-{booking.id}"


def manager_alert(booking: Booking, verb: str = "new") -> str:
    who = booking.customer_name or "A customer"
    lines = [f"📅 *{'BOOKING UPDATED' if verb == 'updated' else 'NEW BOOKING REQUEST'}* ({reference(booking)})", "",
             f"{who} ({booking.customer_phone})", f"For: {booking.service}", f"When: {booking.when_text}"]
    if booking.party_size:
        lines.append(f"People: {booking.party_size}")
    if booking.notes:
        lines.append(f"Notes: {booking.notes}")
    lines += ["", f"Reply *CONFIRM {reference(booking)}* or *DECLINE {reference(booking)}*", "(the customer is told straight away)"]
    return "\n".join(lines)


_background: set = set()


def capture_later(tenant_id: int, phone: str) -> None:
    """Look for a complete booking a few seconds from now, after the reply has been sent and saved."""
    async def run():
        await asyncio.sleep(CAPTURE_DELAY_SECONDS)
        await capture(tenant_id, phone)
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------------------
# Answering a booking
# ---------------------------------------------------------------------------

_CUSTOMER_TEXT = {
    "confirmed": {
        "english": "Good news - your booking is confirmed ✅\n{service} · {when}\nWe'll see you then! If anything changes, just message us here.",
        "singlish": "Hari! Oyage booking eka confirm una ✅\n{service} · {when}\nApi balagena inna. Kisi venasak thiyenawanam methanata message ekak danna.",
        "sinhala": "සුභ ආරංචියක් - ඔබගේ වෙන්කිරීම තහවුරු කළා ✅\n{service} · {when}\nඅපි බලා සිටිනවා. යම් වෙනසක් තිබේ නම් මෙතනට පණිවිඩයක් එවන්න.",
    },
    "declined": {
        "english": "Sorry, we couldn't fit that booking in ({service}, {when}). Would another day or time work for you? Just tell us what suits you.",
        "singlish": "Sorry, ({service}, {when}) booking eka karanna bari una. Wena dawasak hari welawak hari hariyanam kiyanna, api balamu.",
        "sinhala": "සමාවෙන්න, ({service}, {when}) වෙන්කිරීම කරන්න බැරි වුණා. වෙන දවසක් හෝ වෙලාවක් ගැලපේ ද? කියන්න, අපි බලමු.",
    },
}
STATUSES = ("pending", "confirmed", "declined", "cancelled")


def customer_message(booking: Booking, status: str, language: str, custom: Optional[str] = None) -> str:
    if custom and custom.strip():
        return custom.strip()
    table = _CUSTOMER_TEXT.get(status)
    if not table:
        return ""
    return table.get(language, table["english"]).format(service=booking.service or "your booking", when=booking.when_text or "")


async def set_status(db, tenant: Tenant, booking: Booking, status: str, by: str, message: Optional[str] = None) -> Dict:
    """Confirm / decline / cancel a booking and (for confirm and decline) tell the customer. Returns the send result."""
    from inbox_service import send_as_agent
    if status not in STATUSES or status == "pending":
        raise ValueError("Choose confirmed, declined or cancelled.")
    booking.status, booking.handled_by, booking.handled_at = status, by[:150], utc_now()
    await db.commit()
    result = {"message_id": None, "ok": True, "error": None}
    if status in ("confirmed", "declined"):
        recent = list((await db.execute(
            select(ChatHistory.content).where(ChatHistory.tenant_id == tenant.id, ChatHistory.customer_phone_number == booking.customer_phone,
                                              ChatHistory.role == "user").order_by(ChatHistory.id.desc()).limit(4))).scalars().all())
        lang = detect_language_intent(" ".join(reversed(recent)), []) if recent else "english"
        text = customer_message(booking, status, lang, message)
        result = await send_as_agent(tenant, booking.customer_phone, text)
    return result


async def handle_manager_command(db, tenant: Tenant, text: str, sender_phone: str) -> Optional[str]:
    """
    "CONFIRM BK-12" / "DECLINE BK-12" typed by the manager. Returns what to tell the manager, or None
    when the text isn't a booking command - or isn't from the manager's own number.
    """
    from inbox_service import is_manager
    m = re.match(r"^\s*(confirm|decline|reject|cancel)\s+bk[- ]?(\d+)\s*$", text or "", re.I)
    if not m or not is_manager(tenant, sender_phone):
        return None
    action = "confirmed" if m.group(1).lower() == "confirm" else ("cancelled" if m.group(1).lower() == "cancel" else "declined")
    booking = await db.get(Booking, int(m.group(2)))
    if booking is None or booking.tenant_id != tenant.id:
        return f"I couldn't find booking BK-{m.group(2)}."
    if booking.status != "pending":
        return f"BK-{booking.id} is already {booking.status}."
    result = await set_status(db, tenant, booking, action, by=f"Manager ({sender_phone})")
    done = {"confirmed": "confirmed ✅", "declined": "declined", "cancelled": "cancelled"}[action]
    note = "" if result.get("ok") else f"\n⚠️ The customer could not be messaged: {result.get('error')}"
    return f"BK-{booking.id} {done}. The customer has been told.{note}" if action != "cancelled" else f"BK-{booking.id} cancelled."


def payload(b: Booking) -> Dict:
    from inbox_service import _ms
    return {"id": b.id, "reference": reference(b), "phone": b.customer_phone, "name": b.customer_name or "",
            "service": b.service or "", "when": b.when_text or "", "people": b.party_size, "notes": b.notes or "",
            "status": b.status, "handledBy": b.handled_by or "", "handledAt": _ms(b.handled_at) if b.handled_at else 0,
            "createdAt": _ms(b.created_at), "updatedAt": _ms(b.updated_at)}


async def list_bookings(db, tenant_id: int, status: Optional[str] = None, limit: int = 200) -> List[Dict]:
    stmt = select(Booking).where(Booking.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(Booking.status == status)
    stmt = stmt.order_by(Booking.id.desc()).limit(max(1, min(limit, 500)))
    return [payload(b) for b in (await db.execute(stmt)).scalars().all()]
