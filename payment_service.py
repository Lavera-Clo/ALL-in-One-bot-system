import logging
from typing import Optional

from config import settings

logger = logging.getLogger("uvicorn.error")

try:
    import stripe
except ImportError:
    stripe = None


def stripe_is_configured() -> bool:
    """True only when the stripe SDK is installed and a real (non-placeholder) secret key is set."""
    key = settings.STRIPE_SECRET_KEY.strip() if settings.STRIPE_SECRET_KEY else ""
    return bool(stripe) and bool(key) and "placeholder" not in key


async def create_payment_link(order, tenant) -> Optional[str]:
    """
    Creates a Stripe Checkout Session for an order and returns the hosted payment URL.
    Returns None if Stripe isn't configured — callers should fall back to the manual
    bank-transfer flow in that case, exactly like the OpenAI/receipt-analysis fallbacks
    elsewhere in this codebase.
    """
    if not stripe_is_configured():
        return None

    stripe.api_key = settings.STRIPE_SECRET_KEY.strip()
    currency = (settings.PAYMENT_CURRENCY or "usd").lower()

    line_items = []
    if order.items:
        for item in order.items:
            line_items.append({
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": item.product_name},
                    "unit_amount": max(0, int(round(item.unit_price * 100))),
                },
                "quantity": item.quantity,
            })
    else:
        unit_price = order.total_amount / max(order.quantity, 1)
        line_items.append({
            "price_data": {
                "currency": currency,
                "product_data": {"name": order.product_name},
                "unit_amount": max(0, int(round(unit_price * 100))),
            },
            "quantity": order.quantity,
        })

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=f"{settings.BASE_URL}/payment/success?order={order.order_number}",
            cancel_url=f"{settings.BASE_URL}/payment/cancel?order={order.order_number}",
            client_reference_id=order.order_number,
            metadata={"order_number": order.order_number, "tenant_id": str(tenant.id)},
        )
        return session.url
    except Exception as e:
        logger.error(f"Stripe checkout session creation failed for order {order.order_number}: {e}")
        return None


def verify_and_parse_webhook(payload: bytes, sig_header: str):
    """Verifies a Stripe webhook signature and returns the parsed event, or None if invalid/unconfigured."""
    if not stripe_is_configured() or not settings.STRIPE_WEBHOOK_SECRET:
        return None
    try:
        return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Stripe webhook signature verification failed: {e}")
        return None
