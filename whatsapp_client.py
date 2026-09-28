import logging
import httpx
from config import settings

logger = logging.getLogger("uvicorn.error")

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

