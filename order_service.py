import difflib
import logging
import random
import string
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from models import Order, OrderItem, Product, Tenant, CartItem, ChatHistory
from receipt_identifier import analyze_bank_receipt
from whatsapp_client import send_whatsapp_message, send_whatsapp_button_message
from realtime import manager as ws_manager

logger = logging.getLogger("uvicorn.error")

STATUS_LABELS = {
    "pending_payment": "⏳ Pending Payment",
    "payment_uploaded": "📸 Receipt Uploaded — Awaiting Verification",
    "confirmed": "✅ Confirmed",
    "shipped": "🚚 Shipped",
    "delivered": "🎉 Delivered",
    "cancelled": "❌ Cancelled",
}


def generate_order_number() -> str:
    """Generate a human-readable order number like ORD-A3X7K2"""
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choices(chars, k=6))
    return f"ORD-{suffix}"


async def get_active_products(db: AsyncSession, tenant_id: int) -> list[Product]:
    """Get all active products for a tenant."""
    stmt = select(Product).where(
        Product.tenant_id == tenant_id,
        Product.is_active == True,
        Product.stock_quantity > 0
    ).order_by(Product.name.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


async def get_product_by_name(db: AsyncSession, tenant_id: int, product_name: str) -> Optional[Product]:
    """
    Find a product by name. Tries exact/substring match first (fast path),
    then falls back to typo-tolerant fuzzy matching so small misspellings
    ("choclate cake" -> "Chocolate Cake") still resolve correctly.
    """
    query = product_name.strip().lower()
    if not query:
        return None

    stmt = select(Product).where(
        Product.tenant_id == tenant_id,
        Product.is_active == True,
        Product.name.ilike(f"%{query}%")
    )
    result = await db.execute(stmt)
    exact_match = result.scalars().first()
    if exact_match:
        return exact_match

    all_stmt = select(Product).where(Product.tenant_id == tenant_id, Product.is_active == True)
    all_result = await db.execute(all_stmt)
    all_products = all_result.scalars().all()
    if not all_products:
        return None

    names_lower = [p.name.lower() for p in all_products]
    close = difflib.get_close_matches(query, names_lower, n=1, cutoff=0.6)
    if close:
        return next((p for p in all_products if p.name.lower() == close[0]), None)

    return None


async def get_product_by_id(db: AsyncSession, product_id: int) -> Optional[Product]:
    """Get a product by ID."""
    stmt = select(Product).where(Product.id == product_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_pending_order_for_customer(db: AsyncSession, tenant_id: int, customer_phone: str) -> Optional[Order]:
    """Find the most recent pending_payment order for a customer."""
    stmt = select(Order).where(
        Order.tenant_id == tenant_id,
        Order.customer_phone == customer_phone,
        Order.status.in_(["pending_payment", "payment_uploaded"])
    ).order_by(Order.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().first()


async def process_receipt_upload(
    db: AsyncSession,
    tenant: Tenant,
    customer_phone: str,
    image_bytes: bytes
) -> Tuple[Optional[Order], dict]:
    """
    Process an uploaded bank receipt image.
    1. Find the customer's pending order
    2. Analyze the receipt with AI Vision
    3. Update the order with receipt data
    4. Notify the owner/manager
    """
    # Find pending order
    order = await get_pending_order_for_customer(db, tenant.id, customer_phone)
    if not order:
        return None, {"error": "no_pending_order"}

    # Analyze receipt with AI Vision
    analysis = await analyze_bank_receipt(image_bytes)

    # Update order with receipt data
    order.status = "payment_uploaded"
    order.payment_receipt_analysis = analysis.get("summary", "Receipt uploaded")
    order.payment_amount_detected = analysis.get("amount")
    order.payment_ref_number = analysis.get("reference_number")

    await db.commit()
    await db.refresh(order)

    # Notify owner/manager about the new payment
    if tenant.human_handoff_number:
        amount_str = f"Rs.{analysis['amount']:,.2f}" if analysis.get("amount") else "Unknown"
        ref_str = analysis.get("reference_number") or "N/A"
        confidence_str = analysis.get("confidence", "unknown")
        if analysis.get("amount_confirmed_by_two_readers"):
            confidence_str += " (2 AI models agree)"

        # Do the comparison for the manager instead of leaving it to a glance at a phone.
        detected = analysis.get("amount")
        if detected is None:
            match_line = "⚠️ Amount could not be read — please check the receipt."
        elif abs(detected - order.total_amount) < 1:
            match_line = "✅ Matches the order total."
        elif detected < order.total_amount:
            match_line = f"⚠️ Rs.{order.total_amount - detected:,.2f} LESS than the order total."
        else:
            match_line = f"ℹ️ Rs.{detected - order.total_amount:,.2f} more than the order total."

        manager_msg = (
            f"💰 *NEW PAYMENT RECEIPT UPLOADED*\n\n"
            f"📦 Order: *{order.order_number}*\n"
            f"👤 Customer: {customer_phone}\n"
            f"🛒 Product: {order.product_name} x{order.quantity}\n"
            f"💵 Order Total: Rs.{order.total_amount:,.2f}\n\n"
            f"🏦 *AI Receipt Analysis:*\n"
            f"• Amount Detected: {amount_str}\n"
            f"• {match_line}\n"
            f"• Reference #: {ref_str}\n"
            f"• Confidence: {confidence_str}\n"
            f"• Summary: {analysis.get('summary', 'N/A')}\n\n"
            f"✅ Reply 'CONFIRM {order.order_number}' to approve this order.\n"
            f"❌ Reply 'REJECT {order.order_number}' to reject."
        )

        try:
            await send_whatsapp_message(
                phone_number_id=tenant.whatsapp_phone_number_id,
                recipient_phone=tenant.human_handoff_number,
                message_text=manager_msg,
                meta_access_token=tenant.meta_access_token
            )
            logger.info(f"Notified manager {tenant.human_handoff_number} about order {order.order_number}")
        except Exception as e:
            logger.error(f"Failed to notify manager about order {order.order_number}: {e}")

    return order, analysis


async def update_order_status(
    db: AsyncSession,
    order: Order,
    tenant: Tenant,
    new_status: str,
    confirmed_by: str = None
) -> Order:
    """Update order status and send appropriate notification to customer."""
    order.status = new_status
    if confirmed_by:
        order.confirmed_by = confirmed_by
    if new_status == "confirmed" and not order.confirmed_at:
        order.confirmed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(order, attribute_names=["items"])

    # Status-specific messages
    status_messages = {
        "confirmed": (
            f"✅ *Your order has been successfully placed!*\n\n"
            f"📦 Order: *{order.order_number}*\n"
            f"🛒 {order.product_name} x{order.quantity}\n"
            f"💵 Total: Rs.{order.total_amount:,.2f}\n\n"
            f"Your order will be sent to you soon! 🚀\n"
            f"Thank you for your purchase! 🙏"
        ),
        "shipped": (
            f"🚚 *Your order has been shipped!*\n\n"
            f"📦 Order: *{order.order_number}*\n"
            f"🛒 {order.product_name} x{order.quantity}\n\n"
            f"Your order is on its way! You'll receive it soon. 📦✨"
        ),
        "delivered": (
            f"🎉 *Your order has been delivered!*\n\n"
            f"📦 Order: *{order.order_number}*\n"
            f"🛒 {order.product_name} x{order.quantity}\n\n"
            f"We hope you love it! Thank you for shopping with us! 🙏💛"
        ),
        "cancelled": (
            f"❌ *Your order has been cancelled.*\n\n"
            f"📦 Order: *{order.order_number}*\n"
            f"🛒 {order.product_name} x{order.quantity}\n\n"
            f"If you have any questions, please contact us."
        ),
    }

    msg = status_messages.get(new_status)
    if msg:
        try:
            await send_whatsapp_message(
                phone_number_id=tenant.whatsapp_phone_number_id,
                recipient_phone=order.customer_phone,
                message_text=msg,
                meta_access_token=tenant.meta_access_token
            )
        except Exception as e:
            logger.error(f"Failed to send status update to {order.customer_phone}: {e}")

    try:
        await ws_manager.broadcast(tenant.id, {
            "type": "order_updated",
            "order_number": order.order_number,
            "status": order.status
        })
    except Exception as e:
        logger.error(f"Failed to broadcast order update over WebSocket: {e}")

    return order


def build_product_catalog_text(products: list[Product]) -> str:
    """Build a formatted text catalog of products for WhatsApp."""
    if not products:
        return "We don't have any products available right now. Please check back later! 🙏"

    lines = ["🛍️ *Our Products:*\n"]
    for i, p in enumerate(products, 1):
        stock_label = f"({p.stock_quantity} in stock)" if p.stock_quantity > 0 else "(Out of stock)"
        lines.append(f"{i}. *{p.name}* — Rs.{p.price:,.2f} {stock_label}")
        if p.description:
            lines.append(f"   _{p.description}_")
        lines.append("")

    lines.append("To order, just tell me the product name and how many you'd like! 🛒")
    return "\n".join(lines)


def build_product_list_sections(products: list[Product]) -> list:
    """Build WhatsApp interactive list sections from products."""
    rows = []
    for p in products[:10]:  # WhatsApp limit: max 10 rows
        rows.append({
            "id": f"buy_product_{p.id}",
            "title": p.name[:24],
            "description": f"Rs.{p.price:,.2f} | Stock: {p.stock_quantity}"[:72]
        })

    return [{
        "title": "Available Products",
        "rows": rows
    }]


# ---------------------------------------------------------------------------
# SHOPPING CART
# ---------------------------------------------------------------------------

async def get_cart_items(db: AsyncSession, tenant_id: int, customer_phone: str) -> list[CartItem]:
    """Get all cart items for a customer, with the product relationship loaded."""
    stmt = (
        select(CartItem)
        .where(CartItem.tenant_id == tenant_id, CartItem.customer_phone == customer_phone)
        .options(selectinload(CartItem.product))
        .order_by(CartItem.created_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def add_to_cart(db: AsyncSession, tenant_id: int, customer_phone: str, product: Product, quantity: int = 1) -> CartItem:
    """Add a product to the customer's cart, or bump quantity if it's already there."""
    stmt = select(CartItem).where(
        CartItem.tenant_id == tenant_id,
        CartItem.customer_phone == customer_phone,
        CartItem.product_id == product.id
    )
    result = await db.execute(stmt)
    item = result.scalars().first()

    if item:
        item.quantity += quantity
    else:
        item = CartItem(tenant_id=tenant_id, customer_phone=customer_phone, product_id=product.id, quantity=quantity)
        db.add(item)

    await db.commit()
    await db.refresh(item, attribute_names=["product"])
    return item


async def remove_from_cart(db: AsyncSession, tenant_id: int, customer_phone: str, product_name: str) -> bool:
    """Remove an item from the cart by (fuzzy) product name. Returns True if something was removed."""
    items = await get_cart_items(db, tenant_id, customer_phone)
    if not items:
        return False

    query = product_name.strip().lower()
    target = next((i for i in items if query in i.product.name.lower()), None)

    if not target:
        names_lower = [i.product.name.lower() for i in items]
        close = difflib.get_close_matches(query, names_lower, n=1, cutoff=0.5)
        if close:
            target = next((i for i in items if i.product.name.lower() == close[0]), None)

    if not target:
        return False

    await db.delete(target)
    await db.commit()
    return True


async def clear_cart(db: AsyncSession, tenant_id: int, customer_phone: str) -> int:
    """Empty a customer's cart. Returns the number of items removed."""
    items = await get_cart_items(db, tenant_id, customer_phone)
    for item in items:
        await db.delete(item)
    await db.commit()
    return len(items)


def build_cart_text(cart_items: list[CartItem]) -> str:
    """Build a formatted WhatsApp view of the current cart."""
    if not cart_items:
        return "🛒 Your cart is empty. Type *shop* to browse our products!"

    lines = ["🛒 *Your Cart:*\n"]
    grand_total = 0.0
    for i, item in enumerate(cart_items, 1):
        subtotal = item.product.price * item.quantity
        grand_total += subtotal
        lines.append(f"{i}. *{item.product.name}* x{item.quantity} — Rs.{subtotal:,.2f}")

    lines.append(f"\n💵 *Total: Rs.{grand_total:,.2f}*")
    lines.append("\nType *checkout* to place your order, *clear cart* to empty it, or keep browsing! 🛍️")
    return "\n".join(lines)


async def checkout_cart(
    db: AsyncSession,
    tenant: Tenant,
    customer_phone: str,
    customer_name: str = None
) -> Tuple[Optional[Order], list[Product], Optional[str]]:
    """
    Converts the customer's cart into a confirmed Order with OrderItem line items,
    reducing stock for each product. Returns (order, low_stock_products, error).
    error is one of None | "empty_cart" | "out_of_stock".
    """
    cart_items = await get_cart_items(db, tenant.id, customer_phone)
    if not cart_items:
        return None, [], "empty_cart"

    for item in cart_items:
        if item.product.stock_quantity < item.quantity:
            return None, [], "out_of_stock"

    order_number = generate_order_number()
    total = sum(item.product.price * item.quantity for item in cart_items)

    if len(cart_items) == 1:
        summary_name = cart_items[0].product.name
    else:
        preview = ", ".join(i.product.name for i in cart_items[:3])
        suffix = "…" if len(cart_items) > 3 else ""
        summary_name = f"{len(cart_items)} items ({preview}{suffix})"

    order = Order(
        tenant_id=tenant.id,
        order_number=order_number,
        customer_phone=customer_phone,
        customer_name=customer_name,
        product_id=cart_items[0].product.id if len(cart_items) == 1 else None,
        product_name=summary_name,
        quantity=sum(item.quantity for item in cart_items),
        total_amount=total,
        status="pending_payment"
    )
    db.add(order)
    await db.flush()

    low_stock_products = []
    for item in cart_items:
        db.add(OrderItem(
            order_id=order.id,
            product_id=item.product.id,
            product_name=item.product.name,
            quantity=item.quantity,
            unit_price=item.product.price,
            subtotal=item.product.price * item.quantity
        ))
        item.product.stock_quantity = max(0, item.product.stock_quantity - item.quantity)
        if item.product.stock_quantity <= item.product.low_stock_threshold:
            low_stock_products.append(item.product)
        await db.delete(item)

    await db.commit()
    await db.refresh(order, attribute_names=["items"])

    logger.info(f"Checked out cart for {customer_phone}: order {order_number}, {len(cart_items)} item(s), Rs.{total:,.2f}")

    try:
        await ws_manager.broadcast(tenant.id, {
            "type": "order_created",
            "order_number": order.order_number,
            "status": order.status,
            "total_amount": order.total_amount
        })
    except Exception as e:
        logger.error(f"Failed to broadcast new order over WebSocket: {e}")

    return order, low_stock_products, None


# ---------------------------------------------------------------------------
# ORDER TRACKING & HISTORY
# ---------------------------------------------------------------------------

async def get_order_by_number(db: AsyncSession, tenant_id: int, order_number: str) -> Optional[Order]:
    """Look up a single order by its human-readable order number, with items loaded."""
    stmt = (
        select(Order)
        .where(Order.tenant_id == tenant_id, Order.order_number == order_number.strip().upper())
        .options(selectinload(Order.items))
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_customer_orders(db: AsyncSession, tenant_id: int, customer_phone: str, limit: int = 5) -> list[Order]:
    """Get a customer's most recent orders, newest first."""
    stmt = (
        select(Order)
        .where(Order.tenant_id == tenant_id, Order.customer_phone == customer_phone)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


def build_order_status_text(order: Order) -> str:
    """Build a detailed WhatsApp status message for a single order."""
    status_label = STATUS_LABELS.get(order.status, order.status)
    lines = [f"📦 *Order {order.order_number}*", f"Status: {status_label}", ""]

    if order.items:
        for item in order.items:
            lines.append(f"• {item.product_name} x{item.quantity} — Rs.{item.subtotal:,.2f}")
    else:
        lines.append(f"• {order.product_name} x{order.quantity}")

    lines.append(f"\n💵 Total: Rs.{order.total_amount:,.2f}")
    lines.append(f"🗓️ Placed: {order.created_at.strftime('%d %b %Y, %I:%M %p')}")
    return "\n".join(lines)


def build_order_history_text(orders: list[Order]) -> str:
    """Build a formatted WhatsApp summary of a customer's recent orders."""
    if not orders:
        return "You don't have any orders with us yet. Type *shop* to browse our products! 🛍️"

    lines = ["📋 *Your Recent Orders:*\n"]
    for o in orders:
        status_label = STATUS_LABELS.get(o.status, o.status)
        lines.append(f"📦 *{o.order_number}* — {status_label}\n   {o.product_name} x{o.quantity} — Rs.{o.total_amount:,.2f}")
    lines.append("\nType *track ORD-XXXXXX* for full details on any order.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LOW STOCK & BROADCAST
# ---------------------------------------------------------------------------

async def get_low_stock_products(db: AsyncSession, tenant_id: int) -> list[Product]:
    """Get active products at or below their configured low-stock threshold."""
    stmt = select(Product).where(
        Product.tenant_id == tenant_id,
        Product.is_active == True,
        Product.stock_quantity <= Product.low_stock_threshold
    ).order_by(Product.stock_quantity.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


async def get_broadcast_recipients(db: AsyncSession, tenant_id: int) -> list[str]:
    """Get distinct customer phone numbers who have ordered or chatted with this tenant."""
    order_stmt = select(Order.customer_phone).where(Order.tenant_id == tenant_id).distinct()
    chat_stmt = select(ChatHistory.customer_phone_number).where(ChatHistory.tenant_id == tenant_id).distinct()

    order_result = await db.execute(order_stmt)
    chat_result = await db.execute(chat_stmt)

    phones = {row[0] for row in order_result.all()} | {row[0] for row in chat_result.all()}
    return sorted(phones)


# ---------------------------------------------------------------------------
# ABANDONED CART RECOVERY
# ---------------------------------------------------------------------------

async def get_abandoned_carts(db: AsyncSession, idle_minutes: int) -> list[dict]:
    """
    Finds carts that have sat untouched for `idle_minutes` and haven't had a
    reminder sent yet. Returns [{"tenant_id", "customer_phone", "items": [CartItem,...]}].
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
    stmt = (
        select(CartItem)
        .where(CartItem.reminded_at.is_(None), CartItem.updated_at < cutoff)
        .options(selectinload(CartItem.product))
        .order_by(CartItem.tenant_id.asc(), CartItem.customer_phone.asc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    grouped: dict[tuple, list] = {}
    for item in items:
        key = (item.tenant_id, item.customer_phone)
        grouped.setdefault(key, []).append(item)

    return [
        {"tenant_id": key[0], "customer_phone": key[1], "items": group}
        for key, group in grouped.items()
    ]


async def mark_cart_reminded(db: AsyncSession, cart_items: list[CartItem]) -> None:
    """Flags a group of cart items as having received their one-time abandoned-cart reminder."""
    now = datetime.now(timezone.utc)
    for item in cart_items:
        item.reminded_at = now
    await db.commit()


async def get_dynamic_kb(tenant: Tenant, db: AsyncSession) -> str:
    """Injects live real-time ERP stock inventory into the AI's knowledge base."""
    products_stmt = select(Product).where(Product.tenant_id == tenant.id, Product.is_active == True)
    products_res = await db.execute(products_stmt)
    live_products = products_res.scalars().all()

    # A business that sells no products through the bot (a clinic, a salon, a venue) gets no
    # inventory block at all. It used to be told "No products currently listed", which the bot
    # then repeated to customers asking what the business offers.
    if not live_products:
        return tenant.knowledge_base or ""

    # Prices are in the tenant's own currency (this printed "$" for every business).
    currency = f"{tenant.currency} " if tenant.currency else ""
    inventory_str = "\n\n--- LIVE ERP INVENTORY ---\n"
    for p in live_products:
        status = f"{p.stock_quantity} in stock" if p.stock_quantity > 0 else "OUT OF STOCK"
        inventory_str += f"- {p.name}: {currency}{p.price:,.2f} ({status})\n"
    inventory_str += "\n[CRITICAL RULE]: When quoting products or prices, ALWAYS refer to the live ERP inventory above. If a customer asks if something is available, strictly check the exact stock here.\n"

    return (tenant.knowledge_base or "") + inventory_str
