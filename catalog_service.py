"""
The WhatsApp catalogue: the business's products inside WhatsApp itself (the shop icon on the
profile and in the chat). Customers browse, add to a cart and send it; the bot turns that cart
into an order the usual way (cart -> checkout -> payment), with stock checked as always.

    portal products ──sync──► Meta Commerce catalogue (items_batch)
                               │  linked to the WhatsApp number, cart switched on
    customer: "shop" ◄─────────┘  "View catalogue" message / product messages
    customer sends a cart ──► webhook type "order" ──► the bot's cart ──► "checkout"

Each product is "p<id>" in the catalogue (its retailer id), so a cart line maps back to exactly
one product. Prices and stock come from the portal; edit them there, not in Commerce Manager.

Meta's side (once): create a catalogue in Commerce Manager, connect it to the WhatsApp Business
Account (WhatsApp Manager -> Catalogue), and give the access token `catalog_management`.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import Product, Tenant, utc_now

logger = logging.getLogger("uvicorn.error")

BATCH = 50


def retailer_id(product: Product) -> str:
    return f"p{product.id}"


def product_from_retailer_id(value: str) -> Optional[int]:
    m = re.fullmatch(r"p(\d+)", (value or "").strip())
    return int(m.group(1)) if m else None


def _public(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return settings.BASE_URL.rstrip("/") + "/" + url.lstrip("/")


def product_link(tenant: Tenant, product: Product) -> str:
    return f"{settings.BASE_URL.rstrip('/')}/shop/{tenant.id}/{product.id}"


def item_data(tenant: Tenant, product: Product) -> dict:
    """One catalogue item, the way Meta's items_batch wants it."""
    currency = (tenant.currency or "LKR").upper()
    data = {
        "id": retailer_id(product),
        "title": product.name[:150],
        "description": (product.description or product.name)[:5000],
        "availability": "in stock" if product.is_active and product.stock_quantity > 0 else "out of stock",
        "condition": "new",
        "price": f"{product.price:.2f} {currency}",
        "link": product_link(tenant, product),
        "brand": (tenant.business_name or "Shop")[:100],
    }
    image = _public(product.image_url or "")
    if image:
        data["image_link"] = image
    return data


def missing_for_catalogue(product: Product) -> Optional[str]:
    """Meta refuses a product without a picture."""
    return None if product.image_url else "no image"


async def _graph(method: str, path: str, token: str, **kwargs) -> dict:
    url = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        raise RuntimeError(err.get("error_user_msg") or err.get("message") or f"Meta answered {r.status_code}")
    return data


def _mock(tenant: Tenant) -> bool:
    return "placeholder" in (tenant.meta_access_token or "")


async def connect(db: AsyncSession, tenant: Tenant, catalog_id: str) -> dict:
    """Link a catalogue to this business, switch on the cart and the shop icon, and sync the products."""
    catalog_id = re.sub(r"\D", "", catalog_id or "")
    if not catalog_id:
        raise ValueError("Paste the catalogue ID from Commerce Manager (numbers only).")
    if not _mock(tenant):
        await _graph("GET", catalog_id, tenant.meta_access_token, params={"fields": "id,name"})
        await _graph("POST", f"{tenant.whatsapp_phone_number_id}/whatsapp_commerce_settings", tenant.meta_access_token,
                     params={"is_cart_enabled": "true", "is_catalog_visible": "true"})
    tenant.wa_catalog_id = catalog_id
    await db.commit()
    return await sync(db, tenant)


async def sync(db: AsyncSession, tenant: Tenant, product_ids: Optional[List[int]] = None,
               deleted_retailer_ids: Optional[List[str]] = None) -> dict:
    """Push products (all, or just `product_ids`) to the catalogue; remove deleted ones."""
    if not tenant.wa_catalog_id:
        return {"synced": 0, "skipped": [], "removed": 0, "error": "No catalogue connected."}
    q = select(Product).where(Product.tenant_id == tenant.id)
    if product_ids:
        q = q.where(Product.id.in_(product_ids))
    products = (await db.execute(q.order_by(Product.id))).scalars().all()

    requests, skipped = [], []
    for p in products:
        why = missing_for_catalogue(p)
        if why:
            skipped.append({"product": p.name, "reason": why})
            continue
        requests.append({"method": "UPDATE", "data": item_data(tenant, p)})
    for rid in deleted_retailer_ids or []:
        requests.append({"method": "DELETE", "data": {"id": rid}})

    error = None
    try:
        for i in range(0, len(requests), BATCH):
            chunk = requests[i:i + BATCH]
            if _mock(tenant):
                logger.info(f"[MOCK CATALOGUE] {len(chunk)} item change(s) for catalogue {tenant.wa_catalog_id}")
                continue
            res = await _graph("POST", f"{tenant.wa_catalog_id}/items_batch", tenant.meta_access_token,
                               json={"item_type": "PRODUCT_ITEM", "allow_upsert": True, "requests": chunk})
            problems = [h for h in res.get("validation_status") or [] if h.get("errors")]
            if problems:
                error = "; ".join(f"{h.get('retailer_id')}: {h['errors'][0].get('message')}" for h in problems[:5])
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]
    tenant.catalog_error = error
    if not error:
        tenant.catalog_synced_at = utc_now()
    await db.commit()
    return {"synced": len([r for r in requests if r["method"] == "UPDATE"]) if not error else 0,
            "removed": len(deleted_retailer_ids or []) if not error else 0, "skipped": skipped, "error": error}


async def sync_later(tenant_id: int, product_ids: Optional[List[int]] = None,
                     deleted_retailer_ids: Optional[List[str]] = None) -> None:
    """For product edits in the portal: runs after the response, never raises."""
    from database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            if tenant and tenant.wa_catalog_id:
                await sync(db, tenant, product_ids, deleted_retailer_ids)
    except Exception:  # noqa: BLE001
        logger.exception("[catalogue] background sync failed")


def status(tenant: Tenant) -> dict:
    return {"catalog_id": tenant.wa_catalog_id, "connected": bool(tenant.wa_catalog_id),
            "synced_at": tenant.catalog_synced_at.isoformat() if tenant.catalog_synced_at else None,
            "error": tenant.catalog_error}


# --- a cart sent from WhatsApp --------------------------------------------------------------

async def order_to_cart(db: AsyncSession, tenant: Tenant, phone: str, order: dict) -> Tuple[list, List[str]]:
    """
    Replace the customer's cart with what they sent from the catalogue. Returns (cart items,
    problems) - a product we don't know, one switched off, or not enough stock (capped).
    """
    from order_service import add_to_cart, clear_cart, get_cart_items

    problems: List[str] = []
    wanted: Dict[int, int] = {}
    for line in order.get("product_items") or []:
        pid = product_from_retailer_id(line.get("product_retailer_id", ""))
        try:
            qty = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if pid is None or qty <= 0:
            problems.append(f"an item we don't recognise ({line.get('product_retailer_id')})")
            continue
        wanted[pid] = wanted.get(pid, 0) + qty

    await clear_cart(db, tenant.id, phone)
    for pid, qty in wanted.items():
        product = await db.get(Product, pid)
        if product is None or product.tenant_id != tenant.id or not product.is_active:
            problems.append("a product that is no longer sold")
            continue
        if product.stock_quantity <= 0:
            problems.append(f"{product.name} is out of stock")
            continue
        if qty > product.stock_quantity:
            problems.append(f"only {product.stock_quantity} {product.name} in stock (you asked for {qty})")
            qty = product.stock_quantity
        await add_to_cart(db, tenant.id, phone, product, quantity=qty)
    return await get_cart_items(db, tenant.id, phone), problems
