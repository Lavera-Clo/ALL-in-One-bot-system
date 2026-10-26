"""
Everything WhatsApp can share, both ways: photos, videos, voice notes and audio, documents,
stickers, locations and contact cards.

Received:  the file is downloaded from Meta (its own link expires) and kept here, so the inbox
           can show the photo, play the video or voice note and open the document.
           Locations and contacts are kept as readable text plus their details.
Sent:      a person in the inbox (or the bot) sends any of them; files are uploaded to Meta first.

Files live under MEDIA_DIR and are served at /media/<random name>. The names are 32 random hex
characters - unguessable - because a browser <img>/<video> tag can't send a sign-in header, and
Meta itself must be able to fetch a link we send.
"""

import logging
import mimetypes
import os
import secrets
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import ChatHistory, Tenant

logger = logging.getLogger("uvicorn.error")

MEDIA_DIR = os.getenv("MEDIA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_store"))
os.makedirs(MEDIA_DIR, exist_ok=True)

# What WhatsApp accepts, and its size limits (Cloud API "supported media types").
ALLOWED: Dict[str, Tuple[set, int]] = {
    "image": ({"image/jpeg", "image/png"}, 5 * 1024 * 1024),
    "video": ({"video/mp4", "video/3gpp"}, 16 * 1024 * 1024),
    "audio": ({"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"}, 16 * 1024 * 1024),
    "sticker": ({"image/webp"}, 500 * 1024),
    "document": (set(), 100 * 1024 * 1024),          # any type
}
MEDIA_KINDS = ("image", "video", "audio", "document", "sticker")

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp",
        "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/amr": ".amr",
        "application/pdf": ".pdf"}


def kind_for(mime: str, filename: str = "") -> str:
    """Which WhatsApp message type a file goes as."""
    mime = (mime or "").split(";")[0].strip().lower()
    for kind in ("image", "video", "audio", "sticker"):
        if mime in ALLOWED[kind][0]:
            return kind
    return "document"


def check_file(kind: str, mime: str, size: int) -> Optional[str]:
    """A reason WhatsApp would refuse this file, or None."""
    if kind not in ALLOWED:
        return f"Unknown media type: {kind}"
    types, limit = ALLOWED[kind]
    if types and (mime or "").split(";")[0].lower() not in types:
        return f"WhatsApp can't send a {mime or 'file of this type'} as {kind}. Allowed: {', '.join(sorted(types))}."
    if size > limit:
        return f"Too big for WhatsApp: {size / 1048576:.1f} MB (the limit for {kind} is {limit / 1048576:.0f} MB)."
    if size == 0:
        return "The file is empty."
    return None


def save_file(data: bytes, mime: str, filename: str = "") -> str:
    """Keep a copy; returns its public URL."""
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    ext = _EXT.get(mime) or os.path.splitext(filename or "")[1][:10] or mimetypes.guess_extension(mime) or ".bin"
    name = secrets.token_hex(16) + ext
    with open(os.path.join(MEDIA_DIR, name), "wb") as f:
        f.write(data)
    return f"{settings.BASE_URL.rstrip('/')}/media/{name}"


# --- received -----------------------------------------------------------------------------

def describe(message: dict) -> Optional[str]:
    """Inbox text for the message types the plain description doesn't cover well; None = use it."""
    kind = message.get("type")
    if kind == "location":
        loc = message.get("location") or {}
        place = " - ".join(x for x in (loc.get("name"), loc.get("address")) if x)
        maps = f"https://maps.google.com/?q={loc.get('latitude')},{loc.get('longitude')}"
        return f"[Location] {place}\n{maps}".replace("] \n", "]\n")
    if kind == "contacts":
        cards = []
        for c in message.get("contacts") or []:
            name = (c.get("name") or {}).get("formatted_name") or "Contact"
            phones = ", ".join(p.get("phone", "") for p in c.get("phones") or [] if p.get("phone"))
            cards.append(f"{name} {phones}".strip())
        return "[Contact] " + "; ".join(cards) if cards else "[Contact shared]"
    if kind == "reaction":
        emoji = (message.get("reaction") or {}).get("emoji")
        return f"[Reacted {emoji}]" if emoji else "[Removed a reaction]"
    if kind == "order":
        items = (message.get("order") or {}).get("product_items") or []
        count = sum(int(i.get("quantity") or 0) for i in items)
        note = (message.get("order") or {}).get("text") or ""
        return f"[Cart from the catalogue] {count} item(s)" + (f"\n{note}" if note else "")
    return None


async def store_incoming(db: AsyncSession, tenant: Tenant, row: ChatHistory, message: dict) -> None:
    """Download a received photo/video/audio/document/sticker and attach the copy to its inbox row."""
    kind = message.get("type")
    if kind not in MEDIA_KINDS:
        return
    media = message.get(kind) or {}
    media_id = media.get("id")
    if not media_id:
        return
    from whatsapp_client import download_whatsapp_media
    try:
        data = await download_whatsapp_media(media_id, tenant.meta_access_token)
    except Exception as exc:  # noqa: BLE001 - the message is saved anyway; only the file is missing
        logger.warning(f"[media] download failed: {exc}")
        return
    if not data:
        return
    mime = media.get("mime_type") or "application/octet-stream"
    row.media_url = save_file(data, mime, media.get("filename") or "")
    row.media_mime = mime.split(";")[0]
    row.media_name = media.get("filename") or None
    await db.commit()
    from inbox_service import announce
    await announce(tenant.id, row.customer_phone_number)


def payload(row: ChatHistory) -> Optional[dict]:
    if not row.media_url:
        return None
    return {"url": row.media_url, "mime": row.media_mime, "name": row.media_name}
