import functools
import inspect
import logging
import httpx
from config import settings

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Send listeners. Every message the server sends to a customer — bot answers, order updates,
# cart reminders, broadcasts, replies typed in the inbox — goes through the send functions
# below, so this is the one place the live inbox (inbox_service.py) learns about them all.
# A listener can never break a send: its errors are logged and swallowed.
# ---------------------------------------------------------------------------

_send_listeners = []


def add_send_listener(listener) -> None:
    """listener(phone_number_id, recipient, text, kind, ok, error, wa_message_id) — an async callable."""
    if listener not in _send_listeners:
        _send_listeners.append(listener)


def _summary(kind: str, a: dict) -> str:
    """The message as a person would read it in the inbox."""
    if kind == "text":
        return a.get("message_text") or ""
    if kind == "image":
        return f"[Image] {a.get('caption') or ''}".strip()
    if kind == "document":
        caption = a.get("caption") or ""
        return f"[Document] {a.get('filename') or ''}" + (f"\n{caption}" if caption else "")
    if kind == "buttons":
        titles = " · ".join(f"[{b.get('title')}]" for b in (a.get("buttons") or [])[:3])
        return f"{a.get('body_text') or ''}\n{titles}".strip()
    if kind == "voice":
        spoken = a.get("spoken_text") or ""
        return f"[Voice note] {spoken}".strip()
    if kind == "list":
        rows = [r.get("title") for s in (a.get("sections") or []) for r in s.get("rows", [])]
        return f"{a.get('body_text') or ''}\n[List: {', '.join(rows[:10])}]".strip()
    return f"[{kind}]"


def describe_send_error(exc: Exception) -> str:
    """Meta's reason for a failed send, in words an owner understands."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            err = exc.response.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        if code in (131047, 131026) or "re-engagement" in str(err.get("message", "")).lower():
            return ("Outside the 24-hour window. WhatsApp only allows an approved template message "
                    "until this customer writes to you again.")
        message = err.get("error_user_msg") or err.get("message") or exc.response.text
        return f"WhatsApp refused the message: {str(message)[:300]}"
    return f"Could not reach WhatsApp: {exc.__class__.__name__}"


async def _notify(kind: str, a: dict, ok: bool, error: str = None, result=None) -> None:
    wa_id = None
    if isinstance(result, dict):
        wa_id = ((result.get("messages") or [{}])[0] or {}).get("id")
    for listener in list(_send_listeners):
        try:
            await listener(a.get("phone_number_id"), a.get("recipient_phone"), _summary(kind, a), kind, ok, error, wa_id)
        except Exception:  # noqa: BLE001 — a listener must never break sending
            logger.exception("WhatsApp send listener failed")


def _reported(kind: str):
    """Reports every call of the wrapped send function to the listeners, success or failure."""
    def wrap(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def send(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                await _notify(kind, bound.arguments, ok=False, error=describe_send_error(exc))
                raise
            await _notify(kind, bound.arguments, ok=True, result=result)
            return result
        return send
    return wrap

class WhatsAppCredentialsError(ValueError):
    """Meta rejected a Phone Number ID / access token pair. The message is safe to show the owner."""


async def verify_whatsapp_number(phone_number_id: str, meta_access_token: str) -> dict:
    """
    Ask Meta whether this access token can use this phone number, before a business switches its
    bot over to it. Returns {"display_phone_number", "verified_name"}; raises
    WhatsAppCredentialsError with Meta's own explanation when they don't work together.
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}"
    params = {"fields": "display_phone_number,verified_name"}
    headers = {"Authorization": f"Bearer {meta_access_token}"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, headers=headers, timeout=15.0)
        except httpx.HTTPError as e:
            raise WhatsAppCredentialsError(f"Could not reach Meta to check the number ({e.__class__.__name__}). Try again.")
    if response.status_code != 200:
        try:
            message = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            message = response.text
        raise WhatsAppCredentialsError(f"Meta did not accept these details: {message[:300]}")
    data = response.json()
    return {"display_phone_number": data.get("display_phone_number"), "verified_name": data.get("verified_name")}


