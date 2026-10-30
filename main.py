import asyncio
import time
import hashlib
import hmac
import html
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import (
    FastAPI, Request, Response, Depends, HTTPException, status, Query,
    UploadFile, File, Form, Header, WebSocket, WebSocketDisconnect, BackgroundTasks
)
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

import platform
import shutil
import secrets
import string


from config import settings
from database import init_db, get_db, AsyncSessionLocal
from models import Tenant, TenantDocument, ChatHistory, Product, Order, OrderItem, AdminUser, AdminSession, BotTestRun, PasswordResetRequest, Booking

from schemas import (
    TenantCreate,
    TenantUpdate,
    TenantResponse,
    WhatsAppWebhookPayload,
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    OrderResponse,
    OrderStatusUpdate,
    BroadcastRequest,
    AdminLoginRequest,
    AdminUserCreate,
    AdminUserUpdate,
    AdminUserResponse,
    SuperAdminAccountCreate,
    SuperAdminAccountUpdate,
    AdminUserAccountDetail,
    ForgotPasswordSubmit,
    PasswordResetResolve,
    PasswordResetReject,
    PasswordResetResponse,
    BotProfileUpdate,
    TenantDocumentUpdate,
    BotPreviewRequest,
    GapAnswerRequest,
    TeachRequest,
    SignupRequest,
    WhatsAppConnectRequest,
    InboxSendRequest, DeviceMessageRecord, ContactUpdate, HistorySettings, BookingUpdate, CallStart, InboxShare, CatalogConnect, BrowserCallOffer, TeamDial, WebsiteTrain,
    TemplateSend,
    ChatBotUpdate,
    BusinessBotUpdate,
    ManualOrderCreate,
)
from learning_service import (answer_gap, dismiss_gap, gap_payload, get_gap, learn_from_past_chats, list_gaps,
                              record_gap, teach, teach_many)
from self_test_service import recover_interrupted_runs, run_payload, run_self_test, start_run as start_test_run
from understanding_service import (
    PLACEHOLDER_PROMPT_START, recover_interrupted as recover_interrupted_understanding, understand_business,
    understanding_payload,
)
from ai_service import (
    generate_ai_response, generate_bot_reply, detect_language_intent,
    get_pdfs_for_user_message, resolve_reply_documents, DOC_REQUEST_WORDS,
    classify_product_from_image, split_reply_into_bubbles, typing_pause_seconds,
)
from voice_reply_service import build_spoken_reply, synthesize_voice_note
from whatsapp_client import (
    WhatsAppCredentialsError,
    verify_whatsapp_number,
    send_whatsapp_message,
    send_whatsapp_document,
    send_whatsapp_image,
    send_whatsapp_button_message,
    send_whatsapp_list_message,
    notify_human_agent,
    download_whatsapp_media,
    send_whatsapp_typing_indicator,
    send_whatsapp_voice_note,
    send_whatsapp_catalog,
    send_whatsapp_media,
    send_whatsapp_location,
    send_whatsapp_contacts,
    send_whatsapp_products,
    send_whatsapp_template,
    upload_whatsapp_media,
)
from order_service import (
    get_active_products,
    get_product_by_name,
    get_product_by_id,
    get_pending_order_for_customer,
    process_receipt_upload,
    update_order_status,
    build_product_catalog_text,
    build_product_list_sections,
    add_to_cart,
    get_cart_items,
    remove_from_cart,
    clear_cart,
    build_cart_text,
    checkout_cart,
    get_order_by_number,
    get_customer_orders,
    build_order_status_text,
    build_order_history_text,
    get_low_stock_products,
    get_broadcast_recipients,
    get_abandoned_carts,
    mark_cart_reminded,
    create_manual_order,
    get_dynamic_kb,
    load_catalog,
)
from auth_service import hash_password, verify_password, generate_session_token, session_expiry, validate_password_strength
from payment_service import stripe_is_configured, create_payment_link, verify_and_parse_webhook
from invoice_service import generate_invoice_pdf
from realtime import manager as ws_manager, inbox_manager, call_desks
from inbox_service import (
    bot_may_reply, chat_messages, describe_incoming, get_conversation, is_manager, list_chats, mark_needs_human,
    on_whatsapp_sent, record_incoming, send_as_agent, send_other_as_agent, set_business_bot, set_chat_bot, update_incoming_text,
    MAX_REPLY_CHARS,
)
from whatsapp_client import add_send_listener
from sync_service import (
    HISTORY_DAYS_ALLOWED, apply_statuses, cleanup_old_messages, contact_payload, find_by_client_id,
    list_contacts, message_stats, record_device_message, sync_changes, update_contact,
)
from booking_service import (
    capture_later as capture_booking_later, handle_manager_command as handle_booking_command,
    list_bookings, payload as booking_payload, set_status as set_booking_status, STATUSES as BOOKING_STATUSES,
)
from conversation_memory import build_context as build_conversation_context, load_brief as load_conversation_brief
from voice_service import (
    bot_text as voice_bot_text, fallback_reply as voice_fallback_reply, hear_voice_note,
    inbox_text as voice_inbox_text,
)

# Every message sent to a customer is recorded for the live inbox (inbox_service.py).
add_send_listener(on_whatsapp_sent)
from rate_limit import RateLimitMiddleware, SecurityHeadersMiddleware
import media_service
import catalog_service
from bot_profile_generator import generate_bot_profile
from bot_learning import get_learning_stats

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

