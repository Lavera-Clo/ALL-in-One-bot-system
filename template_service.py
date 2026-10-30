"""
WhatsApp template messages: how a business reaches a customer more than 24 hours after the
customer's last message. WhatsApp allows only templates the business had approved in Meta's
WhatsApp Manager then (order updates, booking reminders, follow-ups...).

    Meta (WhatsApp Manager)  --approved templates-->  list_templates()  (cached a few minutes)
    inbox "Send template"    --name + the blanks-->   send_template()   --> the customer
                                                      (kept in the chat as the text they received)

The templates live under the business's WhatsApp Business Account (WABA). Its id comes with every
webhook Meta sends (entry.id), so remember_waba() picks it up by itself; it can also be typed in.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import Tenant

logger = logging.getLogger("uvicorn.error")

CACHE_SECONDS = 300
_cache: Dict[str, Tuple[float, List[dict]]] = {}
_PARAM = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _mock(tenant: Tenant) -> bool:
    return "placeholder" in (tenant.meta_access_token or "")


# Offline / test businesses get two sample templates, so the screens can be tried without Meta.
SAMPLE_TEMPLATES = [
    {"name": "order_update", "language": "en_US", "category": "UTILITY", "status": "APPROVED",
     "components": [{"type": "BODY", "text": "Hi {{1}}, your order {{2}} is ready. Reply here if you have any questions."}]},
    {"name": "follow_up", "language": "en_US", "category": "MARKETING", "status": "APPROVED",
     "components": [{"type": "HEADER", "format": "TEXT", "text": "Hello from us"},
                    {"type": "BODY", "text": "Hi {{1}}, it's been a while! Is there anything we can help you with?"}]},
]


async def remember_waba(db: AsyncSession, tenant: Tenant, waba_id: Optional[str]) -> None:
    """Meta sends the WhatsApp Business Account id with every webhook: keep it (once)."""
    waba_id = (waba_id or "").strip()
    if waba_id.isdigit() and tenant.wa_business_account_id != waba_id:
        tenant.wa_business_account_id = waba_id
        await db.commit()
        logger.info(f"Tenant {tenant.id}: WhatsApp Business Account {waba_id} noted (for templates).")


def placeholders(text: str) -> List[str]:
    """The blanks in a template text, in order of first appearance: ['1', '2'] or ['name']."""
    seen: List[str] = []
    for m in _PARAM.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def simplify(t: dict) -> dict:
    """A template as the inbox needs it: the texts, and which blanks each part has."""
    out = {"name": t.get("name"), "language": t.get("language"), "category": t.get("category"),
           "status": t.get("status"), "header": None, "header_params": [], "body": "", "body_params": [],
           "footer": None, "buttons": [], "header_media": None}
    for c in t.get("components") or []:
        kind = (c.get("type") or "").upper()
        if kind == "HEADER":
            fmt = (c.get("format") or "TEXT").upper()
            if fmt == "TEXT":
                out["header"] = c.get("text") or ""
                out["header_params"] = placeholders(out["header"])
            else:
                out["header_media"] = fmt.lower()          # image / video / document: not sendable from here
        elif kind == "BODY":
            out["body"] = c.get("text") or ""
            out["body_params"] = placeholders(out["body"])
        elif kind == "FOOTER":
            out["footer"] = c.get("text") or ""
        elif kind == "BUTTONS":
            out["buttons"] = [b.get("text") for b in c.get("buttons") or [] if b.get("text")]
    return out


async def list_templates(tenant: Tenant, refresh: bool = False) -> List[dict]:
    """The business's APPROVED templates, simplified. Raises ValueError with a reason to show."""
    if _mock(tenant):
        return [simplify(t) for t in SAMPLE_TEMPLATES]
    waba = (tenant.wa_business_account_id or "").strip()
    if not waba:
        raise ValueError("The WhatsApp Business Account ID isn't known yet. It's picked up from the next message a "
                         "customer sends, or paste it in Bot profile (WhatsApp Manager -> Account tools -> Phone numbers).")
    cached = _cache.get(waba)
    if cached and not refresh and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{waba}/message_templates"
    found: List[dict] = []
    params = {"fields": "name,status,language,category,components", "limit": 100}
    async with httpx.AsyncClient(timeout=20.0) as client:
        while url and len(found) < 500:
            r = await client.get(url, params=params, headers={"Authorization": f"Bearer {tenant.meta_access_token}"})
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message") or r.text
                except ValueError:
                    msg = r.text
                raise ValueError(f"Meta didn't return the templates: {str(msg)[:250]}")
            data = r.json()
            found += [t for t in data.get("data") or [] if (t.get("status") or "").upper() == "APPROVED"]
            url, params = (data.get("paging") or {}).get("next"), None
    templates = sorted((simplify(t) for t in found), key=lambda t: (t["name"] or "", t["language"] or ""))
    _cache[waba] = (time.time(), templates)
    return templates


def fill(text: str, values: Dict[str, str]) -> str:
    return _PARAM.sub(lambda m: values.get(m.group(1), m.group(0)), text or "")


def build(template: dict, body_values: Dict[str, str], header_values: Dict[str, str]) -> Tuple[List[dict], str]:
    """(components for Meta, the text the customer will read). Raises ValueError if a blank is empty."""
    for key in template["body_params"] + template["header_params"]:
        value = (body_values if key in template["body_params"] else header_values).get(key, "")
        if not str(value).strip():
            raise ValueError(f"Fill in every blank ({{{{{key}}}}} is empty).")
    if template.get("header_media"):
        raise ValueError(f"This template starts with a {template['header_media']}, which can't be sent from the inbox yet. "
                         "Pick a template with a text header, or none.")

    def params(keys, values):
        out = []
        for key in keys:
            p = {"type": "text", "text": str(values[key]).strip()[:1024]}
            if not key.isdigit():
                p["parameter_name"] = key           # named placeholders ({{name}}) need their name
            out.append(p)
        return out

    components = []
    if template["header_params"]:
        components.append({"type": "header", "parameters": params(template["header_params"], header_values)})
    if template["body_params"]:
        components.append({"type": "body", "parameters": params(template["body_params"], body_values)})
    shown = "\n".join(x for x in [fill(template.get("header") or "", header_values),
                                  fill(template["body"], body_values), template.get("footer") or ""] if x)
    return components, shown