async def download_whatsapp_media(media_id: str, meta_access_token: str) -> bytes:
    """
    Downloads media (like voice notes) from WhatsApp Graph API given a media ID.
    Returns the binary data.
    """
    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Cannot download media {media_id} without real token.")
        return b""

    # 1. Get the media URL from the media ID
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{media_id}"
    headers = {"Authorization": f"Bearer {meta_access_token}"}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=10.0)
            response.raise_for_status()
            res_data = response.json()
            media_url = res_data.get("url")
            
            if not media_url:
                logger.error(f"Failed to get media URL for ID {media_id}: {res_data}")
                return b""
                
            # 2. Download the actual binary data using the media URL
            media_response = await client.get(media_url, headers=headers, timeout=15.0)
            media_response.raise_for_status()
            
            logger.info(f"Successfully downloaded WhatsApp media ID {media_id}")
            return media_response.content
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP Error downloading media {media_id}: {e.response.text}")
            return b""
        except Exception as e:
            logger.error(f"Error downloading media {media_id}: {e}")
            return b""

@_reported("text")
async def send_whatsapp_message(
    phone_number_id: str,
    recipient_phone: str,
    message_text: str,
    meta_access_token: str
) -> dict:
    """
    Posts text response back to Meta's Cloud API Graph endpoint:
    https://graph.facebook.com/{version}/{phone_number_id}/messages
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message_text
        }
    }

    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(
            f"[MOCK WHATSAPP SENDER] Text to {recipient_phone} via phone_id={phone_number_id}:\n"
            f"Message: {message_text}"
        )
        return {"status": "mock_sent", "recipient": recipient_phone, "message": message_text}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            response.raise_for_status()
            res_data = response.json()
            logger.info(f"Successfully sent WhatsApp text to {recipient_phone}. Response: {res_data}")
            return res_data
        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP Error sending WhatsApp message to {recipient_phone}: "
                f"{e.response.status_code} - {e.response.text}"
            )
            raise e
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message to {recipient_phone}: {str(e)}")
            raise e


@_reported("image")
async def send_whatsapp_image(
    phone_number_id: str,
    recipient_phone: str,
    image_url: str,
    caption: str,
    meta_access_token: str
) -> dict:
    """
    Posts an image response (e.g. room photo, hall setup) to Meta Cloud API.
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }

    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Image to {recipient_phone}: URL={image_url}, Caption={caption}")
        return {"status": "mock_sent", "image_url": image_url}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            response.raise_for_status()
            res_data = response.json()
            logger.info(f"Successfully sent WhatsApp image to {recipient_phone}. Response: {res_data}")
            return res_data
        except Exception as e:
            logger.error(f"Failed to send WhatsApp image to {recipient_phone}: {str(e)}")
            raise e