ABANDONED_CART_CHECK_INTERVAL_SECONDS = 300
SERVER_START_TIME = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler to initialize database tables and seed tenants on startup."""
    logger.info("Starting up WhatsApp AI Chatbot service...")
    await init_db()

    # Background training jobs die with the process; mark them so the portal offers Retrain.
    from document_service import recover_interrupted_trainings
    interrupted = await recover_interrupted_trainings()
    if interrupted:
        logger.warning(f"{interrupted} document training job(s) were interrupted by the restart.")
    await recover_interrupted_understanding()
    interrupted_tests = await recover_interrupted_runs()
    if interrupted_tests:
        logger.warning(f"{interrupted_tests} bot test run(s) were interrupted by the restart.")

    if settings.ADMIN_PASSWORD == "admin123":
        logger.warning("ADMIN_PASSWORD is still the default: set your own in .env before going live.")

    # Seed the first starter tenant on a brand-new database only. Its WhatsApp
    # token comes from .env; an existing tenant's token is never touched here.
    async with AsyncSessionLocal() as session:
        tenant_count = (await session.execute(select(func.count(Tenant.id)))).scalar() or 0
        if tenant_count == 0:
            starter_tenant = Tenant(
                business_name="Main Store",
                whatsapp_phone_number_id=settings.SEED_PHONE_NUMBER_ID,
                meta_access_token=settings.META_ACCESS_TOKEN or "EAA_placeholder_set_META_ACCESS_TOKEN",
                system_prompt=(
                    "You are the professional AI customer support and sales assistant for Main Store. "
                    "Answer customers warmly, politely, and accurately based on our business knowledge base and product catalog."
                ),
                knowledge_base=(
                    "=== MASTER BUSINESS KNOWLEDGE BASE ===\n\n"
                    "Welcome to our store! We provide high quality products and exceptional customer service.\n\n"
                    "Browse our product catalog, place orders, or ask any questions about our items, delivery, and pricing.\n\n"
                    "Business Hours: Monday - Sunday, 8:30 AM - 8:00 PM\n"
                    "For assistance or custom inquiries, please leave a message."
                ),
                human_handoff_number="",
                front_office_number="",
                industry="retail"
            )
            session.add(starter_tenant)
            await session.commit()
            logger.info("Successfully auto-seeded starter tenant into database!")

        # Ensure the owner admin account exists and credentials match settings
        owner = (await session.execute(
            select(AdminUser).where(func.lower(AdminUser.username) == settings.ADMIN_USERNAME.lower())
        )).scalar_one_or_none()

        if not owner:
            first_owner = AdminUser(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD or "admin123"),
                role="owner",
                is_active=True
            )
            session.add(first_owner)
            await session.commit()
            logger.info(f"Bootstrapped first owner admin account: '{settings.ADMIN_USERNAME}'")
        elif os.getenv("FORCE_RESET_ADMIN", "1") == "1":
            owner.password_hash = hash_password(settings.ADMIN_PASSWORD or "admin123")
            owner.is_active = True
            owner.role = "owner"
            await session.commit()
            logger.info(f"Synchronized owner account '{owner.username}' credentials.")

        # Auto-migrate legacy tenants away from Wasala branding
        wasala_rows = (await session.execute(
            select(Tenant).where(Tenant.business_name.ilike("%wasala%"))
        )).scalars().all()
        for t in wasala_rows:
            t.business_name = "Main Store"
            t.system_prompt = (
                "You are the professional AI customer support and sales assistant for Main Store. "
                "Answer customers warmly, politely, and accurately based on our business knowledge base and product catalog."
            )
            t.knowledge_base = (
                "=== MASTER BUSINESS KNOWLEDGE BASE ===\n\n"
                "Welcome to our store! We provide high quality products and exceptional customer service.\n\n"
                "Browse our product catalog, place orders, or ask any questions about our items, delivery, and pricing.\n\n"
                "Business Hours: Monday - Sunday, 8:30 AM - 8:00 PM\n"
                "For assistance or custom inquiries, please leave a message."
            )
            t.industry = "retail"
            t.bot_name = "Assistant"
            logger.info(f"Updated legacy tenant #{t.id} from Wasala to Main Store.")
        if wasala_rows:
            await session.commit()

    abandoned_cart_task = asyncio.create_task(abandoned_cart_worker())
    history_cleanup_task = asyncio.create_task(history_cleanup_worker())
    recording_cleanup_task = asyncio.create_task(recording_cleanup_worker())

    yield

    abandoned_cart_task.cancel()
    history_cleanup_task.cancel()
    recording_cleanup_task.cancel()
    logger.info("Shutting down WhatsApp AI Chatbot service...")


HISTORY_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


async def history_cleanup_worker():
    """Every 6 hours: delete messages older than each business's history_days (contacts stay)."""
    await asyncio.sleep(60)            # let startup finish first
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await cleanup_old_messages(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("History cleanup failed")
        await asyncio.sleep(HISTORY_CLEANUP_INTERVAL_SECONDS)


async def recording_cleanup_worker():
    """Every hour: delete call recordings older than CALL_RECORDING_DAYS (the call log stays)."""
    await asyncio.sleep(90)
    while True:
        try:
            await call_service.delete_old_recordings()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recording cleanup failed")
        await asyncio.sleep(60 * 60)


async def abandoned_cart_worker():
    """Background loop: periodically finds idle carts and sends each one a single reminder."""
    while True:
        try:
            await asyncio.sleep(ABANDONED_CART_CHECK_INTERVAL_SECONDS)
            async with AsyncSessionLocal() as session:
                abandoned = await get_abandoned_carts(session, settings.ABANDONED_CART_MINUTES)
                for cart in abandoned:
                    tenant_stmt = select(Tenant).where(Tenant.id == cart["tenant_id"])
                    tenant = (await session.execute(tenant_stmt)).scalar_one_or_none()
                    if not tenant:
                        continue

                    reminder_text = (
                        "👋 You left some items in your cart!\n\n"
                        f"{build_cart_text(cart['items'])}\n\n"
                        "Ready to check out? Just type *checkout* 🛒"
                    )
                    try:
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=cart["customer_phone"],
                            message_text=reminder_text,
                            meta_access_token=tenant.meta_access_token
                        )
                        logger.info(f"Sent abandoned cart reminder to {cart['customer_phone']} (tenant {tenant.id})")
                    except Exception as e:
                        logger.error(f"Failed to send abandoned cart reminder: {e}")

                    await mark_cart_reminded(session, cart["items"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Abandoned cart worker error: {e}")

app = FastAPI(
    title="Multi-Tenant WhatsApp AI Chatbot API",
    description="Production-ready multi-tenant Python FastAPI backend with SQLite, OpenAI gpt-4o-mini, and Meta WhatsApp Cloud API.",
    version="1.0.0",
    lifespan=lifespan
)

# ---------------------------------------------------------------------------
# Duplicate-delivery guard for WhatsApp message IDs (in memory, per process).
# With several server workers, move this to the database or Redis.
# ---------------------------------------------------------------------------
_SEEN_MESSAGES: dict = {}
_SEEN_TTL_SECONDS = 6 * 3600


def seen_before(message_id: Optional[str]) -> bool:
    """True if this WhatsApp message ID was already handled recently; records it otherwise."""
    if not message_id:
        return False
    now = datetime.now(timezone.utc).timestamp()
    if len(_SEEN_MESSAGES) > 5000:
        for key in [k for k, t in _SEEN_MESSAGES.items() if now - t > _SEEN_TTL_SECONDS]:
            _SEEN_MESSAGES.pop(key, None)
    if message_id in _SEEN_MESSAGES:
        return True
    _SEEN_MESSAGES[message_id] = now
    return False


# ---------------------------------------------------------------------------
# Follow-up throttle: stops the bot repeating the same PDF / menu to one customer.
# Measured before: 3 questions in a minute produced 11 WhatsApp messages, including the
# same price-list PDF three times. People mute or block that, and Meta lowers the number's
# quality rating when they do.
# ---------------------------------------------------------------------------
_RECENT_SENDS: dict = {}


def sent_recently(customer: str, what: str, within_seconds: int) -> bool:
    sent_at = _RECENT_SENDS.get((customer, what))
    return sent_at is not None and datetime.now(timezone.utc).timestamp() - sent_at < within_seconds


def mark_sent(customer: str, what: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    if len(_RECENT_SENDS) > 5000:
        for key in [k for k, t in _RECENT_SENDS.items() if now - t > 86400]:
            _RECENT_SENDS.pop(key, None)
    _RECENT_SENDS[(customer, what)] = now


DOC_RESEND_SECONDS = 6 * 3600
MAIN_MENU_RESEND_SECONDS = 30 * 60


# Mount static folder for serving PDFs, images, and admin interface
app.mount("/static", StaticFiles(directory="static"), name="static")
# Photos, videos, voice notes and documents sent or received on WhatsApp (media_service.py).
app.mount("/media", StaticFiles(directory=media_service.MEDIA_DIR), name="media")

# HTTP Security Headers (defense-in-depth across all responses)
app.add_middleware(SecurityHeadersMiddleware)

# Rate-limit noisy/abusable public endpoints (per client IP)
app.add_middleware(RateLimitMiddleware, limits={
    "/webhook": (120, 60),                    # Meta webhook traffic can burst
    "/api/upload/product-image": (10, 60),    # image uploads
    "/api/admin/login": (10, 60),             # brute-force protection
    "/api/signup": (5, 600),                  # each sign-up creates a business
    "/api/auth/forgot-password": (5, 300),    # anti-spam password reset requests
    "/api/superadmin/accounts": (30, 60),     # account management operations
})

# Websites on other addresses (such as the Business Sender site) may call the API from a
# browser only when listed in CORS_ORIGINS. Sign-in is a bearer token, never a cookie, so
# credentials stay off.
_cors_origins = [o.strip().rstrip("/") for o in settings.CORS_ORIGINS.split(",") if o.strip() and o.strip() != "*"]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


# ---------------------------------------------------------------------------
# ADMIN AUTHENTICATION
# ---------------------------------------------------------------------------

def _is_expired(expires_at: datetime) -> bool:
    """SQLite returns naive datetimes even for DateTime(timezone=True) columns; we always
    write these as UTC, so treat a naive value as UTC before comparing."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


async def get_current_admin(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
) -> AdminUser:
    """FastAPI dependency: validates the Bearer session token and returns the logged-in admin."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    admin = await get_admin_from_token(token, db)
    if not admin:
        raise HTTPException(status_code=401, detail="Session expired or invalid — please log in again")
    return admin


async def require_owner(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """FastAPI dependency: like get_current_admin, but only allows 'owner' role accounts through."""
    if admin.role != "owner":
        raise HTTPException(status_code=403, detail="Only owner accounts can perform this action")
    return admin


# ---------------------------------------------------------------------------
# Business isolation. Platform accounts ('owner', 'staff') see every business; a 'client'
# (a business owner who signed up) sees only their own. A business that isn't yours answers
# 404, exactly like one that doesn't exist, so its existence isn't revealed either.
# ---------------------------------------------------------------------------

PLATFORM_ROLES = ("owner", "staff")


def is_platform_admin(admin: AdminUser) -> bool:
    return admin.role in PLATFORM_ROLES


def can_access(admin: AdminUser, tenant_id: Optional[int]) -> bool:
    return is_platform_admin(admin) or (admin.tenant_id is not None and tenant_id == admin.tenant_id)


def ensure_tenant_access(admin: AdminUser, tenant_id: Optional[int]) -> None:
    if not can_access(admin, tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")


async def require_platform_admin(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """FastAPI dependency: platform owners and staff only (never a business owner's account)."""
    if not is_platform_admin(admin):
        raise HTTPException(status_code=403, detail="Only the platform's own staff can do this")
    return admin


async def get_admin_from_token(token: str, db: AsyncSession) -> Optional[AdminUser]:
    """Same validation as get_current_admin, but for contexts without a Header (e.g. WebSocket query param)."""
    if not token:
        return None
    stmt = select(AdminSession).where(AdminSession.token == token)
    admin_session = (await db.execute(stmt)).scalar_one_or_none()
    if not admin_session or _is_expired(admin_session.expires_at):
        return None

    user_stmt = select(AdminUser).where(AdminUser.id == admin_session.admin_user_id, AdminUser.is_active == True)  # noqa: E712
    return (await db.execute(user_stmt)).scalar_one_or_none()


@app.post("/api/admin/login", tags=["Admin Auth"])
async def admin_login(login_in: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate an account (platform staff by username, business owners by email) and issue a session token."""
    # Emails are stored lower-case at sign-up, so "Nimal@CityCare.lk" signs in too.
    stmt = select(AdminUser).where(
        func.lower(AdminUser.username) == login_in.username.strip().lower(), AdminUser.is_active == True)  # noqa: E712
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if not admin or not verify_password(login_in.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    token = generate_session_token()
    db.add(AdminSession(admin_user_id=admin.id, token=token, expires_at=session_expiry()))
    admin.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    return {"token": token, "username": admin.username, "role": admin.role, "tenant_id": admin.tenant_id}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_CHARS = 8


@app.get("/api/auth/options", tags=["Admin Auth"])
async def auth_options():
    """What the sign-in page offers: whether new business owners can create accounts."""
    return {"signup_open": settings.ALLOW_SIGNUP}


@app.post("/api/signup", status_code=status.HTTP_201_CREATED, tags=["Admin Auth"])
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Self-service: a business owner creates their account AND their business in one step, and is
    signed straight in. The business starts in test mode (nothing is sent to WhatsApp) until they
    connect their own number; the account only ever sees this one business.
    """
    if not settings.ALLOW_SIGNUP:
        raise HTTPException(status_code=403, detail="New sign-ups are closed. Contact the platform owner for an account.")

    email = payload.email.strip().lower()
    business_name = " ".join(payload.business_name.split())[:255]
    if not _EMAIL_RE.match(email) or len(email) > 100:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(payload.password) < MIN_PASSWORD_CHARS:
        raise HTTPException(status_code=400, detail=f"Use a password of at least {MIN_PASSWORD_CHARS} characters.")
    if not business_name:
        raise HTTPException(status_code=400, detail="Enter your business name.")
    taken = (await db.execute(select(AdminUser).where(func.lower(AdminUser.username) == email))).scalar_one_or_none()
    if taken:
        raise HTTPException(status_code=400, detail="An account with this email already exists. Sign in instead.")

    tenant = Tenant(
        business_name=business_name,
        whatsapp_phone_number_id=f"test-{uuid.uuid4().hex[:16]}",     # test mode until they connect WhatsApp
        meta_access_token="EAA_placeholder_token",
        system_prompt=f"{PLACEHOLDER_PROMPT_START}{business_name}. Answer customers warmly and only from the knowledge base.",
        knowledge_base="No business documentation has been uploaded yet.",
        industry_source="auto",
    )
    db.add(tenant)
    await db.flush()
    user = AdminUser(username=email, password_hash=hash_password(payload.password), role="client",
                     tenant_id=tenant.id, full_name=(payload.full_name or "").strip()[:150] or None)
    db.add(user)
    await db.flush()
    token = generate_session_token()
    db.add(AdminSession(admin_user_id=user.id, token=token, expires_at=session_expiry()))
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(f"Sign-up: '{email}' created business #{tenant.id} '{business_name}'.")
    return {"token": token, "username": email, "role": "client", "tenant_id": tenant.id}


@app.post("/api/admin/logout", tags=["Admin Auth"])
async def admin_logout(authorization: Optional[str] = Header(None), db: AsyncSession = Depends(get_db)):
    """Invalidate the current session token."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        stmt = select(AdminSession).where(AdminSession.token == token)
        admin_session = (await db.execute(stmt)).scalar_one_or_none()
        if admin_session:
            await db.delete(admin_session)
            await db.commit()
    return {"logged_out": True}


@app.get("/api/admin/me", response_model=AdminUserResponse, tags=["Admin Auth"])
async def admin_me(admin: AdminUser = Depends(get_current_admin)):
    """Returns the currently logged-in admin's profile."""
    return admin


@app.get("/api/admin/users", response_model=List[AdminUserResponse], tags=["Admin Auth"])
async def list_admin_users(admin: AdminUser = Depends(require_owner), db: AsyncSession = Depends(get_db)):
    """List all staff/owner accounts. Owner only."""
    stmt = select(AdminUser).order_by(AdminUser.created_at.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.post("/api/admin/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED, tags=["Admin Auth"])
async def create_admin_user(
    user_in: AdminUserCreate,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """Create a new staff or owner account. Owner only."""
    if user_in.role not in ("owner", "staff"):
        raise HTTPException(status_code=400, detail="role must be 'owner' or 'staff'")

    existing_stmt = select(AdminUser).where(AdminUser.username == user_in.username)
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"Username '{user_in.username}' is already taken")

    if len(user_in.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    new_user = AdminUser(username=user_in.username, password_hash=hash_password(user_in.password), role=user_in.role)
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    logger.info(f"Admin '{admin.username}' created new {user_in.role} account: '{user_in.username}'")
    return new_user


@app.put("/api/admin/users/{user_id}", response_model=AdminUserResponse, tags=["Admin Auth"])
async def update_admin_user(
    user_id: int,
    user_update: AdminUserUpdate,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """Update a staff/owner account's password, role, or active status. Owner only."""
    stmt = select(AdminUser).where(AdminUser.id == user_id)
    target = (await db.execute(stmt)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Admin user not found")

    if user_update.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    if user_update.role is not None and user_update.role not in ("owner", "staff"):
        raise HTTPException(status_code=400, detail="role must be 'owner' or 'staff'")

    if user_update.role == "staff" and target.role == "owner":
        owner_count_stmt = select(func.count(AdminUser.id)).where(AdminUser.role == "owner", AdminUser.is_active == True)
        owner_count = (await db.execute(owner_count_stmt)).scalar() or 0
        if owner_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last remaining owner")

    if user_update.password:
        if len(user_update.password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        target.password_hash = hash_password(user_update.password)
    if user_update.role is not None:
        target.role = user_update.role
    if user_update.is_active is not None:
        target.is_active = user_update.is_active

    await db.commit()
    await db.refresh(target)
    return target


@app.delete("/api/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Admin Auth"])
async def delete_admin_user(
    user_id: int,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """Delete a staff/owner account. Owner only. Cannot delete yourself or the last owner."""
    stmt = select(AdminUser).where(AdminUser.id == user_id)
    target = (await db.execute(stmt)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Admin user not found")

    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    if target.role == "owner":
        owner_count_stmt = select(func.count(AdminUser.id)).where(AdminUser.role == "owner", AdminUser.is_active == True)
        owner_count = (await db.execute(owner_count_stmt)).scalar() or 0
        if owner_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining owner")

    await db.delete(target)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# SUPER ADMIN: SYSTEM STATS, ACCOUNTS & PASSWORD RESET MANAGEMENT
# ---------------------------------------------------------------------------

def _generate_temporary_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    pwd = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(max(length - 4, 2))]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _get_server_space_and_ram():
    """Returns disk, RAM, CPU, and host metrics."""
    try:
        disk = shutil.disk_usage(os.path.abspath(os.curdir))
        disk_total_gb = round(disk.total / (1024 ** 3), 2)
        disk_used_gb = round(disk.used / (1024 ** 3), 2)
        disk_free_gb = round(disk.free / (1024 ** 3), 2)
        disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total > 0 else 0.0
    except Exception:
        disk_total_gb, disk_used_gb, disk_free_gb, disk_percent = 0.0, 0.0, 0.0, 0.0

    ram_total_gb = 0.0
    ram_used_gb = 0.0
    ram_free_gb = 0.0
    ram_percent = 0.0
    cpu_percent = 0.0
    boot_time_iso = None
    process_ram_mb = 0.0
    try:
        import psutil
        vm = psutil.virtual_memory()
        ram_total_gb = round(vm.total / (1024 ** 3), 2)
        ram_used_gb = round(vm.used / (1024 ** 3), 2)
        ram_free_gb = round(vm.available / (1024 ** 3), 2)
        ram_percent = round(vm.percent, 1)
        cpu_percent = round(psutil.cpu_percent(interval=0.05), 1)
        boot_time_iso = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat()
        process = psutil.Process()
        process_ram_mb = round(process.memory_info().rss / (1024 ** 2), 2)
    except Exception:
        pass

    db_size_mb = 0.0
    try:
        db_file = os.path.join(os.path.abspath(os.curdir), "whatsapp_bot.db")
        if os.path.exists(db_file):
            db_size_mb = round(os.path.getsize(db_file) / (1024 ** 2), 2)
    except Exception:
        pass

    uptime_secs = int(time.time() - SERVER_START_TIME)
    days = uptime_secs // 86400
    hours = (uptime_secs % 86400) // 3600
    mins = (uptime_secs % 3600) // 60
    secs = uptime_secs % 60
    uptime_human = f"{days}d {hours}h {mins}m {secs}s" if days > 0 else f"{hours}h {mins}m {secs}s" if hours > 0 else f"{mins}m {secs}s"

    return {
        "disk": {
            "total_gb": disk_total_gb,
            "used_gb": disk_used_gb,
            "free_gb": disk_free_gb,
            "used_percent": disk_percent,
        },
        "ram": {
            "total_gb": ram_total_gb,
            "used_gb": ram_used_gb,
            "free_gb": ram_free_gb,
            "used_percent": ram_percent,
            "process_rss_mb": process_ram_mb,
        },
        "cpu": {
            "percent": cpu_percent,
        },
        "system": {
            "os": f"{platform.system()} {platform.release()}",
            "python_version": platform.python_version(),
            "hostname": platform.node(),
            "boot_time": boot_time_iso,
            "db_size_mb": db_size_mb,
            "uptime_seconds": uptime_secs,
            "uptime_human": uptime_human,
        }
    }


@app.get("/api/superadmin/system-stats", tags=["Super Admin"])
async def superadmin_system_stats(
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin endpoint returning:
    1. Server space (Disk usage: total, used, free, percent)
    2. Server RAM usage (total, used, free, percent, process memory)
    3. CPU & System metadata
    4. Account creation stats: total accounts, owners, staff, clients, active, inactive, tenants, pending password resets
    """
    sys_stats = _get_server_space_and_ram()

    # Accounts metrics
    users = (await db.execute(select(AdminUser))).scalars().all()
    total_accounts = len(users)
    owners_count = sum(1 for u in users if u.role == "owner")
    staff_count = sum(1 for u in users if u.role == "staff")
    clients_count = sum(1 for u in users if u.role == "client")
    active_count = sum(1 for u in users if u.is_active)
    inactive_count = total_accounts - active_count

    tenants_count = (await db.execute(select(func.count(Tenant.id)))).scalar() or 0

    pending_resets = (await db.execute(
        select(func.count(PasswordResetRequest.id)).where(PasswordResetRequest.status == "pending")
    )).scalar() or 0
    total_resets = (await db.execute(
        select(func.count(PasswordResetRequest.id))
    )).scalar() or 0

    # Recent 5 accounts
    recent_users = sorted(users, key=lambda u: u.created_at, reverse=True)[:5]
    recent_list = [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "full_name": u.full_name,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in recent_users
    ]

    return {
        **sys_stats,
        "accounts": {
            "total": total_accounts,
            "owners": owners_count,
            "staff": staff_count,
            "clients": clients_count,
            "active": active_count,
            "inactive": inactive_count,
            "tenants": tenants_count,
            "recent": recent_list,
        },
        "resets": {
            "pending": pending_resets,
            "total": total_resets,
        }
    }


@app.get("/api/superadmin/accounts", tags=["Super Admin"])
async def superadmin_list_accounts(
    q: Optional[str] = Query(None, description="Search username or full name"),
    role: Optional[str] = Query(None, description="Filter by role: owner, staff, client, or all"),
    status: Optional[str] = Query(None, description="Filter by status: active, inactive, or all"),
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    List all accounts with business name, search, and filtering. Super Admin (owner) only.
    """
    tenants = (await db.execute(select(Tenant))).scalars().all()
    tenant_map = {t.id: t.business_name for t in tenants}

    stmt = select(AdminUser).order_by(AdminUser.created_at.desc())
    users = (await db.execute(stmt)).scalars().all()

    filtered = []
    q_clean = (q or "").strip().lower()
    for u in users:
        if q_clean:
            name_match = (u.full_name or "").lower()
            user_match = u.username.lower()
            if q_clean not in name_match and q_clean not in user_match:
                continue
        if role and role != "all" and u.role != role:
            continue
        if status and status != "all":
            if status == "active" and not u.is_active:
                continue
            if status == "inactive" and u.is_active:
                continue

        filtered.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": u.is_active,
            "tenant_id": u.tenant_id,
            "tenant_name": tenant_map.get(u.tenant_id),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        })

    return filtered


@app.post("/api/superadmin/accounts", tags=["Super Admin"], status_code=status.HTTP_201_CREATED)
async def superadmin_create_account(
    user_in: SuperAdminAccountCreate,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin create any account (owner, staff, client) with password and optional tenant.
    """
    if user_in.role not in ("owner", "staff", "client"):
        raise HTTPException(status_code=400, detail="Role must be 'owner', 'staff', or 'client'")

    username = user_in.username.strip().lower()
    existing = (await db.execute(select(AdminUser).where(func.lower(AdminUser.username) == username))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"Username '{user_in.username}' is already in use")

    valid, err_msg = validate_password_strength(user_in.password, user_in.username)
    if not valid:
        raise HTTPException(status_code=400, detail=err_msg)

    new_user = AdminUser(
        username=user_in.username.strip(),
        password_hash=hash_password(user_in.password),
        role=user_in.role,
        full_name=user_in.full_name.strip() if user_in.full_name else None,
        tenant_id=user_in.tenant_id if user_in.tenant_id and user_in.tenant_id > 0 else None,
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {
        "id": new_user.id,
        "username": new_user.username,
        "full_name": new_user.full_name,
        "role": new_user.role,
        "is_active": new_user.is_active,
        "tenant_id": new_user.tenant_id,
        "created_at": new_user.created_at.isoformat() if new_user.created_at else None,
    }


@app.put("/api/superadmin/accounts/{user_id}", tags=["Super Admin"])
async def superadmin_update_account(
    user_id: int,
    update_data: SuperAdminAccountUpdate,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin update account details, status, role, tenant, or password.
    """
    target = (await db.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Account not found")

    if update_data.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own Super Admin account")

    if update_data.role is not None:
        if update_data.role not in ("owner", "staff", "client"):
            raise HTTPException(status_code=400, detail="Role must be 'owner', 'staff', or 'client'")
        if update_data.role != "owner" and target.role == "owner":
            owner_count = (await db.execute(select(func.count(AdminUser.id)).where(AdminUser.role == "owner", AdminUser.is_active == True))).scalar() or 0
            if owner_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last remaining owner")
        target.role = update_data.role

    if update_data.full_name is not None:
        target.full_name = update_data.full_name.strip() if update_data.full_name else None

    if update_data.is_active is not None:
        target.is_active = update_data.is_active

    if update_data.tenant_id is not None:
        target.tenant_id = update_data.tenant_id if update_data.tenant_id > 0 else None

    if update_data.password:
        valid, err_msg = validate_password_strength(update_data.password, target.username)
        if not valid:
            raise HTTPException(status_code=400, detail=err_msg)
        target.password_hash = hash_password(update_data.password)

    await db.commit()
    await db.refresh(target)
    return {
        "id": target.id,
        "username": target.username,
        "full_name": target.full_name,
        "role": target.role,
        "is_active": target.is_active,
        "tenant_id": target.tenant_id,
    }


@app.delete("/api/superadmin/accounts/{user_id}", tags=["Super Admin"], status_code=status.HTTP_204_NO_CONTENT)
async def superadmin_delete_account(
    user_id: int,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin delete account. Cannot delete self or last remaining owner.
    """
    target = (await db.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Account not found")

    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own Super Admin account")

    if target.role == "owner":
        owner_count = (await db.execute(select(func.count(AdminUser.id)).where(AdminUser.role == "owner", AdminUser.is_active == True))).scalar() or 0
        if owner_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining owner")

    await db.delete(target)
    await db.commit()
    return None


@app.post("/api/superadmin/accounts/{user_id}/reset-password", tags=["Super Admin"])
async def superadmin_direct_reset_password(
    user_id: int,
    payload: Optional[dict] = None,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin resets any account's password directly.
    Generates a secure temporary password if none provided.
    """
    target = (await db.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Account not found")

    provided = (payload or {}).get("password") if isinstance(payload, dict) else None
    if provided and provided.strip():
        valid, err_msg = validate_password_strength(provided.strip(), target.username)
        if not valid:
            raise HTTPException(status_code=400, detail=err_msg)
        new_pwd = provided.strip()
    else:
        new_pwd = _generate_temporary_password(10)

    target.password_hash = hash_password(new_pwd)

    from sqlalchemy import delete
    await db.execute(delete(AdminSession).where(AdminSession.admin_user_id == target.id))

    await db.commit()
    return {
        "ok": True,
        "user_id": target.id,
        "username": target.username,
        "new_password": new_pwd,
        "message": f"Password for '{target.username}' has been successfully reset."
    }


# ---------------------------------------------------------------------------
# FORGOT PASSWORD & RESET REQUESTS
# ---------------------------------------------------------------------------

@app.post("/api/auth/forgot-password", tags=["Admin Auth"])
async def submit_forgot_password(
    req: ForgotPasswordSubmit,
    db: AsyncSession = Depends(get_db)
):
    """
    Public endpoint: User submits username/email requesting password reset.
    Creates a pending PasswordResetRequest for Super Admin to review and resolve.
    """
    clean_username = req.username.strip()
    if not clean_username:
        raise HTTPException(status_code=400, detail="Please enter your username or email")

    user = (await db.execute(
        select(AdminUser).where(func.lower(AdminUser.username) == clean_username.lower())
    )).scalar_one_or_none()

    if user:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        recent_req = (await db.execute(
            select(PasswordResetRequest).where(
                PasswordResetRequest.username == user.username,
                PasswordResetRequest.status == "pending",
                PasswordResetRequest.requested_at >= cutoff
            )
        )).scalars().first()

        if not recent_req:
            reset_req = PasswordResetRequest(
                username=user.username,
                status="pending",
                requested_at=datetime.now(timezone.utc),
                notes="User requested password reset via login portal."
            )
            db.add(reset_req)
            await db.commit()

    return {
        "ok": True,
        "message": "Password reset request submitted. Your Super Administrator will review and reset your credentials."
    }


@app.get("/api/superadmin/password-resets", tags=["Super Admin"])
async def superadmin_list_password_resets(
    status: Optional[str] = Query("all", description="Status filter: pending, resolved, rejected, all"),
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin view all forgot password requests.
    """
    stmt = select(PasswordResetRequest).order_by(PasswordResetRequest.requested_at.desc())
    if status and status != "all":
        stmt = stmt.where(PasswordResetRequest.status == status)

    requests = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "username": r.username,
            "status": r.status,
            "requested_at": r.requested_at.isoformat() if r.requested_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolved_by": r.resolved_by,
            "temp_password": r.temp_password,
            "notes": r.notes,
        }
        for r in requests
    ]


@app.post("/api/superadmin/password-resets/{request_id}/resolve", tags=["Super Admin"])
async def superadmin_resolve_password_reset(
    request_id: int,
    payload: Optional[PasswordResetResolve] = None,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin resolves a forgot-password request by resetting the user's password.
    Returns the newly generated temporary password to give to the user.
    """
    req = (await db.execute(select(PasswordResetRequest).where(PasswordResetRequest.id == request_id))).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Password reset request not found")

    user = (await db.execute(
        select(AdminUser).where(func.lower(AdminUser.username) == req.username.lower())
    )).scalar_one_or_none()

    if not user:
        req.status = "rejected"
        req.notes = "Account no longer exists."
        req.resolved_by = admin.username
        req.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=404, detail=f"No account found with username '{req.username}'")

    if payload and payload.new_password and len(payload.new_password.strip()) >= 6:
        new_pwd = payload.new_password.strip()
    else:
        new_pwd = _generate_temporary_password(10)

    user.password_hash = hash_password(new_pwd)

    from sqlalchemy import delete
    await db.execute(delete(AdminSession).where(AdminSession.admin_user_id == user.id))

    req.status = "resolved"
    req.temp_password = new_pwd
    req.resolved_by = admin.username
    req.resolved_at = datetime.now(timezone.utc)
    if payload and payload.notes:
        req.notes = payload.notes

    await db.commit()
    return {
        "ok": True,
        "id": req.id,
        "username": req.username,
        "temp_password": new_pwd,
        "message": f"Password reset for '{req.username}'. Provide the temporary password to the user."
    }


@app.post("/api/superadmin/password-resets/{request_id}/reject", tags=["Super Admin"])
async def superadmin_reject_password_reset(
    request_id: int,
    payload: Optional[PasswordResetReject] = None,
    admin: AdminUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db)
):
    """
    Super Admin rejects/dismisses a forgot password request.
    """
    req = (await db.execute(select(PasswordResetRequest).where(PasswordResetRequest.id == request_id))).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Password reset request not found")

    req.status = "rejected"
    req.resolved_by = admin.username
    req.resolved_at = datetime.now(timezone.utc)
    if payload and payload.reason:
        req.notes = payload.reason

    await db.commit()
    return {"ok": True, "id": req.id, "status": "rejected"}


@app.get("/superadmin", tags=["Super Admin Portal"])
@app.get("/super-admin", tags=["Super Admin Portal"])
async def get_superadmin_portal():
    """Serves the Super Admin Dashboard."""
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "superadmin.html")
    if os.path.exists(page_path):
        return FileResponse(page_path)
    return HTMLResponse("<h2>Error: static/superadmin.html not found</h2>", status_code=404)



@app.post("/api/upload/product-image", tags=["Products"])
async def upload_product_image(file: UploadFile = File(...), admin: AdminUser = Depends(get_current_admin)):
    """Upload a product image. Returns the public URL. Max 5 MB. JPEG/PNG/WebP/GIF only."""
    allowed_types = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
    if (file.content_type or "").lower() not in allowed_types:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF images are allowed.")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds the 5 MB limit.")

    images_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "product_images")
    os.makedirs(images_dir, exist_ok=True)

    ext = os.path.splitext(file.filename or "image.jpg")[1].lower() or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(images_dir, filename), "wb") as f:
        f.write(contents)

    logger.info(f"Product image uploaded: {filename}")
    return {"url": f"/static/product_images/{filename}", "filename": filename}


@app.get("/admin", tags=["Admin Portal"])
@app.get("/dashboard", tags=["Admin Portal"])
async def get_admin_dashboard():
    """Serves the modern Admin Control Center for hotel managers to edit package prices & details."""
    admin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "admin.html")
    if os.path.exists(admin_path):
        return FileResponse(admin_path)
    return HTMLResponse("<h2>Admin Portal Error: static/admin.html not found</h2>", status_code=404)


@app.get("/", tags=["Health"])
async def root(request: Request):
    """
    The public home page for browsers. Anything asking for JSON (scripts, uptime checks) still
    gets the service status this URL always returned.
    """
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return {
            "status": "online",
            "service": "Multi-Tenant WhatsApp AI Chatbot Backend",
            "admin_portal": "/admin",
            "docs": "/docs"
        }
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "home.html")
    with open(page_path, encoding="utf-8") as fh:
        page = fh.read()
    industries = "".join(f"<span>{html.escape(choice['label'])}</span>" for choice in INDUSTRY_CHOICES
                         if choice["key"] != "generic")
    page = page.replace("{{PLATFORM_NAME}}", html.escape(settings.PLATFORM_NAME)).replace("{{INDUSTRIES}}", industries)
    return HTMLResponse(page)

from training_service import extract_text_from_pdf
from document_service import (
    add_document,
    create_document,
    delete_document as remove_document,
    get_tenant_documents,
    queue_retrain,
    rebuild_knowledge_base,
    recover_interrupted_trainings,
    run_training,
    update_document,
)
import llm_router
import call_service
import training_sources
import template_service
from prompts.persona_system_prompt import AVAILABLE_TEMPLATES
from tenant_config import build_bot_config, parse_quick_menu
from industry_brain import CONTACT_BUTTON_ID, INDUSTRY_CHOICES, button_phrase, get_pack


@app.get("/train", tags=["Knowledge Base Trainer"])
async def get_bot_trainer_page():
    """Serves the frontend for the Bot Knowledge Trainer."""
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "train.html")
    if os.path.exists(page_path):
        return FileResponse(page_path)
    return HTMLResponse("<h2>Error: static/train.html not found</h2>", status_code=404)


async def _get_tenant_or_404(db: AsyncSession, tenant_id: int, admin: AdminUser) -> Tenant:
    """The business, if this account may see it. Every per-business endpoint goes through here."""
    ensure_tenant_access(admin, tenant_id)
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _document_payload(doc) -> dict:
    return {
        "id": doc.id,
        "title": doc.title,
        "source": doc.source,
        "original_filename": doc.original_filename,
        "file_url": doc.file_url,
        "doc_type": doc.doc_type,
        "trigger_keywords": doc.trigger_keywords,
        "caption": doc.caption,
        "is_active": doc.is_active,
        "char_count": doc.char_count,
        "summary": doc.summary,
        "status": doc.status,
        "training_stage": doc.training_stage,
        "training_progress": doc.training_progress,
        "training_report": json.loads(doc.training_report) if doc.training_report else None,
        "extraction_method": doc.extraction_method,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


# ---------------------------------------------------------------------------
# Bot profile — the per-client values that used to be hardcoded in ai_service.py
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/bot-profile", tags=["Bot Portal"])
async def get_bot_profile(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """The business identity and contact details this tenant's bot speaks with."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    config = build_bot_config(tenant)
    return {
        "tenant_id": tenant.id,
        "business_name": tenant.business_name,
        "bot_name": tenant.bot_name or "",
        "bot_role_title": tenant.bot_role_title or "",
        "business_location": tenant.business_location or "",
        "currency": tenant.currency or "",
        "industry": config.industry,
        "persona_template": config.persona_template,
        "bot_enabled": tenant.bot_enabled,
        "history_days": tenant.history_days,
        "human_handoff_number": tenant.human_handoff_number or "",
        "front_office_number": tenant.front_office_number or "",
        "handoff_rules": config.handoff_rules,
        "quick_menu": tenant.quick_menu or "",
        "quick_buttons": tenant.quick_buttons or "",
        "wa_business_account_id": tenant.wa_business_account_id or "",
        "available_templates": AVAILABLE_TEMPLATES,
        # What each industry gives a business that has not typed its own, so the portal can
        # show them and swap them when the industry is changed.
        "available_industries": [
            {
                **choice,
                "role_title": get_pack(choice["key"]).role_title,
                "handoff_rules": get_pack(choice["key"]).handoff_text,
                "quick_buttons": "\n".join(get_pack(choice["key"]).buttons),
            }
            for choice in INDUSTRY_CHOICES
        ],
    }


@app.patch("/api/tenants/{tenant_id}/bot-profile", tags=["Bot Portal"])
async def update_bot_profile(
    tenant_id: int,
    payload: BotProfileUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Update the bot's identity. No redeploy needed — the next message uses the new values."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)

    if payload.persona_template and payload.persona_template not in AVAILABLE_TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"persona_template must be one of: {', '.join(AVAILABLE_TEMPLATES)}"
        )

    changes = payload.model_dump(exclude_unset=True)

    # Choosing a different industry brings that industry's personality with it, unless this same
    # request names another one.
    new_industry = changes.get("industry")
    if new_industry and new_industry != build_bot_config(tenant).industry:
        if not changes.get("persona_template"):
            changes["persona_template"] = ""
        # The owner picked it: automatic understanding will suggest, never change it again.
        tenant.industry_source = "owner"

    for field, value in changes.items():
        if value is not None:
            setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)
    # Cached answers may quote the old phone numbers or handoff rules.
    from bot_learning import clear_tenant_cache
    await clear_tenant_cache(db, tenant.id)
    logger.info(f"Tenant {tenant.id}: bot profile updated by {admin.username}.")
    return {"message": "Bot profile updated."}


# ---------------------------------------------------------------------------
# Document library — additive training
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/documents", tags=["Bot Portal"])
async def list_tenant_documents(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    documents = await get_tenant_documents(db, tenant.id)
    return {
        "tenant_id": tenant.id,
        "business_name": tenant.business_name,
        "documents": [_document_payload(d) for d in documents],
        "knowledge_base": tenant.knowledge_base,
        "knowledge_base_chars": len(tenant.knowledge_base or ""),
    }


@app.post("/api/tenants/{tenant_id}/documents", tags=["Bot Portal"])
async def upload_tenant_document(
    tenant_id: int,
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    doc_type: str = Form("both"),
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Add one document to this tenant's library. ADDITIVE — previously trained documents
    are kept, and the knowledge base is rebuilt from all of them.

    Returns immediately with the document in 'queued' state; multi-model training
    (training_pipeline.py) runs in the background. Poll GET .../documents for progress.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)

    content = await file.read() if file else None
    filename = file.filename if file else None

    try:
        document = await create_document(
            db, tenant,
            content=content,
            filename=filename,
            pasted_text=text,
            title=title,
            doc_type=doc_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    background_tasks.add_task(run_training, document.id)
    return {
        "message": f"'{document.title}' uploaded. Training has started — it usually takes 1-3 minutes.",
        "document": _document_payload(document),
    }


@app.post("/api/tenants/{tenant_id}/documents/website", tags=["Bot Portal"])
async def train_from_website(tenant_id: int, payload: WebsiteTrain, background_tasks: BackgroundTasks,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Read the business's website (the home page and up to 7 of its most useful pages: prices, menu,
    delivery, contact...) and train it like a document.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        site = await training_sources.read_website(payload.url)
        document = await create_document(db, tenant, pasted_text=site["text"], title=f"Website: {site['title']}"[:255],
                                         doc_type="knowledge")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    document.source = "website"
    document.original_filename = site["url"][:255]
    await db.commit()
    background_tasks.add_task(run_training, document.id)
    return {"message": f"Read {len(site['pages'])} page(s) from {site['url']}. Training has started.",
            "pages": site["pages"], "document": _document_payload(document)}


@app.post("/api/tenants/{tenant_id}/documents/{document_id}/refresh-website", tags=["Bot Portal"])
async def refresh_website_document(tenant_id: int, document_id: int, background_tasks: BackgroundTasks,
                                   db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """The website changed (new prices...): read it again and retrain."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    document = (await db.execute(select(TenantDocument).where(
        TenantDocument.id == document_id, TenantDocument.tenant_id == tenant.id))).scalar_one_or_none()
    if not document or document.source != "website":
        raise HTTPException(status_code=404, detail="Website document not found")
    if document.status in ("queued", "processing"):
        raise HTTPException(status_code=409, detail="This document is already training.")
    try:
        site = await training_sources.read_website(document.original_filename or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    document.raw_text = site["text"]
    document.char_count = len(site["text"])
    document = await queue_retrain(db, document)
    background_tasks.add_task(run_training, document.id)
    return {"message": f"Read {len(site['pages'])} page(s) again. Retraining.", "document": _document_payload(document)}


@app.post("/api/tenants/{tenant_id}/documents/photos", tags=["Bot Portal"])
async def train_from_photos(tenant_id: int, background_tasks: BackgroundTasks, files: List[UploadFile] = File(...),
                            title: Optional[str] = Form(None),
                            db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Photos of a menu, price board or product list. They become one PDF (a page per photo) that the AI
    vision readers read - two different models on each page, cross-checking every number.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    photos = [(await f.read(), f.filename or "photo") for f in files]
    try:
        pdf = await asyncio.to_thread(training_sources.photos_to_pdf, photos)
        name = f"photos-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.pdf"
        document = await create_document(db, tenant, content=pdf, filename=name,
                                         title=(title or f"Photos ({len(photos)})")[:255], doc_type="knowledge")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    document.source = "photos"
    await db.commit()
    background_tasks.add_task(run_training, document.id)
    return {"message": f"{len(photos)} photo(s) uploaded. The AI is reading them - usually 1-3 minutes.",
            "document": _document_payload(document)}


@app.post("/api/tenants/{tenant_id}/faq/import", tags=["Bot Learning"])
async def import_faq(tenant_id: int, file: UploadFile = File(...),
                     db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """A spreadsheet (.csv or .xlsx): questions in column A, answers in column B. Each row is taught at once."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="The file is over 5 MB.")
    try:
        pairs = training_sources.parse_faq(content, file.filename or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = await teach_many(db, tenant, pairs, source="faq")
    return {"message": f"Taught {result['added']} new answer(s), updated {result['updated']}.", **result}


@app.post("/api/tenants/{tenant_id}/learn-from-chats", tags=["Bot Learning"])
async def learn_from_chats_api(tenant_id: int, background_tasks: BackgroundTasks, days: int = Query(90, ge=1, le=365),
                               db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Look through chats where your team replied (last `days`): general facts they told customers become
    suggested answers in "Questions to answer", for you to check and save.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    background_tasks.add_task(learn_from_past_chats, tenant.id, days)
    return {"message": "Looking through your team's replies. Suggestions appear in 'Questions to answer' in a minute or two."}


@app.patch("/api/tenants/{tenant_id}/documents/{document_id}", tags=["Bot Portal"])
async def edit_tenant_document(
    tenant_id: int,
    document_id: int,
    payload: TenantDocumentUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    stmt = select(TenantDocument).where(
        TenantDocument.id == document_id, TenantDocument.tenant_id == tenant.id
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        document = await update_document(db, document, tenant, **payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": "Document updated.", "document": _document_payload(document)}


@app.post("/api/tenants/{tenant_id}/documents/{document_id}/retrain", tags=["Bot Portal"])
async def retrain_tenant_document(
    tenant_id: int,
    document_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Re-run the full multi-model pipeline on a stored document (re-reads the original PDF
    with AI vision). The previous summary stays live until the new one succeeds.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    stmt = select(TenantDocument).where(
        TenantDocument.id == document_id, TenantDocument.tenant_id == tenant.id
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if document.status in ("queued", "processing"):
        raise HTTPException(status_code=409, detail="This document is already training.")
    try:
        document = await queue_retrain(db, document)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    background_tasks.add_task(run_training, document.id)
    return {"message": "Retraining started.", "document": _document_payload(document)}


@app.delete("/api/tenants/{tenant_id}/documents/{document_id}", tags=["Bot Portal"])
async def delete_tenant_document(
    tenant_id: int,
    document_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    stmt = select(TenantDocument).where(
        TenantDocument.id == document_id, TenantDocument.tenant_id == tenant.id
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    await remove_document(db, document, tenant)
    return {"message": "Document deleted and knowledge base rebuilt."}


@app.get("/api/tenants/{tenant_id}/understanding", tags=["Bot Portal"])
async def get_business_understanding(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """What the system worked out about this business from its documents: industry, confidence, evidence."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return understanding_payload(tenant)


@app.post("/api/tenants/{tenant_id}/understand", tags=["Bot Portal"])
async def recheck_business_understanding(
    tenant_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Work out the business type again from the current documents, fill empty profile fields, then re-test."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    kb = (tenant.knowledge_base or "").strip()
    if not kb or kb == "No business documentation has been uploaded yet.":
        raise HTTPException(status_code=400, detail="Upload at least one document first - that is what the system reads.")
    background_tasks.add_task(understand_business, tenant.id)
    return {"message": "Reading your documents again. This takes about a minute."}


# ---------------------------------------------------------------------------
# WhatsApp connection — a business owner switches their bot from test mode to their own number
# ---------------------------------------------------------------------------

def _is_test_mode(tenant: Tenant) -> bool:
    return (tenant.whatsapp_phone_number_id or "").startswith("test-") or "placeholder" in (tenant.meta_access_token or "")


def _whatsapp_payload(tenant: Tenant) -> dict:
    test_mode = _is_test_mode(tenant)
    return {
        "connected": not test_mode,
        "phone_number_id": None if test_mode else tenant.whatsapp_phone_number_id,
        # The token itself is never sent back to the browser.
        "webhook_url": f"{settings.BASE_URL.rstrip('/')}/webhook",
        "verify_token": settings.META_VERIFY_TOKEN,
    }


@app.get("/api/tenants/{tenant_id}/whatsapp", tags=["Bot Portal"])
async def get_whatsapp_connection(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Whether this business's bot is live on its own WhatsApp number, and the webhook details for Meta."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return _whatsapp_payload(tenant)


@app.put("/api/tenants/{tenant_id}/whatsapp", tags=["Bot Portal"])
async def connect_whatsapp(
    tenant_id: int,
    payload: WhatsAppConnectRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Connect the business's own WhatsApp number. Meta is asked first whether the token can use the
    number, so a typo is caught here rather than as silently unanswered customers.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    phone_number_id = payload.phone_number_id.strip()
    token = payload.access_token.strip()
    if not phone_number_id.isdigit():
        raise HTTPException(status_code=400, detail="The Phone Number ID is the long number from Meta's API Setup page (digits only).")
    if not token:
        raise HTTPException(status_code=400, detail="Paste the access token from Meta.")
    other = (await db.execute(select(Tenant).where(
        Tenant.whatsapp_phone_number_id == phone_number_id, Tenant.id != tenant.id))).scalar_one_or_none()
    if other:
        raise HTTPException(status_code=400, detail="This WhatsApp number is already connected to another business.")
    try:
        number = await verify_whatsapp_number(phone_number_id, token)
    except WhatsAppCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))

    tenant.whatsapp_phone_number_id = phone_number_id
    tenant.meta_access_token = token
    await db.commit()
    logger.info(f"Tenant {tenant.id}: WhatsApp connected to {number.get('display_phone_number')} by {admin.username}.")
    # A new token: photos and voice notes the old (expired) one couldn't download are fetched now.
    background_tasks.add_task(media_service.fetch_missing, tenant.id, None, 200)
    return {"message": f"Connected to {number.get('display_phone_number') or 'your number'}.", **number, **_whatsapp_payload(tenant)}


# ---------------------------------------------------------------------------
# Live inbox — see every chat, reply by hand, switch the bot off chat by chat
# (inbox_service.py). Used by the portal and by the Business Sender website.
# ---------------------------------------------------------------------------

def _chat_phone(phone: str) -> str:
    """A customer's WhatsApp number as stored: digits only."""
    cleaned = "".join(ch for ch in phone if ch.isdigit())
    if not 7 <= len(cleaned) <= 16:
        raise HTTPException(status_code=400, detail="That doesn't look like a WhatsApp number.")
    return cleaned


@app.get("/api/tenants/{tenant_id}/inbox/chats", tags=["Inbox"])
async def inbox_chats(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Every conversation, newest first: last message, who sent it, and whether the bot may answer."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return {"bot_enabled": tenant.bot_enabled, "chats": await list_chats(db, tenant)}


@app.get("/api/tenants/{tenant_id}/inbox/chats/{phone}/messages", tags=["Inbox"])
async def inbox_messages(tenant_id: int, phone: str, background_tasks: BackgroundTasks,
                         limit: int = Query(300, ge=1, le=1000),
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """One customer's messages, oldest first (the most recent `limit`)."""
    await _get_tenant_or_404(db, tenant_id, admin)
    phone = _chat_phone(phone)
    conv = await get_conversation(db, tenant_id, phone)
    messages = await chat_messages(db, tenant_id, phone, limit)
    if any(m.get("mediaPending") for m in messages):
        # A photo or voice note that couldn't be downloaded earlier: try again now.
        background_tasks.add_task(media_service.fetch_missing, tenant_id, phone)
    return {
        "phone": phone,
        "name": conv.display_name if conv else "",
        # What the bot has understood about this customer (goal, stage, details, open questions),
        # for a person taking over the chat.
        "brief": load_conversation_brief(conv.memory) if conv else {},
        "bot_enabled": conv.bot_enabled if conv else True,
        "needs_human": bool(conv and conv.needs_human),
        "messages": messages,
    }


@app.post("/api/tenants/{tenant_id}/inbox/chats/{phone}/messages", tags=["Inbox"])
async def inbox_send(tenant_id: int, phone: str, payload: InboxSendRequest,
                     db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Send a reply typed by a person. WhatsApp's refusal (most often: the customer last wrote more
    than 24 hours ago) comes back as ok=false with the reason; the message is kept as FAILED.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    phone = _chat_phone(phone)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Type a message first.")
    if len(text) > MAX_REPLY_CHARS:
        raise HTTPException(status_code=400, detail=f"WhatsApp messages can be at most {MAX_REPLY_CHARS} characters.")
    earlier = await find_by_client_id(db, tenant.id, payload.client_msg_id)
    if earlier is not None:
        # The same send repeated (the app lost the first answer): don't message the customer twice.
        return {"message_id": earlier.id, "ok": earlier.status != "FAILED", "error": earlier.error}
    result = await send_as_agent(tenant, phone, text, client_msg_id=payload.client_msg_id)
    logger.info(f"Tenant {tenant.id}: {admin.username} replied to {phone} from the inbox (ok={result['ok']}).")
    return result


@app.post("/api/tenants/{tenant_id}/inbox/messages/{message_id}/retry", tags=["Inbox"])
async def inbox_retry(tenant_id: int, message_id: int, db: AsyncSession = Depends(get_db),
                      admin: AdminUser = Depends(get_current_admin)):
    """Try a failed text message again (it keeps its place in the chat)."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    row = await db.get(ChatHistory, message_id)
    if row is None or row.tenant_id != tenant.id or row.role != "assistant":
        raise HTTPException(status_code=404, detail="Message not found")
    if row.status != "FAILED":
        raise HTTPException(status_code=400, detail="Only a message that failed can be retried.")
    if (row.message_type or "text") != "text":
        raise HTTPException(status_code=400, detail="Only text messages can be retried from here.")
    return await send_as_agent(tenant, row.customer_phone_number, row.content, retry_of=row.id)


@app.patch("/api/tenants/{tenant_id}/inbox/chats/{phone}", tags=["Inbox"])
async def inbox_set_chat_bot(tenant_id: int, phone: str, payload: ChatBotUpdate,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Bot on or off for this one customer. Off: their messages are saved and a person replies."""
    await _get_tenant_or_404(db, tenant_id, admin)
    conv = await set_chat_bot(db, tenant_id, _chat_phone(phone), payload.bot_enabled)
    return {"phone": conv.customer_phone, "bot_enabled": conv.bot_enabled, "needs_human": conv.needs_human}


@app.patch("/api/tenants/{tenant_id}/bot", tags=["Inbox"])
async def inbox_set_business_bot(tenant_id: int, payload: BusinessBotUpdate,
                                 db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """The master switch: off = no automatic replies to anyone (each chat's own switch is kept)."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    await set_business_bot(db, tenant, payload.enabled)
    return {"bot_enabled": tenant.bot_enabled}


# --- Shared database: sync, contacts, history -------------------------------------------
# One database for the website, the Android app and the bot (sync_service.py).

@app.get("/api/tenants/{tenant_id}/sync", tags=["Sync"])
async def sync_api(tenant_id: int, since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000),
                   db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Messages and contacts changed after `since` (a stamp from the last answer's `cursor`).
    Ask again at once while `hasMore`; once caught up, ask from `cursor - overlapUs` and skip
    ids already held - saves finishing out of order are caught that way.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return await sync_changes(db, tenant, since, limit)


@app.post("/api/tenants/{tenant_id}/inbox/chats/{phone}/record", tags=["Sync"])
async def record_device_message_api(tenant_id: int, phone: str, payload: DeviceMessageRecord,
                                    db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Save a message a phone sent by itself (e.g. a photo). Repeating the upload is harmless."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    row = await record_device_message(db, tenant, _chat_phone(phone), payload.text.strip(), payload.type or "text",
                                      payload.wa_message_id, payload.client_msg_id, payload.status,
                                      payload.error, payload.sent_at)
    return {"message_id": row.id, "status": row.status}


@app.get("/api/tenants/{tenant_id}/contacts", tags=["Sync"])
async def contacts_api(tenant_id: int, q: str = Query("", max_length=100), limit: int = Query(500, ge=1, le=2000),
                       db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Every customer who has messaged the business, kept after old messages are cleaned up."""
    await _get_tenant_or_404(db, tenant_id, admin)
    return {"contacts": await list_contacts(db, tenant_id, q, limit)}


@app.patch("/api/tenants/{tenant_id}/contacts/{phone}", tags=["Sync"])
async def update_contact_api(tenant_id: int, phone: str, payload: ContactUpdate,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Give a customer a name (shown before their WhatsApp name) or notes. Every device sees it."""
    await _get_tenant_or_404(db, tenant_id, admin)
    conv = await update_contact(db, tenant_id, _chat_phone(phone), payload.saved_name, payload.notes)
    return contact_payload(conv)


@app.get("/api/tenants/{tenant_id}/inbox/settings", tags=["Sync"])
async def history_settings_api(tenant_id: int, db: AsyncSession = Depends(get_db),
                               admin: AdminUser = Depends(get_current_admin)):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return {"history_days": tenant.history_days, "allowed": list(HISTORY_DAYS_ALLOWED)}


@app.patch("/api/tenants/{tenant_id}/inbox/settings", tags=["Sync"])
async def update_history_settings_api(tenant_id: int, payload: HistorySettings, db: AsyncSession = Depends(get_db),
                                      admin: AdminUser = Depends(get_current_admin)):
    """How many days of messages to keep (contacts and daily counts are always kept)."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    if payload.history_days not in HISTORY_DAYS_ALLOWED:
        raise HTTPException(status_code=400, detail=f"Choose one of: {', '.join(map(str, HISTORY_DAYS_ALLOWED))} days.")
    tenant.history_days = payload.history_days
    await db.commit()
    return {"history_days": tenant.history_days, "allowed": list(HISTORY_DAYS_ALLOWED)}


@app.get("/api/tenants/{tenant_id}/message-stats", tags=["Sync"])
async def message_stats_api(tenant_id: int, days: int = Query(30, ge=1, le=730), db: AsyncSession = Depends(get_db),
                            admin: AdminUser = Depends(get_current_admin)):
    """Messages in and out per day - also for days whose messages were already cleaned up."""
    await _get_tenant_or_404(db, tenant_id, admin)
    return {"days": await message_stats(db, tenant_id, days)}


# --- Bookings the bot collected from chats (booking_service.py) ---------------------------------

@app.get("/api/tenants/{tenant_id}/bookings", tags=["Bookings"])
async def bookings_api(tenant_id: int, status_filter: Optional[str] = Query(None, alias="status"),
                       db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Booking and appointment requests, newest first. `?status=pending` for the ones waiting for an answer."""
    await _get_tenant_or_404(db, tenant_id, admin)
    if status_filter and status_filter not in BOOKING_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(BOOKING_STATUSES)}")
    return {"bookings": await list_bookings(db, tenant_id, status_filter)}


@app.patch("/api/tenants/{tenant_id}/bookings/{booking_id}", tags=["Bookings"])
async def update_booking_api(tenant_id: int, booking_id: int, payload: BookingUpdate,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Confirm, decline or cancel a booking. Confirm and decline message the customer (in their language)."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    booking = await db.get(Booking, booking_id)
    if booking is None or booking.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != "pending":
        raise HTTPException(status_code=400, detail=f"This booking is already {booking.status}.")
    try:
        sent = await set_booking_status(db, tenant, booking, payload.status, by=admin.full_name or admin.username, message=payload.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.refresh(booking)
    return {"booking": booking_payload(booking), "customer_message": sent}


@app.get("/api/tenants/{tenant_id}/calls", tags=["Calls"])
async def list_calls_api(tenant_id: int, phone: Optional[str] = None,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """WhatsApp voice calls, newest first, with transcript and summary."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return {"calls": await call_service.list_calls(db, tenant.id, phone)}


@app.post("/api/tenants/{tenant_id}/calls", tags=["Calls"])
async def start_call_api(tenant_id: int, payload: CallStart,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Call a customer on WhatsApp; the AI talks, opening with `purpose`. If they haven't allowed calls
    they get WhatsApp's "allow calls?" request first and the call starts when they accept.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        return await call_service.start_outgoing_call(db, tenant, payload.phone, payload.purpose)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tenants/{tenant_id}/calls/ice-servers", tags=["Calls"])
async def call_ice_servers(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """The STUN/TURN servers the website's call audio should use (the same as this server's)."""
    await _get_tenant_or_404(db, tenant_id, admin)
    return {"iceServers": call_service.ice_servers()}


@app.post("/api/tenants/{tenant_id}/calls/team-dial", tags=["Calls"])
async def team_dial_api(tenant_id: int, payload: TeamDial,
                        db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Call a customer from the website and talk yourself (no AI). Send the browser's SDP offer; the answer
    comes back. If they haven't allowed calls they're asked first, and you call again once they accept.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        return await call_service.team_dial(db, tenant, payload.phone, payload.sdp, admin.full_name or admin.username)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tenants/{tenant_id}/calls/{call_id}/answer", tags=["Calls"])
async def team_answer_api(tenant_id: int, call_id: int, payload: BrowserCallOffer,
                          db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Answer a ringing call from the website, or take over a call the AI is on."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        sdp = await call_service.team_answer(tenant, call_id, payload.sdp, admin.full_name or admin.username)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"sdp": sdp}


@app.post("/api/tenants/{tenant_id}/calls/{call_id}/claim", tags=["Calls"])
async def claim_call_api(tenant_id: int, call_id: int,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Answer / Take over was pressed: the AI goes silent at once and the call is held while the audio connects."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        await call_service.claim_call(tenant, call_id, admin.full_name or admin.username)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@app.post("/api/tenants/{tenant_id}/calls/{call_id}/to-ai", tags=["Calls"])
async def let_ai_answer_api(tenant_id: int, call_id: int,
                            db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Stop ringing the team: the AI answers this call now."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    return {"ok": await call_service.let_ai_answer(tenant, call_id)}


@app.post("/api/tenants/{tenant_id}/calls/{call_id}/hangup", tags=["Calls"])
async def hang_up_call_api(tenant_id: int, call_id: int,
                           db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    if not await call_service.hang_up_call(db, tenant.id, call_id):
        raise HTTPException(status_code=404, detail="Call not found")
    return {"message": "Call ended."}


@app.post("/api/tenants/{tenant_id}/inbox/chats/{phone}/media", tags=["Inbox"])
async def inbox_send_media(tenant_id: int, phone: str, file: UploadFile = File(...), caption: str = Form(""),
                           as_type: Optional[str] = Form(None), voice: bool = Form(False),
                           client_msg_id: Optional[str] = Form(None),
                           db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Send a photo, video, audio, document or sticker from the inbox. The type is worked out from the
    file (as_type="document" sends a photo as a file instead); voice=true sends a recording as a voice
    note. Formats WhatsApp won't take are converted (media_service.prepare_outgoing).
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    phone = _chat_phone(phone)
    earlier = await find_by_client_id(db, tenant.id, client_msg_id)
    if earlier is not None:
        return {"message_id": earlier.id, "ok": earlier.status != "FAILED", "error": earlier.error}
    raw = await file.read()
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Too big for WhatsApp (the limit is 100 MB).")
    try:
        ready = await asyncio.to_thread(media_service.prepare_outgoing, raw, file.content_type or "",
                                        file.filename or "", as_type, voice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    data, mime, kind, name = ready["data"], ready["mime"], ready["kind"], ready["name"]
    url = media_service.save_file(data, mime, name)

    async def send():
        media_id = await upload_whatsapp_media(tenant.whatsapp_phone_number_id, data, mime, name or "file",
                                               tenant.meta_access_token)
        await send_whatsapp_media(tenant.whatsapp_phone_number_id, phone, kind, tenant.meta_access_token,
                                  media_id=media_id, caption=caption, filename=name or "", voice=ready["voice"])

    return await send_other_as_agent(tenant, phone, send, media={"url": url, "mime": mime, "name": name},
                                     client_msg_id=client_msg_id)


@app.get("/api/tenants/{tenant_id}/templates", tags=["Inbox"])
async def list_templates_api(tenant_id: int, refresh: bool = False,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """This business's approved WhatsApp templates (from Meta), for messaging customers after 24 hours."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        return {"templates": await template_service.list_templates(tenant, refresh=refresh)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tenants/{tenant_id}/inbox/chats/{phone}/template", tags=["Inbox"])
async def inbox_send_template(tenant_id: int, phone: str, payload: TemplateSend,
                              db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Send an approved template - works however long ago the customer last wrote."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    phone = _chat_phone(phone)
    earlier = await find_by_client_id(db, tenant.id, payload.client_msg_id)
    if earlier is not None:
        return {"message_id": earlier.id, "ok": earlier.status != "FAILED", "error": earlier.error}
    try:
        templates = await template_service.list_templates(tenant)
        template = next((t for t in templates if t["name"] == payload.name and t["language"] == payload.language), None)
        if template is None:
            raise ValueError("That template isn't approved for this number (or its name/language changed).")
        components, shown = template_service.build(template, payload.body, payload.header)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async def send():
        await send_whatsapp_template(tenant.whatsapp_phone_number_id, phone, payload.name, payload.language,
                                     components, tenant.meta_access_token, shown_text=shown)

    return await send_other_as_agent(tenant, phone, send, client_msg_id=payload.client_msg_id)


@app.post("/api/tenants/{tenant_id}/inbox/chats/{phone}/share", tags=["Inbox"])
async def inbox_share(tenant_id: int, phone: str, payload: InboxShare,
                      db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Send a location pin, contact cards, the catalogue, or products from the catalogue."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    phone = _chat_phone(phone)
    pid, token = tenant.whatsapp_phone_number_id, tenant.meta_access_token
    if payload.type == "location":
        if payload.latitude is None or payload.longitude is None:
            raise HTTPException(status_code=400, detail="A location needs latitude and longitude.")
        send = lambda: send_whatsapp_location(pid, phone, payload.latitude, payload.longitude, token,  # noqa: E731
                                              name=payload.name or "", address=payload.address or "")
    elif payload.type == "contacts":
        if not payload.contacts:
            raise HTTPException(status_code=400, detail="Add at least one contact (name and phone).")
        send = lambda: send_whatsapp_contacts(pid, phone, [c.model_dump() for c in payload.contacts], token)  # noqa: E731
    elif payload.type in ("catalog", "products"):
        if not tenant.wa_catalog_id:
            raise HTTPException(status_code=400, detail="Connect a WhatsApp catalogue first (Catalogue settings).")
        if payload.type == "catalog":
            send = lambda: send_whatsapp_catalog(pid, phone, payload.text or "Browse our products here 👇", token)  # noqa: E731
        else:
            ids = [f"p{i}" for i in payload.product_ids or []]
            if not ids:
                raise HTTPException(status_code=400, detail="Choose at least one product.")
            send = lambda: send_whatsapp_products(pid, phone, tenant.wa_catalog_id, ids, token,  # noqa: E731
                                                  body_text=payload.text or "")
    else:
        raise HTTPException(status_code=400, detail="type must be location, contacts, catalog or products.")
    return await send_other_as_agent(tenant, phone, send, client_msg_id=payload.client_msg_id)


@app.get("/api/tenants/{tenant_id}/catalog", tags=["Catalogue"])
async def catalog_status_api(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Is a WhatsApp catalogue connected, when were products last synced, and any error from Meta."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    products = (await db.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    return {**catalog_service.status(tenant),
            "without_image": [p.name for p in products if catalog_service.missing_for_catalogue(p)]}


@app.post("/api/tenants/{tenant_id}/catalog", tags=["Catalogue"])
async def catalog_connect_api(tenant_id: int, payload: CatalogConnect,
                              db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Connect the Commerce Manager catalogue (by its ID), switch on the WhatsApp cart, and sync all products."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        result = await catalog_service.connect(db, tenant, payload.catalog_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**catalog_service.status(tenant), "result": result}


@app.post("/api/tenants/{tenant_id}/catalog/sync", tags=["Catalogue"])
async def catalog_sync_api(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Push every product (name, price, stock, picture) to the WhatsApp catalogue again."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    if not tenant.wa_catalog_id:
        raise HTTPException(status_code=400, detail="No catalogue connected.")
    return await catalog_service.sync(db, tenant)


@app.delete("/api/tenants/{tenant_id}/catalog", tags=["Catalogue"])
async def catalog_disconnect_api(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Stop using the WhatsApp catalogue ("shop" goes back to the product list). Nothing is deleted at Meta."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    tenant.wa_catalog_id = None
    tenant.catalog_error = None
    await db.commit()
    return catalog_service.status(tenant)


@app.get("/shop/{tenant_id}/{product_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_product_page(tenant_id: int, product_id: int, db: AsyncSession = Depends(get_db)):
    """The product page each catalogue item links to (Meta requires a link)."""
    import html as _html
    product = await db.get(Product, product_id)
    tenant = await db.get(Tenant, tenant_id)
    if product is None or tenant is None or product.tenant_id != tenant.id or not product.is_active:
        raise HTTPException(status_code=404, detail="Not found")
    esc = _html.escape
    image = catalog_service._public(product.image_url or "")
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(product.name)}</title><style>body{{font-family:system-ui;max-width:560px;margin:24px auto;padding:0 16px}}"
        "img{width:100%;border-radius:12px}</style></head><body>"
        + (f"<img src='{esc(image)}' alt=''>" if image else "")
        + f"<h1>{esc(product.name)}</h1><p><b>{esc(tenant.currency or 'LKR')} {product.price:,.2f}</b></p>"
        f"<p>{esc(product.description or '')}</p><p>{esc(tenant.business_name or '')} — order on WhatsApp.</p></body></html>")


@app.post("/api/tenants/{tenant_id}/calls/manager-permission", tags=["Calls"])
async def ask_manager_call_permission(tenant_id: int,
                                      db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Ask the manager's WhatsApp to allow calls, so the AI can put customers through to them."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    if not tenant.human_handoff_number:
        raise HTTPException(status_code=400, detail="Set the manager's number (human hand-off number) first.")
    try:
        await call_service.ask_permission(tenant, tenant.human_handoff_number,
                                          "Allow calls so the assistant can put customers through to you.")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Sent. Tap 'Allow' on the manager's WhatsApp."}


@app.post("/api/tenants/{tenant_id}/orders", response_model=OrderResponse, status_code=status.HTTP_201_CREATED, tags=["Orders"])
async def create_manual_order_api(tenant_id: int, payload: ManualOrderCreate,
                                  db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """An order entered by hand (a phone call, a walk-in). Stock is taken as for a WhatsApp checkout."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        order = await create_manual_order(db, tenant, payload.customer_phone, payload.customer_name,
                                          [(line.product_id, line.quantity) for line in payload.items], payload.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return order


@app.websocket("/ws/inbox/{tenant_id}")
async def inbox_websocket(websocket: WebSocket, tenant_id: int, token: Optional[str] = Query(None),
                          calls: Optional[str] = Query(None)):
    """
    Live updates for an open inbox: chat changes, the master switch, and order events. Opens its
    own short database session to check the token, so no session stays open for the socket's life.
    """
    async with AsyncSessionLocal() as db:
        admin = await get_admin_from_token(token, db) if token else None
    if not admin:
        await websocket.close(code=4401)
        return
    if not can_access(admin, tenant_id):
        await websocket.close(code=4404)
        return
    await websocket.accept()
    inbox_manager.register(tenant_id, websocket)
    ws_manager.register(tenant_id, websocket)       # order_created / order_updated too
    if calls == "1":
        call_desks.register(tenant_id, websocket)   # this website answers calls: ring it first
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        inbox_manager.disconnect(tenant_id, websocket)
        ws_manager.disconnect(tenant_id, websocket)
        call_desks.disconnect(tenant_id, websocket)


# ---------------------------------------------------------------------------
# Platform overview — every business on the platform, for the platform's own staff
# ---------------------------------------------------------------------------

@app.get("/api/platform/businesses", tags=["Platform"])
async def platform_businesses(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_platform_admin)):
    """Every business: who owns it, how far its setup has got, whether it is live, and recent activity."""
    from models import KnowledgeGap
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    tenants = (await db.execute(select(Tenant).order_by(Tenant.id.asc()))).scalars().all()
    owners = (await db.execute(select(AdminUser).where(AdminUser.tenant_id.is_not(None)))).scalars().all()
    rows = []
    for t in tenants:
        documents = await get_tenant_documents(db, t.id)
        latest_test = (await db.execute(select(BotTestRun).where(
            BotTestRun.tenant_id == t.id, BotTestRun.status == "done").order_by(BotTestRun.id.desc()).limit(1))).scalars().first()
        messages = (await db.execute(select(func.count(ChatHistory.id)).where(
            ChatHistory.tenant_id == t.id, ChatHistory.role == "user", ChatHistory.created_at >= week_ago))).scalar() or 0
        open_questions = (await db.execute(select(func.count(KnowledgeGap.id)).where(
            KnowledgeGap.tenant_id == t.id, KnowledgeGap.status == "open"))).scalar() or 0
        rows.append({
            "id": t.id,
            "business_name": t.business_name,
            "industry": get_pack(t.industry).label if t.industry else None,
            "owners": [u.username for u in owners if u.tenant_id == t.id],
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "documents": sum(1 for d in documents if d.status == "ready"),
            "test_score": latest_test.score if latest_test else None,
            "whatsapp_connected": not _is_test_mode(t),
            "customer_messages_7d": messages,
            "open_questions": open_questions,
        })
    return {"businesses": rows}


@app.get("/api/industries", tags=["Bot Portal"])
async def list_industries(admin: AdminUser = Depends(get_current_admin)):
    """The industries a business can be set up as (see industry_brain.py)."""
    return INDUSTRY_CHOICES


@app.get("/api/ai/providers", tags=["Bot Portal"])
async def ai_provider_status(admin: AdminUser = Depends(get_current_admin)):
    """Which AI providers are configured, which are paused (rate-limited / no quota), and usage."""
    return llm_router.status()


@app.post("/api/tenants/{tenant_id}/documents/rebuild", tags=["Bot Portal"])
async def rebuild_tenant_knowledge_base(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    knowledge_base = await rebuild_knowledge_base(db, tenant)
    return {"message": "Knowledge base rebuilt.", "knowledge_base": knowledge_base}


@app.post("/api/tenants/{tenant_id}/preview", tags=["Bot Portal"])
async def preview_bot_reply(
    tenant_id: int,
    payload: BotPreviewRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Ask the bot a test question from the portal, using this tenant's real profile,
    knowledge base and documents — without sending anything to WhatsApp.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    documents = await get_tenant_documents(db, tenant.id, active_only=True)
    config = build_bot_config(tenant, documents)
    
    dynamic_kb = await get_dynamic_kb(tenant, db)

    bot_reply = await generate_bot_reply(
        system_prompt=tenant.system_prompt,
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=payload.message,
        config=config,
    )
    reply, attachments = resolve_reply_documents(bot_reply.text, payload.message, config)
    # `unknown` lets the portal offer "teach the bot this answer" right under the reply.
    return {"reply": reply, "handoff": bot_reply.handoff, "unknown": bot_reply.unknown, "attachments": attachments}


# ---------------------------------------------------------------------------
# Automatic testing — runs the bot on questions from its own documents
# ---------------------------------------------------------------------------

@app.post("/api/tenants/{tenant_id}/self-test", tags=["Bot Testing"])
async def start_self_test(
    tenant_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Start an automatic test of this business's bot. Poll GET .../self-test for progress and results."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        run = await start_test_run(db, tenant)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    background_tasks.add_task(run_self_test, run.id)
    return {"message": "Test started. It usually takes 1-2 minutes.", "run": run_payload(run)}


@app.get("/api/tenants/{tenant_id}/self-test", tags=["Bot Testing"])
async def get_self_test(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """The latest test run, plus the score of the one before it for comparison."""
    await _get_tenant_or_404(db, tenant_id, admin)
    runs = (await db.execute(
        select(BotTestRun).where(BotTestRun.tenant_id == tenant_id).order_by(BotTestRun.id.desc()).limit(5)
    )).scalars().all()
    latest = runs[0] if runs else None
    previous = next((r for r in runs[1:] if r.status == "done"), None)
    return {"run": run_payload(latest), "previous_score": previous.score if previous else None}


# ---------------------------------------------------------------------------
# Learning from real chats — questions the bot couldn't answer, answered by the owner
# ---------------------------------------------------------------------------

async def _get_gap_or_404(db: AsyncSession, tenant_id: int, gap_id: int):
    gap = await get_gap(db, tenant_id, gap_id)
    if not gap:
        raise HTTPException(status_code=404, detail="Question not found")
    return gap


@app.get("/api/tenants/{tenant_id}/gaps", tags=["Bot Learning"])
async def list_knowledge_gaps(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Questions customers asked that the bot couldn't answer (open), and the answers taught so far."""
    await _get_tenant_or_404(db, tenant_id, admin)
    gaps = await list_gaps(db, tenant_id)
    return {
        "open": [gap_payload(g) for g in gaps if g.status == "open"],
        "answered": [gap_payload(g) for g in gaps if g.status == "answered"],
        "dismissed_count": sum(1 for g in gaps if g.status == "dismissed"),
    }


@app.post("/api/tenants/{tenant_id}/gaps/{gap_id}/answer", tags=["Bot Learning"])
async def answer_knowledge_gap(
    tenant_id: int,
    gap_id: int,
    payload: GapAnswerRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Teach the bot the answer. It joins the knowledge base, so the next customer gets it."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    gap = await _get_gap_or_404(db, tenant_id, gap_id)
    try:
        gap = await answer_gap(db, tenant, gap, payload.answer, payload.question or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"Tenant {tenant.id}: {admin.username} answered question #{gap.id}.")
    return {"message": "Answer saved. The bot will use it from now on.", "gap": gap_payload(gap)}


@app.post("/api/tenants/{tenant_id}/gaps/{gap_id}/dismiss", tags=["Bot Learning"])
async def dismiss_knowledge_gap(
    tenant_id: int,
    gap_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Ignore an open question, or remove an answer the bot was taught."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    gap = await _get_gap_or_404(db, tenant_id, gap_id)
    await dismiss_gap(db, tenant, gap)
    return {"message": "Removed."}


@app.post("/api/tenants/{tenant_id}/teach", tags=["Bot Learning"])
async def teach_bot_answer(
    tenant_id: int,
    payload: TeachRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Teach a question and answer directly (for example from a failed test question)."""
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    try:
        gap = await teach(db, tenant, payload.question, payload.answer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Answer saved. The bot will use it from now on.", "gap": gap_payload(gap)}


# ---------------------------------------------------------------------------
# Auto Bot Profile Generator & Self-Learning ML
# ---------------------------------------------------------------------------

@app.post("/api/tenants/{tenant_id}/auto-profile", tags=["Bot Profile"])
async def auto_generate_bot_profile(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Analyzes the tenant's knowledge base and auto-generates a complete bot profile:
    bot name, persona, system prompt, handoff rules, quick menu, greeting style, and currency.
    Optionally applies the profile to the tenant row.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    kb = tenant.knowledge_base or ""
    if not kb.strip() or kb.strip() == "No business documentation has been uploaded yet.":
        raise HTTPException(status_code=400, detail="Upload at least one business document before generating a profile.")

    try:
        profile = await generate_bot_profile(kb, business_name_hint=tenant.business_name)
    except llm_router.LLMUnavailable as e:
        # Nothing is applied: this used to overwrite the tenant's prompt, rules and menu with
        # placeholder text whenever the AI providers were busy.
        raise HTTPException(status_code=503, detail=f"No AI provider could generate the profile right now ({e}). Nothing was changed.")

    # Auto-apply the profile to the tenant. The industry decides the persona, so any older
    # persona value is cleared rather than replaced with a tone word no persona is named after.
    tenant.industry = profile.industry
    tenant.persona_template = None
    tenant.bot_name = profile.bot_name
    tenant.bot_role_title = profile.bot_role_title
    tenant.system_prompt = profile.system_prompt
    tenant.handoff_rules = profile.handoff_rules
    tenant.quick_menu = profile.quick_menu or tenant.quick_menu     # keep what they had if the model gave none
    tenant.currency = profile.currency or tenant.currency
    await db.commit()
    from bot_learning import clear_tenant_cache
    await clear_tenant_cache(db, tenant.id)     # answers cached under the old persona/rules

    return {
        "message": "Bot profile auto-generated and applied!",
        "profile": {
            "business_name": profile.business_name,
            "business_type": profile.business_type,
            "industry": profile.industry,
            "bot_name": profile.bot_name,
            "bot_role_title": profile.bot_role_title,
            "persona_template": profile.persona_template,
            "system_prompt": profile.system_prompt,
            "handoff_rules": profile.handoff_rules,
            "quick_menu": profile.quick_menu,
            "greeting_style": profile.greeting_style,
            "currency": profile.currency,
        }
    }


@app.get("/api/tenants/{tenant_id}/learning-stats", tags=["Bot Learning"])
async def get_bot_learning_stats(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Returns self-learning ML statistics for this tenant's bot."""
    await _get_tenant_or_404(db, tenant_id, admin)
    stats = await get_learning_stats(db, tenant_id)
    return stats


@app.get("/api/tenants/{tenant_id}/qa-cache", tags=["Bot Learning"])
async def get_bot_qa_cache(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Returns all cached Q&A pairs the bot has learned for this tenant."""
    await _get_tenant_or_404(db, tenant_id, admin)
    from models import BotQACache
    stmt = select(BotQACache).where(BotQACache.tenant_id == tenant_id).order_by(BotQACache.use_count.desc())
    result = await db.execute(stmt)
    caches = result.scalars().all()
    return {
        "total": len(caches),
        "pairs": [
            {
                "id": c.id,
                "question": c.question,
                "answer": c.answer,
                "use_count": c.use_count,
                "positive_signals": c.positive_signals,
                "negative_signals": c.negative_signals,
                "created_at": str(c.created_at),
            }
            for c in caches
        ]
    }

@app.post("/api/tenants/{tenant_id}/train", tags=["Knowledge Base Trainer"], deprecated=True)
async def train_bot_knowledge_base(
    tenant_id: int,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Legacy trainer endpoint, kept so older clients keep working.

    It no longer overwrites the knowledge base — it adds a document like
    POST /api/tenants/{id}/documents does, and requires an admin session.
    """
    tenant = await _get_tenant_or_404(db, tenant_id, admin)
    content = await file.read() if file else None

    try:
        await add_document(
            db, tenant,
            content=content,
            filename=file.filename if file else None,
            pasted_text=text,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to generate structured knowledge base: {e}")
        raise HTTPException(status_code=500, detail="Failed to train bot with AI.")

    return {"message": "Bot successfully trained!", "knowledge_base": tenant.knowledge_base}


@app.get("/health", tags=["Health"])
async def health_check(db: AsyncSession = Depends(get_db)):
    # Public (uptime monitors call it): says only whether the database answers. The error itself,
    # which can name the database host, goes to the server log.
    try:
        await db.execute(select(1))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.error(f"Health check database error: {e}")
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "unreachable"})


# ---------------------------------------------------------------------------
# META WHATSAPP WEBHOOK ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/webhook", tags=["WhatsApp Webhook"])
async def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge")
):
    """
    Meta Webhook Verification Endpoint.
    Meta sends a GET request to verify the webhook endpoint URL during setup.
    """
    logger.info(f"Received webhook verification request: mode={hub_mode}, token={hub_verify_token}")

    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("Webhook verification successful!")
        # Meta expects the challenge returned as a plain text string / integer response
        return Response(content=hub_challenge, media_type="text/plain", status_code=200)

    logger.warning("Webhook verification failed: Invalid verify token or mode mismatch.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Verification token mismatch"
    )


@app.post("/webhook", tags=["WhatsApp Webhook"])
async def process_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives incoming WhatsApp events from Meta Graph API.

    Acknowledges IMMEDIATELY and does the work in the background. Meta re-delivers webhooks
    that aren't answered quickly, and an AI reply can take seconds on free-tier providers —
    processing inline risked customers receiving the same answer two or three times.
    """
    raw = await request.body()
    if not _valid_meta_signature(raw, request.headers.get("x-hub-signature-256")):
        logger.warning("Rejected a webhook POST with a missing or wrong Meta signature.")
        return Response(content="Invalid signature", status_code=401)
    try:
        body = json.loads(raw)
    except Exception as e:
        logger.error(f"Failed to parse incoming webhook JSON: {e}")
        return Response(content="EVENT_RECEIVED", status_code=200)

    background_tasks.add_task(_process_webhook_in_background, body)
    return Response(content="EVENT_RECEIVED", status_code=200)


def _valid_meta_signature(raw: bytes, header: Optional[str]) -> bool:
    """
    Meta signs each webhook with the app secret: `X-Hub-Signature-256: sha256=<hex HMAC of the
    raw body>`. Without META_APP_SECRET the check is skipped (local testing) - with a warning,
    once, because then anyone who learns the URL could post fake messages.
    """
    secret = settings.META_APP_SECRET.strip()
    if not secret:
        global _signature_warning_shown
        if not _signature_warning_shown:
            logger.warning("META_APP_SECRET is not set: webhook signatures are NOT checked. Set it before going live.")
            _signature_warning_shown = True
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


_signature_warning_shown = False


# One lock per customer, so "hi" and "price?" sent a second apart are answered in order.
_customer_locks: dict = {}


def _webhook_customer_key(body: dict) -> Optional[str]:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []) or []:
                if message.get("from"):
                    return f"{value.get('metadata', {}).get('phone_number_id')}:{message['from']}"
    return None


async def _process_webhook_in_background(body: dict) -> None:
    """Runs after Meta already got its 200. Uses its own DB session (the request's is closed)."""
    key = _webhook_customer_key(body)
    slot = None
    if key:
        # [lock, users]: the lock is dropped only when no task holds or waits for it.
        slot = _customer_locks.setdefault(key, [asyncio.Lock(), 0])
        slot[1] += 1
    try:
        async with AsyncSessionLocal() as db:
            if slot:
                async with slot[0]:
                    await _handle_webhook_body(body, db)
            else:
                await _handle_webhook_body(body, db)    # delivery/read receipts: no ordering needed
    except Exception:
        logger.exception("Background webhook processing failed")
    finally:
        if slot:
            slot[1] -= 1
            if slot[1] == 0:
                _customer_locks.pop(key, None)


async def _handle_webhook_body(body: dict, db: AsyncSession):
    """Processes one webhook payload: resolves the tenant, replies to each message."""
    logger.info(f"Incoming Webhook Payload: {body}")

    # Extract entry data
    entries = body.get("entry", [])
    if not entries:
        return Response(content="EVENT_RECEIVED", status_code=200)

    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")
            # entry.id is the WhatsApp Business Account: where this number's templates live.
            if phone_number_id and entry.get("id"):
                waba_tenant = (await db.execute(
                    select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id))).scalar_one_or_none()
                if waba_tenant and waba_tenant.wa_business_account_id != entry["id"]:
                    await template_service.remember_waba(db, waba_tenant, str(entry["id"]))

            # WhatsApp voice calls (call_service.py): incoming offers, answers to our calls, hang-ups.
            if (value.get("calls") or change.get("field") == "calls") and phone_number_id:
                call_tenant = (await db.execute(
                    select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id))).scalar_one_or_none()
                if call_tenant:
                    await call_service.handle_call_events(call_tenant, value)

            # Delivery / read receipts for messages sent to customers: the ticks every device shows.
            # Statuses of type "call" (RINGING / ACCEPTED / REJECTED) belong to calls we placed.
            if value.get("statuses") and phone_number_id:
                status_tenant = (await db.execute(
                    select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id))).scalar_one_or_none()
                if status_tenant:
                    call_statuses = [s for s in value["statuses"] if s.get("type") == "call"]
                    message_statuses = [s for s in value["statuses"] if s.get("type") != "call"]
                    if call_statuses:
                        await call_service.handle_call_statuses(status_tenant, call_statuses)
                    if message_statuses:
                        await apply_statuses(db, status_tenant, message_statuses)

            messages = value.get("messages", [])
            if not messages or not phone_number_id:
                continue

            for message in messages:
                try:
                    # "Allow calls?" answered: place the call that was waiting for it.
                    if (message.get("interactive") or {}).get("type") == "call_permission_reply":
                        perm_tenant = (await db.execute(
                            select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id))).scalar_one_or_none()
                        if perm_tenant and not seen_before(message.get("id")):
                            await call_service.handle_permission_reply(
                                perm_tenant, message.get("from", ""), message["interactive"].get("call_permission_reply") or {})
                        continue
                    msg_type = message.get("type")
                    customer_phone = message.get("from")
                    user_text = ""
                    tapped_button_id = ""    # id of the reply button the customer tapped, if any

                    if not customer_phone:
                        continue

                    # Meta retries a webhook it considers slow or failed. Without this, a slow
                    # AI reply could make the customer receive the same answer two or three times.
                    if seen_before(message.get("id")):
                        logger.info(f"Skipping duplicate delivery of WhatsApp message {message.get('id')}")
                        continue

                    # -----------------------------------------------------------
                    # TENANT RESOLVER (Needs to happen first to get tokens for media)
                    # -----------------------------------------------------------
                    stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == phone_number_id)
                    result = await db.execute(stmt)
                    tenant = result.scalar_one_or_none()

                    if not tenant:
                        logger.error(f"No tenant found matching whatsapp_phone_number_id='{phone_number_id}'")
                        continue

                    logger.info(f"Resolved tenant: ID={tenant.id}, Business='{tenant.business_name}'")

                    # -----------------------------------------------------------
                    # LIVE INBOX: every message is saved first, whatever happens next, so the
                    # business sees the whole conversation. The bot then replies only if its
                    # master switch and this chat's switch are both on (inbox_service.py);
                    # otherwise a person answers from the inbox. The manager's own number is
                    # always served, so order CONFIRM / REJECT keeps working while it's paused.
                    # -----------------------------------------------------------
                    contact = next((c for c in value.get("contacts") or [] if c.get("wa_id") == customer_phone), None)
                    customer_name = ((contact or {}).get("profile") or {}).get("name")
                    inbox_text, inbox_kind = describe_incoming(message)
                    incoming_row = await record_incoming(db, tenant.id, customer_phone, inbox_text, inbox_kind,
                                                         message.get("id"), customer_name)
                    # Voice notes are understood before the bot switch is checked: a person
                    # answering a paused chat needs to read what was said, not just "[Voice note]".
                    voice = voice_style = None
                    if msg_type == "audio":
                        voice, voice_style = await hear_voice_note(db, tenant, customer_phone, message)
                        await update_incoming_text(db, incoming_row, voice_inbox_text(voice))
                    # A copy of the photo / video / voice note / document / sticker for the inbox.
                    if msg_type in media_service.MEDIA_KINDS:
                        await media_service.store_incoming(db, tenant, incoming_row, message)

                    conversation = await get_conversation(db, tenant.id, customer_phone)
                    if not bot_may_reply(tenant, conversation) and not is_manager(tenant, customer_phone):
                        logger.info(f"Bot is off for {customer_phone} (tenant {tenant.id}); saved for a person to answer.")
                        continue

                    if msg_type == "text":
                        user_text = message.get("text", {}).get("body", "").strip()
                    elif msg_type == "interactive":
                        interactive_obj = message.get("interactive", {})
                        i_type = interactive_obj.get("type")
                        if i_type == "button_reply":
                            user_text = interactive_obj.get("button_reply", {}).get("title", "").strip()
                            btn_id = interactive_obj.get("button_reply", {}).get("id", "")
                            tapped_button_id = btn_id
                            # Handle order confirmation/rejection by manager via WhatsApp buttons
                            if btn_id.startswith("confirm_ord_"):
                                order_num = btn_id.replace("confirm_ord_", "")
                                stmt_ord = select(Order).where(Order.order_number == order_num, Order.tenant_id == tenant.id)
                                ord_result = await db.execute(stmt_ord)
                                the_order = ord_result.scalars().first()
                                if the_order and the_order.status == "payment_uploaded":
                                    await update_order_status(db, the_order, tenant, "confirmed", confirmed_by="Manager (WhatsApp)")
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text=f"✅ Order *{order_num}* has been confirmed!",
                                        meta_access_token=tenant.meta_access_token
                                    )
                                continue
                            elif btn_id.startswith("reject_ord_"):
                                order_num = btn_id.replace("reject_ord_", "")
                                stmt_ord = select(Order).where(Order.order_number == order_num, Order.tenant_id == tenant.id)
                                ord_result = await db.execute(stmt_ord)
                                the_order = ord_result.scalars().first()
                                if the_order and the_order.status == "payment_uploaded":
                                    await update_order_status(db, the_order, tenant, "cancelled", confirmed_by="Manager (WhatsApp)")
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text=f"❌ Order *{order_num}* has been rejected.",
                                        meta_access_token=tenant.meta_access_token
                                    )
                                continue
                        elif i_type == "list_reply":
                            list_id = interactive_obj.get("list_reply", {}).get("id", "")
                            user_text = interactive_obj.get("list_reply", {}).get("title", "").strip()
                            # Handle product selection from interactive list
                            if list_id.startswith("buy_product_"):
                                try:
                                    prod_id = int(list_id.replace("buy_product_", ""))
                                    product = await get_product_by_id(db, prod_id)
                                    if product and product.stock_quantity > 0:
                                        await add_to_cart(db, tenant.id, customer_phone, product, quantity=1)
                                        cart_items = await get_cart_items(db, tenant.id, customer_phone)
                                        cart_msg = (
                                            f"🛒 Added *{product.name}* to your cart!\n\n"
                                            f"{build_cart_text(cart_items)}"
                                        )
                                        await send_whatsapp_message(
                                            phone_number_id=tenant.whatsapp_phone_number_id,
                                            recipient_phone=customer_phone,
                                            message_text=cart_msg,
                                            meta_access_token=tenant.meta_access_token
                                        )
                                    else:
                                        await send_whatsapp_message(
                                            phone_number_id=tenant.whatsapp_phone_number_id,
                                            recipient_phone=customer_phone,
                                            message_text="Sorry, this product is currently out of stock 😔",
                                            meta_access_token=tenant.meta_access_token
                                        )
                                except Exception as prod_err:
                                    logger.error(f"Product selection error: {prod_err}")
                                continue
                    elif msg_type == "audio":
                        # Heard above (voice_service.py: Whisper, then Gemini when unsure or not English).
                        if voice.status == "ok":
                            user_text = voice_bot_text(voice)
                        else:
                            # Silent, or nobody could listen: say so in the customer's own language.
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=voice_fallback_reply(voice.status, voice_style),
                                meta_access_token=tenant.meta_access_token
                            )
                            continue
                    elif msg_type == "order":
                        # A cart sent from the WhatsApp catalogue (catalog_service.py): it becomes the
                        # bot's cart, then the usual checkout (stock, payment) takes over.
                        cart_items, problems = await catalog_service.order_to_cart(
                            db, tenant, customer_phone, message.get("order") or {})
                        if cart_items:
                            note = ("\n\n⚠️ " + "; ".join(problems)) if problems else ""
                            await send_whatsapp_button_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                body_text=f"🛒 Got your cart!\n\n{build_cart_text(cart_items)}{note}\n\nTap *Checkout* to place the order.",
                                buttons=[{"id": "catalog_checkout", "title": "Checkout"}],
                                meta_access_token=tenant.meta_access_token,
                            )
                        else:
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text="Sorry, none of those items can be ordered right now"
                                             + (f" ({'; '.join(problems)})" if problems else "") + ".",
                                meta_access_token=tenant.meta_access_token,
                            )
                        continue
                    elif msg_type in ("location", "contacts"):
                        # Readable to the bot: "deliver here" with a pin, or a friend's number.
                        user_text = inbox_text
                    elif msg_type == "image":
                        # -----------------------------------------------------------
                        # BANK RECEIPT IMAGE HANDLER
                        # -----------------------------------------------------------
                        image_id = message.get("image", {}).get("id")
                        if image_id:
                            logger.info(f"Image received from {customer_phone} (ID: {image_id}). Checking for pending order...")
                            image_bytes = await download_whatsapp_media(image_id, tenant.meta_access_token)
                            if image_bytes:
                                order, analysis = await process_receipt_upload(db, tenant, customer_phone, image_bytes)
                                if order:
                                    amount_str = f"Rs.{analysis['amount']:,.2f}" if analysis.get("amount") else "could not be read"
                                    receipt_reply = (
                                        f"📸 *Payment Receipt Received!*\n\n"
                                        f"📦 Order: *{order.order_number}*\n"
                                        f"💵 Amount detected: {amount_str}\n\n"
                                        f"We've forwarded your receipt to our team for verification. "
                                        f"You'll receive a confirmation message once approved! ✅🙏"
                                    )
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text=receipt_reply,
                                        meta_access_token=tenant.meta_access_token
                                    )
                                else:
                                    # No pending order — try AI product image classification
                                    logger.info(f"No pending order for {customer_phone}. Attempting product classification from image...")
                                    prod_stmt = select(Product).where(
                                        Product.tenant_id == tenant.id,
                                        Product.is_active == True
                                    )
                                    prod_result = await db.execute(prod_stmt)
                                    catalog_products = prod_result.scalars().all()

                                    if catalog_products:
                                        classification = await classify_product_from_image(image_bytes, catalog_products)
                                        if classification["matched"] and classification["product_name"]:
                                            matched = next(
                                                (p for p in catalog_products if p.name.lower() == classification["product_name"].lower()),
                                                None
                                            )
                                            if matched:
                                                if matched.stock_quantity > 0:
                                                    avail_reply = (
                                                        f"📸 I can see you're asking about *{matched.name}*!\n\n"
                                                        f"✅ *In Stock* — {matched.stock_quantity} unit(s) available\n"
                                                        f"💵 Price: Rs.{matched.price:,.2f}\n"
                                                    )
                                                    if matched.description:
                                                        avail_reply += f"📝 _{matched.description}_\n"
                                                    avail_reply += f"\nWant to order? Type *buy {matched.name}* or tap *shop* to browse all products! 🛒"
                                                else:
                                                    avail_reply = (
                                                        f"📸 I can see you're asking about *{matched.name}*.\n\n"
                                                        f"❌ *Out of Stock* — This item is currently unavailable.\n\n"
                                                        f"Type *shop* to see all available products! 🛍️"
                                                    )
                                                await send_whatsapp_message(
                                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                                    recipient_phone=customer_phone,
                                                    message_text=avail_reply,
                                                    meta_access_token=tenant.meta_access_token
                                                )
                                            else:
                                                await send_whatsapp_message(
                                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                                    recipient_phone=customer_phone,
                                                    message_text="Thank you for the image! 📸 I couldn't find an exact match in our catalog. Type *shop* to browse all available products! 🛍️",
                                                    meta_access_token=tenant.meta_access_token
                                                )
                                        else:
                                            await send_whatsapp_message(
                                                phone_number_id=tenant.whatsapp_phone_number_id,
                                                recipient_phone=customer_phone,
                                                message_text="Thank you for the image! 📸 I couldn't identify a specific product from it. Type *shop* or *products* to browse our catalog! 🛍️",
                                                meta_access_token=tenant.meta_access_token
                                            )
                                    else:
                                        await send_whatsapp_message(
                                            phone_number_id=tenant.whatsapp_phone_number_id,
                                            recipient_phone=customer_phone,
                                            message_text="Thank you for the image! 📸 Could you tell me in a message what you'd like to know about it? 🙏",
                                            meta_access_token=tenant.meta_access_token
                                        )
                        continue
                    else:
                        logger.info(f"Skipping unsupported message type: {msg_type}")
                        continue

                    if not user_text:
                        continue


                    logger.info(
                        f"Message received (type={msg_type}) on Phone Number ID '{phone_number_id}' "
                        f"from '{customer_phone}': '{user_text}'"
                    )

                    # -----------------------------------------------------------
                    # MANAGER ORDER CONFIRM/REJECT VIA TEXT
                    # -----------------------------------------------------------
                    msg_upper = user_text.strip().upper()

                    # Booking requests the bot collected: the manager answers "CONFIRM BK-12" / "DECLINE BK-12".
                    booking_answer = await handle_booking_command(db, tenant, user_text, customer_phone)
                    if booking_answer is not None:
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=booking_answer,
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    # Only the manager's own number may confirm or reject an order. This used to accept the
                    # command from ANY sender, so a customer who knew their own order number could confirm
                    # their own payment by texting it.
                    if (msg_upper.startswith("CONFIRM ORD-") or msg_upper.startswith("REJECT ORD-")) \
                            and is_manager(tenant, customer_phone):
                        action = "confirmed" if msg_upper.startswith("CONFIRM") else "cancelled"
                        order_num = user_text.strip().split(" ", 1)[1].strip().upper()
                        stmt_ord = select(Order).where(Order.order_number == order_num, Order.tenant_id == tenant.id)
                        ord_result = await db.execute(stmt_ord)
                        the_order = ord_result.scalars().first()
                        if the_order and the_order.status == "payment_uploaded":
                            await update_order_status(db, the_order, tenant, action, confirmed_by=f"Manager ({customer_phone})")
                            status_word = "confirmed ✅" if action == "confirmed" else "rejected ❌"
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=f"Order *{order_num}* has been {status_word}",
                                meta_access_token=tenant.meta_access_token
                            )
                        else:
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=f"Could not find a pending order with number *{order_num}*.",
                                meta_access_token=tenant.meta_access_token
                            )
                        continue

                    # -----------------------------------------------------------
                    # SHOPPING CART, CHECKOUT & ORDER TRACKING (TEXT COMMANDS)
                    # -----------------------------------------------------------
                    msg_cmd = user_text.strip().lower()

                    if msg_cmd in ["cart", "my cart", "view cart", "🛒 my cart"]:
                        cart_items = await get_cart_items(db, tenant.id, customer_phone)
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=build_cart_text(cart_items),
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    if msg_cmd in ["clear cart", "empty cart", "clear my cart"]:
                        removed = await clear_cart(db, tenant.id, customer_phone)
                        reply_text = "🗑️ Your cart has been cleared." if removed else "Your cart was already empty."
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=reply_text,
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    remove_match = re.match(r'remove\s+(.+?)(?:\s+from\s+cart)?$', msg_cmd)
                    if remove_match and "cart" not in remove_match.group(1):
                        product_query = remove_match.group(1).strip()
                        removed_ok = await remove_from_cart(db, tenant.id, customer_phone, product_query)
                        if removed_ok:
                            cart_items = await get_cart_items(db, tenant.id, customer_phone)
                            reply_text = f"✅ Removed *{product_query}* from your cart.\n\n{build_cart_text(cart_items)}"
                        else:
                            reply_text = f"I couldn't find *{product_query}* in your cart. Type *cart* to see what's there."
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=reply_text,
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    if msg_cmd in ["checkout", "place order", "confirm order", "check out"]:
                        order, low_stock_products, checkout_error = await checkout_cart(db, tenant, customer_phone)
                        if checkout_error == "empty_cart":
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text="🛒 Your cart is empty. Type *shop* to browse our products first!",
                                meta_access_token=tenant.meta_access_token
                            )
                        elif checkout_error == "out_of_stock":
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text="Sorry, one or more items in your cart just went out of stock. Type *cart* to review and adjust quantities.",
                                meta_access_token=tenant.meta_access_token
                            )
                        elif order:
                            if tenant.wa_catalog_id:
                                # Stock went down: a product that just sold out shows as such in WhatsApp.
                                asyncio.ensure_future(catalog_service.sync_later(
                                    tenant.id, [i.product_id for i in order.items if i.product_id]))
                            bank_info = tenant.bank_details or "Please contact us for bank details."
                            item_lines = "\n".join(f"• {i.product_name} x{i.quantity} — Rs.{i.subtotal:,.2f}" for i in order.items)

                            payment_section = (
                                f"🏦 *Bank Transfer Details:*\n{bank_info}\n\n"
                                f"📸 After payment, please send a photo of your bank transfer receipt here and we'll process your order!"
                            )
                            if stripe_is_configured():
                                payment_link = await create_payment_link(order, tenant)
                                if payment_link:
                                    order.payment_link_url = payment_link
                                    order.payment_provider = "stripe"
                                    await db.commit()
                                    payment_section = (
                                        f"💳 *Pay instantly online:*\n{payment_link}\n\n"
                                        f"— OR —\n\n{payment_section}"
                                    )

                            order_msg = (
                                f"✅ *Order Placed!*\n\n"
                                f"📦 Order #: *{order.order_number}*\n"
                                f"{item_lines}\n\n"
                                f"💵 Total: Rs.{order.total_amount:,.2f}\n\n"
                                f"{payment_section}"
                            )
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=order_msg,
                                meta_access_token=tenant.meta_access_token
                            )
                            if low_stock_products and tenant.human_handoff_number:
                                stock_lines = "\n".join(f"• {p.name}: {p.stock_quantity} left" for p in low_stock_products)
                                try:
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=tenant.human_handoff_number,
                                        message_text=f"⚠️ *Low Stock Alert*\n\n{stock_lines}\n\nConsider restocking soon.",
                                        meta_access_token=tenant.meta_access_token
                                    )
                                except Exception as stock_err:
                                    logger.error(f"Failed to send low stock alert: {stock_err}")
                        continue

                    order_track_match = re.search(r'\bord-[a-z0-9]{4,}\b', msg_cmd, re.IGNORECASE)
                    is_track_query = "track" in msg_cmd or order_track_match
                    if is_track_query:
                        if order_track_match:
                            tracked_order = await get_order_by_number(db, tenant.id, order_track_match.group(0))
                            reply_text = (
                                build_order_status_text(tracked_order) if tracked_order
                                else f"I couldn't find an order matching *{order_track_match.group(0).upper()}*. Please double-check the order number."
                            )
                        else:
                            recent_orders = await get_customer_orders(db, tenant.id, customer_phone, limit=1)
                            reply_text = (
                                build_order_status_text(recent_orders[0]) if recent_orders
                                else "You don't have any orders with us yet. Type *shop* to browse our products! 🛍️"
                            )
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=reply_text,
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    if msg_cmd in ["my orders", "order history", "past orders", "previous orders"]:
                        recent_orders = await get_customer_orders(db, tenant.id, customer_phone, limit=5)
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=build_order_history_text(recent_orders),
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    if msg_cmd in ["pay", "pay online", "pay now", "online payment"]:
                        recent_orders = await get_customer_orders(db, tenant.id, customer_phone, limit=1)
                        payable = recent_orders[0] if recent_orders and recent_orders[0].status == "pending_payment" else None

                        if not payable:
                            pay_reply = "You don't have a pending order to pay for right now. Type *shop* to place one!"
                        elif not stripe_is_configured():
                            pay_reply = "Online payment isn't set up yet — please use the bank transfer details we sent you, or send a receipt photo once you've paid."
                        else:
                            link = payable.payment_link_url
                            if not link:
                                link = await create_payment_link(payable, tenant)
                                if link:
                                    payable.payment_link_url = link
                                    payable.payment_provider = "stripe"
                                    await db.commit()
                            pay_reply = (
                                f"💳 Pay for order *{payable.order_number}* (Rs.{payable.total_amount:,.2f}) securely online:\n👉 {link}"
                                if link else
                                "Sorry, I couldn't generate a payment link right now — please use the bank transfer details instead."
                            )

                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=pay_reply,
                            meta_access_token=tenant.meta_access_token
                        )
                        continue

                    # -----------------------------------------------------------
                    # PRODUCT BROWSING & ORDERING
                    # -----------------------------------------------------------
                    msg_lower_check = user_text.lower().strip()
                    is_shop_query = any(w in msg_lower_check for w in [
                        "shop", "products", "product", "catalog", "catalogue",
                        "buy", "order", "purchase", "what do you sell",
                        "බඩු", "මිලදී", "ගන්න"
                    ])

                    if is_shop_query and tenant.wa_catalog_id and not re.search(r'(?:buy|order|purchase|i want|i need)\s+\S', msg_lower_check):
                        # The WhatsApp catalogue: browse, add to cart and send it inside WhatsApp.
                        try:
                            await send_whatsapp_catalog(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                body_text="🛍️ Browse our products, add what you like to the cart and send it to us here.",
                                meta_access_token=tenant.meta_access_token,
                            )
                            continue
                        except Exception as catalog_err:
                            logger.warning(f"Catalogue message failed, sending the list instead: {catalog_err}")

                    if is_shop_query:
                        products = await get_active_products(db, tenant.id)
                        if products:
                            # Check if user wants to buy a specific product
                            buy_match = re.search(r'(?:buy|order|purchase|i want|i need)\s+(.+)', msg_lower_check)
                            if buy_match:
                                product_query = buy_match.group(1).strip()
                                # Extract quantity if present
                                qty_match = re.search(r'(\d+)\s*(?:x|pcs|pieces|units)?\s*(.*)', product_query)
                                if qty_match:
                                    quantity = int(qty_match.group(1))
                                    product_query = qty_match.group(2).strip() or product_query
                                else:
                                    quantity = 1

                                product = await get_product_by_name(db, tenant.id, product_query)
                                if product and product.stock_quantity >= quantity:
                                    await add_to_cart(db, tenant.id, customer_phone, product, quantity=quantity)
                                    cart_items = await get_cart_items(db, tenant.id, customer_phone)
                                    cart_msg = (
                                        f"🛒 Added *{product.name}* x{quantity} to your cart!\n\n"
                                        f"{build_cart_text(cart_items)}"
                                    )
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text=cart_msg,
                                        meta_access_token=tenant.meta_access_token
                                    )
                                    continue
                                elif product and product.stock_quantity < quantity:
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text=f"Sorry, we only have {product.stock_quantity} of *{product.name}* in stock 😔",
                                        meta_access_token=tenant.meta_access_token
                                    )
                                    continue

                            # Show product catalog as interactive list
                            catalog_text = build_product_catalog_text(products)
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=catalog_text,
                                meta_access_token=tenant.meta_access_token
                            )
                            sections = build_product_list_sections(products)
                            try:
                                await send_whatsapp_list_message(
                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                    recipient_phone=customer_phone,
                                    body_text="👇 Tap below to select a product and place your order:",
                                    button_title="Browse Products",
                                    sections=sections,
                                    meta_access_token=tenant.meta_access_token
                                )
                            except Exception as list_err:
                                logger.error(f"Failed to send product list: {list_err}")
                            continue

                    # Fetch past chat history for context (up to 10 previous turns). The message
                    # being answered was already saved for the inbox; it is left out here because
                    # it is passed separately (counting it twice would make it look repeated).
                    history_stmt = (
                        select(ChatHistory)
                        .where(
                            ChatHistory.tenant_id == tenant.id,
                            ChatHistory.customer_phone_number == customer_phone,
                            ChatHistory.id != incoming_row.id,
                        )
                        .order_by(ChatHistory.created_at.asc(), ChatHistory.id.asc())
                    )
                    history_res = await db.execute(history_stmt)
                    history_records = history_res.scalars().all()

                    formatted_history = [
                        {"role": item.role, "content": item.content} for item in history_records
                    ]

                    # Build THIS tenant's bot config — persona, contact numbers and sendable
                    # documents all come from their own row, never from shared constants.
                    tenant_documents = await get_tenant_documents(db, tenant.id, active_only=True)
                    bot_config = build_bot_config(tenant, tenant_documents)
                    # Who this is and where the chat stands - their name, how long since they last
                    # wrote, what the bot remembers - so the reply continues the conversation.
                    bot_config.conversation = build_conversation_context(conversation, history_records, incoming_row.id)
                    # The live product list, so "how much for 3 of these?" is worked out by code, not guessed.
                    bot_config.catalog = await load_catalog(db, tenant.id)

                    dynamic_kb = await get_dynamic_kb(tenant, db)

                    # Blue ticks and "typing..." while the reply is being written
                    received_at = time.monotonic()
                    await send_whatsapp_typing_indicator(
                        tenant.whatsapp_phone_number_id, message.get("id"), tenant.meta_access_token)

                    # Generate Multilingual AI Response (gpt-4o-mini / Local Simulator)
                    bot_reply = await generate_bot_reply(
                        system_prompt=tenant.system_prompt,
                        knowledge_base=dynamic_kb,
                        chat_history=formatted_history,
                        user_message=user_text,
                        config=bot_config,
                        from_voice=msg_type == "audio",
                    )
                    ai_reply, is_handoff_requested = bot_reply.text, bot_reply.handoff

                    # Remove every [SEND_DOC: ...] tag from the text and pick this tenant's files to
                    # attach: the AI's choice if it named one of THIS tenant's documents, otherwise
                    # the deterministic keyword resolver.
                    ai_reply, docs_to_send = resolve_reply_documents(ai_reply, user_text, bot_config)

                    # The customer's message was saved on arrival, and the reply is saved by the
                    # inbox's send listener once WhatsApp accepts (or refuses) it.

                    # Learning from real chats: a question the documents don't cover goes on the
                    # owner's "Questions to answer" list. Once they answer it, every later customer
                    # gets that answer. (This replaced caching the bot's own replies, which were
                    # stored on every message but never safe to reuse.)
                    if bot_reply.unknown:
                        await record_gap(db, tenant.id, user_text, bot_reply=ai_reply,
                                         language=detect_language_intent(user_text, formatted_history),
                                         customer_phone=customer_phone)

                    # A customer who spoke gets spoken to: voice note first, then the same reply in text
                    if msg_type == "audio" and settings.VOICE_REPLIES and ai_reply:
                        try:
                            spoken = await build_spoken_reply(ai_reply)
                            audio_bytes, audio_mime = await synthesize_voice_note(spoken)
                            if audio_bytes:
                                await send_whatsapp_voice_note(
                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                    recipient_phone=customer_phone,
                                    audio_bytes=audio_bytes,
                                    mime_type=audio_mime,
                                    meta_access_token=tenant.meta_access_token,
                                    spoken_text=spoken,
                                )
                        except Exception as voice_err:
                            logger.error(f"Could not reply with a voice note: {voice_err}")

                    # If they are booking something, record it for the team once the details are complete
                    # (looks at the chat a few seconds from now, after this reply has been saved).
                    capture_booking_later(tenant.id, customer_phone)

                    # Send the reply as separate short messages with a typing-speed pause between them.
                    # Without a real token (local testing) nothing is sent, so nobody needs to wait.
                    pause_for_typing = "placeholder" not in (tenant.meta_access_token or "placeholder")
                    for bubble_index, bubble in enumerate(split_reply_into_bubbles(ai_reply)):
                        pause = typing_pause_seconds(bubble)
                        if bubble_index == 0:
                            pause -= time.monotonic() - received_at
                        else:
                            await send_whatsapp_typing_indicator(
                                tenant.whatsapp_phone_number_id, message.get("id"), tenant.meta_access_token)
                        if pause > 0 and pause_for_typing:
                            await asyncio.sleep(pause)
                        await send_whatsapp_message(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            recipient_phone=customer_phone,
                            message_text=bubble,
                            meta_access_token=tenant.meta_access_token
                        )

                    # Send attached PDF document(s) — but not one this customer received in the
                    # last few hours, unless they explicitly asked for a file again.
                    asked_for_file = any(w in user_text.lower() for w in DOC_REQUEST_WORDS)
                    docs_to_send = [
                        d for d in docs_to_send
                        if asked_for_file or not sent_recently(customer_phone, f"doc:{d.get('url')}", DOC_RESEND_SECONDS)
                    ]
                    for doc_info in docs_to_send:
                        if doc_info and doc_info.get("url"):
                            mark_sent(customer_phone, f"doc:{doc_info['url']}")
                            logger.info(f"Sending PDF document to {customer_phone}: {doc_info['filename']} from URL {doc_info['url']}")
                            try:
                                doc_res = await send_whatsapp_document(
                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                    recipient_phone=customer_phone,
                                    document_url=doc_info["url"],
                                    filename=doc_info["filename"],
                                    caption=doc_info["caption"],
                                    meta_access_token=tenant.meta_access_token
                                )
                                logger.info(f"Successfully delivered PDF document to {customer_phone}: {doc_res}")
                            except Exception as doc_err:
                                logger.error(f"Failed to deliver PDF document to {customer_phone}: {doc_err}")

                    # -----------------------------------------------------------
                    # INTERACTIVE QUICK REPLY BUTTONS & LIST DROPDOWN MENUS
                    # -----------------------------------------------------------
                    msg_lower = user_text.lower().strip()
                    greetings_list = ["hi", "hello", "hey", "ayubowan", "good morning", "good afternoon", "good evening", "start", "ආයුබෝවන්"]
                    is_pure_greeting = msg_lower in greetings_list or (any(g in msg_lower for g in greetings_list) and len(msg_lower.split()) <= 2)

                    # Tracks whether an interactive follow-up (buttons/list/contact info) was
                    # already sent this turn, so the persistent main menu doesn't pile on top.
                    interactive_sent = False

                    # Quick Reply Buttons (Trigger on greetings or general main menu requests)
                    # Only for a greeting or a SHORT menu request ("menu", "options", "help") — not when
                    # "menu" appears inside a question like "Silver menu for 150 guests?".
                    is_menu_request = len(msg_lower.split()) <= 3 and any(
                        w in msg_lower for w in ["option", "options", "help", "menu", "start", "main menu"])
                    should_send_quick_buttons = is_pure_greeting or is_menu_request

                    # The buttons fit the tenant's line of business (a clinic offers "Book Appointment",
                    # not "View Menus"); every tenant used to get the banquet hall's three.
                    quick_buttons = bot_config.quick_buttons()

                    if should_send_quick_buttons:
                        interactive_sent = True
                        try:
                            await send_whatsapp_button_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                body_text="👇 Choose an option below to proceed quickly:",
                                buttons=quick_buttons,
                                meta_access_token=tenant.meta_access_token
                            )
                        except Exception as btn_err:
                            logger.error(f"Failed to send quick reply buttons to {customer_phone}: {btn_err}")

                    # Quick-pick list menu, built from THIS tenant's own quick_menu setting.
                    # A tenant that configured nothing simply gets no list, instead of being
                    # shown another business's venues (which is what used to happen).
                    quick_menu_rows = parse_quick_menu(tenant.quick_menu)

                    if quick_menu_rows:
                        # Fire on generic "show me the options" intent, plus the words the client
                        # used in their own menu titles.
                        menu_words = {w for row in quick_menu_rows for w in row["title"].lower().split() if len(w) > 3}
                        # ...and a tap on either greeting button, whatever this tenant's are called.
                        button_phrases = [button_phrase(b["title"]) for b in quick_buttons if b["id"] != CONTACT_BUTTON_ID]
                        generic_triggers = ["option", "options", "package", "packages", "capacity"] + button_phrases
                        is_short = len(msg_lower.split()) <= 4      # long specific questions get an answer, not a list
                        if is_short and (any(w in msg_lower for w in generic_triggers) or any(w in msg_lower for w in menu_words)):
                            interactive_sent = True
                            list_sections = [{"title": (tenant.business_name or "Options")[:24], "rows": quick_menu_rows}]
                            try:
                                await send_whatsapp_list_message(
                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                    recipient_phone=customer_phone,
                                    body_text="👇 *Select an option below:*",
                                    button_title="Choose",
                                    sections=list_sections,
                                    meta_access_token=tenant.meta_access_token
                                )
                            except Exception as list_err:
                                logger.error(f"Failed to send quick list menu to {customer_phone}: {list_err}")

                    # "Contact Manager" Button Handler — send both phone numbers
                    # The contact button has a different title per industry ("Call Reception",
                    # "Talk to an Agent"), so it is recognised by its id.
                    is_contact_request = tapped_button_id == CONTACT_BUTTON_ID or any(
                        w in msg_lower for w in ["contact manager", "talk to manager", "speak to manager", "call manager"]
                    )
                    if is_contact_request:
                        interactive_sent = True
                        # Contact details come from the tenant row only — no shared fallback
                        # number, so a client who set none never leaks someone else's.
                        contact_parts = ["Here you go 🙏\n"]
                        if bot_config.has_front_office:
                            contact_parts.append(f"📞 *Hotline:* {bot_config.front_office_number}")
                        if bot_config.has_manager:
                            contact_parts.append(f"💬 *WhatsApp Manager:* {bot_config.manager_whatsapp}")
                            contact_parts.append(f"👉 {bot_config.manager_link('Hi, I would like to speak with the manager')}")
                        if len(contact_parts) == 1:
                            contact_parts.append("Our team will get back to you shortly.")
                        else:
                            contact_parts.append("\nThey're really lovely — you're in good hands")
                        contact_msg = "\n".join(contact_parts)
                        try:
                            await send_whatsapp_message(
                                phone_number_id=tenant.whatsapp_phone_number_id,
                                recipient_phone=customer_phone,
                                message_text=contact_msg,
                                meta_access_token=tenant.meta_access_token
                            )
                        except Exception as contact_err:
                            logger.error(f"Failed to send contact info to {customer_phone}: {contact_err}")

                    # -----------------------------------------------------------
                    # PERSISTENT MAIN MENU (shown after replies with no other
                    # interactive follow-up, only for tenants that sell products)
                    # -----------------------------------------------------------
                    if not interactive_sent and not sent_recently(customer_phone, "main_menu", MAIN_MENU_RESEND_SECONDS):
                        active_products = await get_active_products(db, tenant.id)
                        if active_products:
                            mark_sent(customer_phone, "main_menu")
                            menu_buttons = [
                                {"id": "btn_shop", "title": "🛍️ Shop"},
                                {"id": "btn_cart", "title": "🛒 My Cart"},
                                {"id": "btn_track", "title": "📦 Track Order"}
                            ]
                            try:
                                await send_whatsapp_button_message(
                                    phone_number_id=tenant.whatsapp_phone_number_id,
                                    recipient_phone=customer_phone,
                                    body_text="Need anything else? 👇",
                                    buttons=menu_buttons,
                                    meta_access_token=tenant.meta_access_token
                                )
                            except Exception as menu_err:
                                logger.error(f"Failed to send persistent main menu to {customer_phone}: {menu_err}")

                    # Handle Human Handoff if requested. The chat is flagged "Needs you" in the
                    # inbox too, so it's found even when nobody saw the WhatsApp alert.
                    if is_handoff_requested:
                        await mark_needs_human(db, tenant.id, customer_phone)
                    if is_handoff_requested and tenant.human_handoff_number:
                        logger.info(f"Triggering human handoff notification to {tenant.human_handoff_number}")
                        await notify_human_agent(
                            phone_number_id=tenant.whatsapp_phone_number_id,
                            handoff_number=tenant.human_handoff_number,
                            customer_phone=customer_phone,
                            last_user_message=user_text,
                            meta_access_token=tenant.meta_access_token
                        )
                except Exception as msg_err:
                    logger.error(f"Error processing message from {customer_phone}: {msg_err}")


# ---------------------------------------------------------------------------
# TENANT MANAGEMENT CRUD ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/api/tenants", response_model=TenantResponse, status_code=status.HTTP_201_CREATED, tags=["Tenant Admin"])
async def create_tenant(tenant_in: TenantCreate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_platform_admin)):
    """Create a new business tenant. Platform staff only - business owners get theirs from /api/signup."""
    # Check if phone number ID already exists
    stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == tenant_in.whatsapp_phone_number_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tenant with whatsapp_phone_number_id='{tenant_in.whatsapp_phone_number_id}' already exists."
        )

    new_tenant = Tenant(**tenant_in.model_dump())
    # An industry picked on creation is the owner's choice; without one, the system works it out
    # from the documents once they are trained (understanding_service.py).
    new_tenant.industry_source = "owner" if new_tenant.industry else "auto"
    db.add(new_tenant)
    await db.commit()
    await db.refresh(new_tenant)
    logger.info(f"Created new tenant: {new_tenant.business_name} (ID: {new_tenant.id})")
    return new_tenant


@app.get("/api/tenants", response_model=List[TenantResponse], tags=["Tenant Admin"])
async def list_tenants(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all registered tenants - or, for a business owner's account, just their own."""
    stmt = select(Tenant).order_by(Tenant.id.asc())
    if not is_platform_admin(admin):
        stmt = stmt.where(Tenant.id == admin.tenant_id)
    result = await db.execute(stmt)
    return result.scalars().all()


@app.get("/api/tenants/{tenant_id}", response_model=TenantResponse, tags=["Tenant Admin"])
async def get_tenant(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Get tenant details by ID."""
    ensure_tenant_access(admin, tenant_id)
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@app.put("/api/tenants/{tenant_id}", response_model=TenantResponse, tags=["Tenant Admin"])
async def update_tenant(tenant_id: int, tenant_update: TenantUpdate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Update tenant prompt, knowledge base, or Meta credentials."""
    ensure_tenant_access(admin, tenant_id)
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = tenant_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)
    return tenant


@app.delete("/api/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Tenant Admin"])
async def delete_tenant(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_owner)):
    """Delete a tenant by ID, with its owners' accounts (they would have nothing left to see)."""
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    for owner in (await db.execute(select(AdminUser).where(AdminUser.tenant_id == tenant.id))).scalars().all():
        await db.delete(owner)
    await db.delete(tenant)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# ORDERS DASHBOARD PAGE
# ---------------------------------------------------------------------------

@app.get("/orders", tags=["Order Management"])
@app.get("/order-dashboard", tags=["Order Management"])
async def get_orders_dashboard():
    """Serves the Order Management Dashboard for owners/managers."""
    orders_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "orders.html")
    if os.path.exists(orders_path):
        return FileResponse(orders_path)
    return HTMLResponse("<h2>Error: static/orders.html not found</h2>", status_code=404)


# ---------------------------------------------------------------------------
# PRODUCT CRUD API ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/products", response_model=List[ProductResponse], tags=["Products"])
async def list_products(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all products for a tenant."""
    ensure_tenant_access(admin, tenant_id)
    stmt = select(Product).where(Product.tenant_id == tenant_id).order_by(Product.id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.post("/api/tenants/{tenant_id}/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED, tags=["Products"])
async def create_product(tenant_id: int, product_in: ProductCreate, background_tasks: BackgroundTasks,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Add a new product for a tenant."""
    ensure_tenant_access(admin, tenant_id)
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    new_product = Product(tenant_id=tenant_id, **product_in.model_dump())
    db.add(new_product)
    await db.commit()
    await db.refresh(new_product)
    if tenant.wa_catalog_id:
        background_tasks.add_task(catalog_service.sync_later, tenant.id, [new_product.id])
    return new_product


@app.put("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
async def update_product(product_id: int, product_update: ProductUpdate, background_tasks: BackgroundTasks,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Update a product."""
    stmt = select(Product).where(Product.id == product_id)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product or not can_access(admin, product.tenant_id):
        raise HTTPException(status_code=404, detail="Product not found")

    update_data = product_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(product, field, value)

    await db.commit()
    await db.refresh(product)
    # Price, stock or picture changed: the WhatsApp catalogue follows.
    background_tasks.add_task(catalog_service.sync_later, product.tenant_id, [product.id])
    return product


@app.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
async def delete_product(product_id: int, background_tasks: BackgroundTasks,
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Delete a product."""
    stmt = select(Product).where(Product.id == product_id)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product or not can_access(admin, product.tenant_id):
        raise HTTPException(status_code=404, detail="Product not found")

    tenant_id, gone = product.tenant_id, catalog_service.retailer_id(product)
    await db.delete(product)
    await db.commit()
    background_tasks.add_task(catalog_service.sync_later, tenant_id, [-1], [gone])
    return None


# ---------------------------------------------------------------------------
# ORDER MANAGEMENT API ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/orders", response_model=List[OrderResponse], tags=["Orders"])
async def list_orders(tenant_id: int, status_filter: Optional[str] = Query(None, alias="status"), db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all orders for a tenant, optionally filtered by status."""
    ensure_tenant_access(admin, tenant_id)
    stmt = select(Order).where(Order.tenant_id == tenant_id).options(selectinload(Order.items))
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
    stmt = stmt.order_by(Order.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.get("/api/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
async def get_order(order_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Get order details by ID."""
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order or not can_access(admin, order.tenant_id):
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.put("/api/orders/{order_id}/status", response_model=OrderResponse, tags=["Orders"])
async def update_order_status_api(order_id: int, status_update: OrderStatusUpdate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Update order status. Valid transitions:
    pending_payment → payment_uploaded → confirmed → shipped → delivered
    Any status → cancelled
    Triggers automatic WhatsApp notification to the customer.
    """
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order and not can_access(admin, order.tenant_id):
        order = None   # someone else's order: answer exactly as if it didn't exist
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    valid_statuses = ["pending_payment", "payment_uploaded", "confirmed", "shipped", "delivered", "cancelled"]
    if status_update.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    # Get tenant for WhatsApp notifications
    stmt_tenant = select(Tenant).where(Tenant.id == order.tenant_id)
    tenant = (await db.execute(stmt_tenant)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    updated_order = await update_order_status(
        db, order, tenant, status_update.status,
        confirmed_by=status_update.confirmed_by
    )
    return updated_order


# ---------------------------------------------------------------------------
# ANALYTICS DASHBOARD
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/analytics", tags=["Analytics"])
async def get_tenant_analytics(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Aggregate revenue, order-status breakdown, top products, and low-stock alerts."""
    ensure_tenant_access(admin, tenant_id)
    tenant_stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(tenant_stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    paid_statuses = ["confirmed", "shipped", "delivered"]

    total_orders = (await db.execute(
        select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
    )).scalar() or 0

    total_revenue = (await db.execute(
        select(func.coalesce(func.sum(Order.total_amount), 0.0))
        .where(Order.tenant_id == tenant_id, Order.status.in_(paid_statuses))
    )).scalar() or 0.0

    status_rows = (await db.execute(
        select(Order.status, func.count(Order.id))
        .where(Order.tenant_id == tenant_id)
        .group_by(Order.status)
    )).all()
    orders_by_status = {row[0]: row[1] for row in status_rows}

    fourteen_days_ago = datetime.now(timezone.utc) - timedelta(days=13)
    revenue_rows = (await db.execute(
        select(func.date(Order.created_at), func.coalesce(func.sum(Order.total_amount), 0.0))
        .where(
            Order.tenant_id == tenant_id,
            Order.status.in_(paid_statuses),
            Order.created_at >= fourteen_days_ago
        )
        .group_by(func.date(Order.created_at))
    )).all()
    # SQLite returns the day as text, PostgreSQL as a date: key both as "YYYY-MM-DD".
    revenue_by_day = {str(row[0])[:10]: float(row[1] or 0) for row in revenue_rows}

    revenue_trend = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        revenue_trend.append({"date": day, "revenue": round(revenue_by_day.get(day, 0.0), 2)})

    top_products_rows = (await db.execute(
        select(OrderItem.product_name, func.sum(OrderItem.quantity), func.sum(OrderItem.subtotal))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.tenant_id == tenant_id, Order.status != "cancelled")
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
    )).all()
    top_products = [
        {"name": row[0], "quantity_sold": int(row[1]), "revenue": round(row[2], 2)}
        for row in top_products_rows
    ]

    low_stock = await get_low_stock_products(db, tenant_id)

    return {
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "orders_by_status": orders_by_status,
        "revenue_trend": revenue_trend,
        "top_products": top_products,
        "low_stock_products": [
            {"id": p.id, "name": p.name, "stock_quantity": p.stock_quantity, "low_stock_threshold": p.low_stock_threshold}
            for p in low_stock
        ]
    }


# ---------------------------------------------------------------------------
# CUSTOMER BROADCAST
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/customers", tags=["Broadcast"])
async def list_broadcast_customers(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List distinct customer phone numbers who have ordered or chatted with this tenant."""
    ensure_tenant_access(admin, tenant_id)
    phones = await get_broadcast_recipients(db, tenant_id)
    return {"count": len(phones), "phones": phones}


@app.post("/api/tenants/{tenant_id}/broadcast", tags=["Broadcast"])
async def send_broadcast(tenant_id: int, broadcast_in: BroadcastRequest, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Sends a one-time WhatsApp text message to every customer who has ordered or chatted
    with this tenant. NOTE: Meta's WhatsApp Cloud API only permits free-form messages within
    a 24-hour customer service window; sends to customers outside that window may fail unless
    delivered via an approved message template.
    """
    ensure_tenant_access(admin, tenant_id)
    tenant_stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(tenant_stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if not broadcast_in.message.strip():
        raise HTTPException(status_code=400, detail="Broadcast message cannot be empty.")

    recipients = await get_broadcast_recipients(db, tenant_id)
    sent, failed = 0, 0
    for phone in recipients:
        try:
            await send_whatsapp_message(
                phone_number_id=tenant.whatsapp_phone_number_id,
                recipient_phone=phone,
                message_text=broadcast_in.message,
                meta_access_token=tenant.meta_access_token
            )
            sent += 1
        except Exception as e:
            logger.error(f"Broadcast failed for {phone}: {e}")
            failed += 1

    logger.info(f"Broadcast complete for tenant {tenant_id}: sent={sent}, failed={failed}, total={len(recipients)}")
    return {"total_recipients": len(recipients), "sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# INVOICES & CSV EXPORTS
# ---------------------------------------------------------------------------

@app.get("/api/orders/{order_id}/invoice", tags=["Invoices"])
async def get_order_invoice(order_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """
    Downloads a PDF invoice/receipt for an order, for the business it belongs to.

    This used to be open to anyone. Order ids count up 1, 2, 3, so every business's invoices -
    customer names, phone numbers, amounts - could be downloaded in a loop. Nothing sends this
    URL to WhatsApp; the Orders page downloads it with the signed-in session.
    """
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order or not can_access(admin, order.tenant_id):
        raise HTTPException(status_code=404, detail="Order not found")

    tenant_stmt = select(Tenant).where(Tenant.id == order.tenant_id)
    tenant = (await db.execute(tenant_stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    pdf_bytes = generate_invoice_pdf(order, tenant)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=invoice_{order.order_number}.pdf"}
    )


@app.get("/api/tenants/{tenant_id}/orders/export", tags=["Invoices"])
async def export_orders_csv(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Exports all orders for a tenant as a CSV file."""
    ensure_tenant_access(admin, tenant_id)
    stmt = (
        select(Order)
        .where(Order.tenant_id == tenant_id)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
    )
    orders = (await db.execute(stmt)).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Order Number", "Customer Phone", "Customer Name", "Items", "Quantity",
        "Total Amount", "Status", "Payment Provider", "Confirmed By", "Created At"
    ])
    for o in orders:
        items_str = "; ".join(f"{i.product_name} x{i.quantity}" for i in o.items) if o.items else f"{o.product_name} x{o.quantity}"
        writer.writerow([
            o.order_number, o.customer_phone, o.customer_name or "", items_str, o.quantity,
            f"{o.total_amount:.2f}", o.status, o.payment_provider or "bank_transfer",
            o.confirmed_by or "", o.created_at.strftime("%Y-%m-%d %H:%M:%S")
        ])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=orders_tenant_{tenant_id}.csv"}
    )


@app.get("/api/tenants/{tenant_id}/customers/export", tags=["Invoices"])
async def export_customers_csv(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Exports the distinct customer list for a tenant as a CSV file."""
    ensure_tenant_access(admin, tenant_id)
    phones = await get_broadcast_recipients(db, tenant_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Phone Number"])
    for phone in phones:
        writer.writerow([phone])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=customers_tenant_{tenant_id}.csv"}
    )


# ---------------------------------------------------------------------------
# ONLINE PAYMENTS (STRIPE)
# ---------------------------------------------------------------------------

@app.post("/api/webhooks/stripe", tags=["Payments"])
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receives Stripe payment confirmations and auto-confirms the matching order."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    event = verify_and_parse_webhook(payload, sig_header)
    if not event:
        raise HTTPException(status_code=400, detail="Invalid or unconfigured Stripe webhook")

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        order_number = session_obj.get("client_reference_id")
        payment_intent = session_obj.get("payment_intent")

        if order_number:
            stmt = select(Order).where(Order.order_number == order_number).options(selectinload(Order.items))
            order = (await db.execute(stmt)).scalar_one_or_none()
            if order and order.status in ("pending_payment", "payment_uploaded"):
                order.payment_intent_id = payment_intent
                order.paid_at = datetime.now(timezone.utc)
                await db.commit()

                tenant_stmt = select(Tenant).where(Tenant.id == order.tenant_id)
                tenant = (await db.execute(tenant_stmt)).scalar_one_or_none()
                if tenant:
                    await update_order_status(db, order, tenant, "confirmed", confirmed_by="Stripe (auto-confirmed)")
                    logger.info(f"Order {order_number} auto-confirmed via Stripe payment.")

    return {"status": "ok"}


@app.get("/payment/success", response_class=HTMLResponse, tags=["Payments"])
async def payment_success_page(order: Optional[str] = Query(None)):
    """Simple confirmation page Stripe Checkout redirects to after a successful payment."""
    return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
            <h1 style="color:#10b981;">✅ Payment Successful!</h1>
            <p>Your order {f"<strong>{order}</strong>" if order else ""} has been paid.</p>
            <p>You can close this window and return to WhatsApp — we'll message you a confirmation shortly.</p>
        </body></html>
    """)


@app.get("/payment/cancel", response_class=HTMLResponse, tags=["Payments"])
async def payment_cancel_page(order: Optional[str] = Query(None)):
    """Simple page Stripe Checkout redirects to when the customer cancels payment."""
    return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:4rem;">
            <h1 style="color:#f59e0b;">Payment Cancelled</h1>
            <p>No charge was made for order {f"<strong>{order}</strong>" if order else ""}.</p>
            <p>You can close this window and return to WhatsApp to try again or pay via bank transfer instead.</p>
        </body></html>
    """)


# ---------------------------------------------------------------------------
# LIVE DASHBOARD UPDATES (WEBSOCKET)
# ---------------------------------------------------------------------------

@app.websocket("/ws/orders/{tenant_id}")
async def orders_websocket(
    websocket: WebSocket,
    tenant_id: int,
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """Pushes order_created / order_updated events to connected dashboards in real time."""
    admin = await get_admin_from_token(token, db) if token else None
    if not admin:
        await websocket.close(code=4401)
        return
    if not can_access(admin, tenant_id):
        await websocket.close(code=4404)     # another business's live orders
        return

    await ws_manager.connect(tenant_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(tenant_id, websocket)