@_reported("document")
async def send_whatsapp_document(
    phone_number_id: str,
    recipient_phone: str,
    document_url: str,
    filename: str,
    caption: str,
    meta_access_token: str
) -> dict:
    """
    Posts a PDF document response (e.g. Wedding Brochure, Menu PDF) to Meta Cloud API.
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption
        }
    }

    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Document to {recipient_phone}: URL={document_url}, Filename={filename}")
        return {"status": "mock_sent", "document_url": document_url}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=90.0)
            response.raise_for_status()
            res_data = response.json()
            logger.info(f"Successfully sent WhatsApp document to {recipient_phone}. Response: {res_data}")
            return res_data
        except Exception as e:
            logger.error(f"Failed to send WhatsApp document to {recipient_phone}: {str(e)}")
            raise e


async def send_whatsapp_typing_indicator(
    phone_number_id: str,
    message_id: str,
    meta_access_token: str
) -> bool:
    """
    Marks the customer's message as read (blue ticks) and shows "typing..." in their chat while
    the reply is written. WhatsApp hides the indicator when the next message arrives or after
    about 25 seconds. Never raises: a missing indicator must not stop the reply.
    """
    if not message_id or not meta_access_token or "placeholder" in meta_access_token:
        return False

    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {meta_access_token}", "Content-Type": "application/json"}
    read_receipt = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json={**read_receipt, "typing_indicator": {"type": "text"}},
                                         headers=headers, timeout=8.0)
            if response.status_code == 200:
                return True
            # Older Graph API versions reject the typing field: still send the read receipt
            logger.warning(f"Typing indicator rejected ({response.status_code}): {response.text[:200]}")
            await client.post(url, json=read_receipt, headers=headers, timeout=8.0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not send typing indicator: {e}")
    return False


@_reported("voice")
async def send_whatsapp_voice_note(
    phone_number_id: str,
    recipient_phone: str,
    audio_bytes: bytes,
    mime_type: str,
    meta_access_token: str,
    spoken_text: str = ""
) -> dict:
    """
    Uploads generated speech to Meta and sends it as a voice note. `spoken_text` is what the
    voice says; it is only used to show the voice note in the inbox.
    """
    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Voice note to {recipient_phone}: {len(audio_bytes)} bytes of {mime_type}")
        return {"status": "mock_sent", "recipient": recipient_phone}

    base_url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}"
    auth = {"Authorization": f"Bearer {meta_access_token}"}
    is_ogg = mime_type == "audio/ogg"
    filename = "voice_note.ogg" if is_ogg else "voice_note.mp3"

    async with httpx.AsyncClient(timeout=45.0) as client:
        upload = await client.post(
            f"{base_url}/media",
            headers=auth,
            files={"file": (filename, audio_bytes, mime_type)},
            data={"messaging_product": "whatsapp", "type": mime_type},
        )
        upload.raise_for_status()
        media_id = upload.json().get("id")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone,
            "type": "audio",
            "audio": {"id": media_id},
        }
        # "voice": true gives the voice-note bubble; older Graph API versions reject the field
        if is_ogg:
            response = await client.post(f"{base_url}/messages", headers=auth,
                                         json={**payload, "audio": {"id": media_id, "voice": True}})
            if response.status_code == 200:
                return response.json()
            logger.warning(f"Voice-note bubble rejected ({response.status_code}); sending as plain audio")
        response = await client.post(f"{base_url}/messages", headers=auth, json=payload)
        response.raise_for_status()
        return response.json()


async def notify_human_agent(
    phone_number_id: str,
    handoff_number: str,
    customer_phone: str,
    last_user_message: str,
    meta_access_token: str
) -> dict:
    """
    Alerts the human handoff contact when a user requests human assistance.
    """
    alert_text = (
        f"🚨 *HUMAN HANDOFF REQUEST*\n\n"
        f"Customer: {customer_phone}\n"
        f"Message: \"{last_user_message}\"\n\n"
        f"Please step in to reply directly."
    )
    return await send_whatsapp_message(
        phone_number_id=phone_number_id,
        recipient_phone=handoff_number,
        message_text=alert_text,
        meta_access_token=meta_access_token
    )


@_reported("buttons")
async def send_whatsapp_button_message(
    phone_number_id: str,
    recipient_phone: str,
    body_text: str,
    buttons: list,
    meta_access_token: str,
    header_text: str = None,
    footer_text: str = None
) -> dict:
    """
    Posts an interactive quick reply button message (up to 3 buttons) to Meta Cloud API.
    buttons format: [{"id": "btn_1", "title": "📊 View Prices"}, ...]
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }

    button_list = []
    for btn in buttons[:3]:
        button_list.append({
            "type": "reply",
            "reply": {
                "id": str(btn.get("id")),
                "title": str(btn.get("title"))[:20]  # Meta limit: max 20 characters
            }
        })

    interactive_obj = {
        "type": "button",
        "body": {"text": body_text},
        "action": {"buttons": button_list}
    }

    if header_text:
        interactive_obj["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive_obj["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "interactive",
        "interactive": interactive_obj
    }

    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Interactive Button to {recipient_phone}: Body='{body_text}', Buttons={buttons}")
        return {"status": "mock_sent", "buttons": buttons}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            response.raise_for_status()
            res_data = response.json()
            logger.info(f"Successfully sent WhatsApp interactive buttons to {recipient_phone}. Response: {res_data}")
            return res_data
        except Exception as e:
            logger.error(f"Failed to send WhatsApp interactive buttons to {recipient_phone}: {str(e)}")
            raise e


@_reported("list")
async def send_whatsapp_list_message(
    phone_number_id: str,
    recipient_phone: str,
    body_text: str,
    button_title: str,
    sections: list,
    meta_access_token: str,
    header_text: str = None,
    footer_text: str = None
) -> dict:
    """
    Posts an interactive dropdown list message to Meta Cloud API.
    sections format: [{"title": "Header", "rows": [{"id": "1", "title": "Row", "description": "Desc"}]}]
    """
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }

    interactive_obj = {
        "type": "list",
        "body": {"text": body_text},
        "action": {
            "button": button_title[:20],  # Meta limit: max 20 chars
            "sections": sections
        }
    }

    if header_text:
        interactive_obj["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive_obj["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "interactive",
        "interactive": interactive_obj
    }

    if meta_access_token.startswith("EAA_placeholder") or "placeholder" in meta_access_token:
        logger.info(f"[MOCK WHATSAPP SENDER] Interactive List to {recipient_phone}: Body='{body_text}', Button='{button_title}'")
        return {"status": "mock_sent", "sections": sections}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            response.raise_for_status()
            res_data = response.json()
            logger.info(f"Successfully sent WhatsApp interactive list to {recipient_phone}. Response: {res_data}")
            return res_data
        except Exception as e:
            logger.error(f"Failed to send WhatsApp interactive list to {recipient_phone}: {str(e)}")
            raise e

