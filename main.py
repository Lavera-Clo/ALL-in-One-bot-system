import asyncio
import csv
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
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from business_idea_service import generate_business_ideas, create_pdf_from_text

from config import settings
from database import init_db, get_db, AsyncSessionLocal
from models import Tenant, TenantDocument, ChatHistory, Product, Order, OrderItem, AdminUser, AdminSession

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
    BotProfileUpdate,
    TenantDocumentUpdate,
    BotPreviewRequest
)
from ai_service import (
    generate_ai_response, get_pdfs_for_user_message, resolve_reply_documents, DOC_REQUEST_WORDS,
    transcribe_audio, classify_product_from_image,
)
from whatsapp_client import (
    send_whatsapp_message,
    send_whatsapp_document,
    send_whatsapp_image,
    send_whatsapp_button_message,
    send_whatsapp_list_message,
    notify_human_agent,
    download_whatsapp_media
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
    mark_cart_reminded
)
from auth_service import hash_password, verify_password, generate_session_token, session_expiry
from payment_service import stripe_is_configured, create_payment_link, verify_and_parse_webhook
from invoice_service import generate_invoice_pdf
from realtime import manager as ws_manager
from rate_limit import RateLimitMiddleware
from bot_profile_generator import generate_bot_profile
from bot_learning import get_cached_answer, cache_qa_pair, record_signal, get_learning_stats

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

ABANDONED_CART_CHECK_INTERVAL_SECONDS = 300

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

    # Automatically seed Wasala Nature Resort tenant if not present
    async with AsyncSessionLocal() as session:
        stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == "1157440684128924")
        existing = (await session.execute(stmt)).scalar_one_or_none()
        NEW_TOKEN = "EAAjfYY4wqiYBSWivwPb8V5cB6wbqZC0b2pLr6fjzTAuVqfU57FQc5V7fd4uYCZCFnzCUwHAF0sRqmQCnsAgjVnWrAsvLgHZAalZBwgRg2fONnit5IZBrx8Sdnyf91C8StvHrM5RBaPIzfQKJMgR3P3veTRi6gtfZA1jO38zUirUPdTpUjGRAxN7KGEZBSuuihrLsQZDZD"
        if not existing:
            wasala_tenant = Tenant(
                business_name="Wasala Nature Resort - Banquet & Events",
                whatsapp_phone_number_id="1157440684128924",
                meta_access_token=NEW_TOKEN,
                system_prompt=(
                    "You are the senior Banquet & Event Planning AI Consultant for Wasala Nature Resort in Bentota, Sri Lanka. "
                    "Answer guests warmly and accurately in English, Sinhala, or Singlish with official package prices."
                ),
                knowledge_base=(
                    "=== WASALA NATURE RESORT - BANQUET & EVENTS MASTER KNOWLEDGE BASE ===\n\n"
                    "🏛️ BANQUET HALLS AVAILABLE:\n"
                    "1. Grand Ballroom (Capacity 150-650 guests)\n"
                    "2. Starlight Ballroom (Capacity 100-350 guests)\n\n"
                    "📊 OFFICIAL BANQUET PACKAGE PRICING MATRIX (TOTAL LKR):\n\n"
                    "SILVER MENU PACKAGES:\n"
                    "- 100 Pax (Grand Ballroom): Rs. 539,500\n"
                    "- 100 Pax (Starlight Ballroom): Rs. 479,500\n"
                    "- 150 Pax (Grand Ballroom): Rs. 708,000\n"
                    "- 150 Pax (Starlight Ballroom): Rs. 678,000\n"
                    "- 200 Pax: Rs. 876,500\n"
                    "- 250 Pax: Rs. 1,075,000\n"
                    "- 300 Pax: Rs. 1,273,500\n\n"
                    "GOLD MENU PACKAGES:\n"
                    "- 100 Pax (Grand Ballroom): Rs. 589,500\n"
                    "- 100 Pax (Starlight Ballroom): Rs. 529,500\n"
                    "- 150 Pax (Grand Ballroom): Rs. 783,000\n"
                    "- 150 Pax (Starlight Ballroom): Rs. 753,000\n"
                    "- 200 Pax: Rs. 976,500\n"
                    "- 250 Pax: Rs. 1,200,000\n"
                    "- 300 Pax: Rs. 1,423,500\n\n"
                    "PREMIER MENU PACKAGES:\n"
                    "- 100 Pax (Grand Ballroom): Rs. 709,000\n"
                    "- 100 Pax (Starlight Ballroom): Rs. 649,000\n"
                    "- 150 Pax (Grand Ballroom): Rs. 917,500\n"
                    "- 150 Pax (Starlight Ballroom): Rs. 887,500\n"
                    "- 200 Pax: Rs. 1,143,500\n"
                    "- 250 Pax: Rs. 1,397,500\n"
                    "- 300 Pax: Rs. 1,647,500\n\n"
                    "🍽️ EXTRA PLATE RATES (PER EXTRA GUEST):\n"
                    "- Silver Package Extra Plate: Rs. 4,170.00\n"
                    "- Gold Package Extra Plate: Rs. 4,770.00\n"
                    "- Premier Menu Extra Plate: Rs. 5,370.00\n\n"
                    "👥 PER-HEAD BREAKDOWN RATES:\n"
                    "1. Starlight Ballroom: Silver Rs. 3,395 | Gold Rs. 3,895 | Premier Rs. 4,795\n"
                    "2. Grand Ballroom (150-200 Pax): Silver Rs. 3,595 | Gold Rs. 4,095\n"
                    "3. More Than 200 Pax (All Halls): Silver Rs. 3,395 | Gold Rs. 3,895\n\n"
                    "📄 OFFICIAL BROCHURE PDF:\n"
                    "- 2026 Wedding Packages Brochure: https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf\n\n"
                    "📋 POLICIES:\n"
                    "- Advance Booking Deposit: 25% to hold date. Noise cutoff: 11:30 PM."
                ),
                human_handoff_number="+94779998877",
                front_office_number="+94342275000"
            )
            session.add(wasala_tenant)
            await session.commit()
            logger.info("Successfully auto-seeded Wasala Nature Resort tenant into database!")
        else:
            existing.meta_access_token = NEW_TOKEN
            await session.commit()
            logger.info("Successfully updated Meta access token for existing tenant in database!")

        # Bootstrap the first owner admin account, only when no admin users exist yet.
        admin_count_stmt = select(func.count(AdminUser.id))
        admin_count = (await session.execute(admin_count_stmt)).scalar() or 0
        if admin_count == 0:
            first_owner = AdminUser(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                role="owner"
            )
            session.add(first_owner)
            await session.commit()
            logger.info(f"Bootstrapped first owner admin account: '{settings.ADMIN_USERNAME}'")

    abandoned_cart_task = asyncio.create_task(abandoned_cart_worker())

    yield

    abandoned_cart_task.cancel()
    logger.info("Shutting down WhatsApp AI Chatbot service...")


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

# Rate-limit noisy/abusable public endpoints (per client IP)
app.add_middleware(RateLimitMiddleware, limits={
    "/webhook": (120, 60),                    # Meta webhook traffic can burst
    "/api/upload/product-image": (10, 60),    # image uploads
    "/api/admin/login": (10, 60),             # brute-force protection
    "/api/generate-business-ideas": (5, 300), # each run costs an OpenAI call
})


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
    """Authenticate a staff/owner account and issue a session token."""
    stmt = select(AdminUser).where(AdminUser.username == login_in.username, AdminUser.is_active == True)
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if not admin or not verify_password(login_in.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    token = generate_session_token()
    db.add(AdminSession(admin_user_id=admin.id, token=token, expires_at=session_expiry()))
    admin.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    return {"token": token, "username": admin.username, "role": admin.role}


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
async def root():
    return {
        "status": "online",
        "service": "Multi-Tenant WhatsApp AI Chatbot Backend",
        "admin_portal": "/admin",
        "business_ideas": "/business-ideas",
        "docs": "/docs"
    }

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
from prompts.persona_system_prompt import AVAILABLE_TEMPLATES
from tenant_config import DEFAULT_HANDOFF_RULES, build_bot_config, parse_quick_menu


@app.get("/train", tags=["Knowledge Base Trainer"])
async def get_bot_trainer_page():
    """Serves the frontend for the Bot Knowledge Trainer."""
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "train.html")
    if os.path.exists(page_path):
        return FileResponse(page_path)
    return HTMLResponse("<h2>Error: static/train.html not found</h2>", status_code=404)


async def _get_tenant_or_404(db: AsyncSession, tenant_id: int) -> Tenant:
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
    tenant = await _get_tenant_or_404(db, tenant_id)
    return {
        "tenant_id": tenant.id,
        "business_name": tenant.business_name,
        "bot_name": tenant.bot_name or "",
        "bot_role_title": tenant.bot_role_title or "",
        "business_location": tenant.business_location or "",
        "currency": tenant.currency or "",
        "persona_template": tenant.persona_template or "generic",
        "human_handoff_number": tenant.human_handoff_number or "",
        "front_office_number": tenant.front_office_number or "",
        "handoff_rules": tenant.handoff_rules or DEFAULT_HANDOFF_RULES,
        "quick_menu": tenant.quick_menu or "",
        "available_templates": AVAILABLE_TEMPLATES,
    }


@app.patch("/api/tenants/{tenant_id}/bot-profile", tags=["Bot Portal"])
async def update_bot_profile(
    tenant_id: int,
    payload: BotProfileUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Update the bot's identity. No redeploy needed — the next message uses the new values."""
    tenant = await _get_tenant_or_404(db, tenant_id)

    if payload.persona_template and payload.persona_template not in AVAILABLE_TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"persona_template must be one of: {', '.join(AVAILABLE_TEMPLATES)}"
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
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
    tenant = await _get_tenant_or_404(db, tenant_id)
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
    tenant = await _get_tenant_or_404(db, tenant_id)

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


@app.patch("/api/tenants/{tenant_id}/documents/{document_id}", tags=["Bot Portal"])
async def edit_tenant_document(
    tenant_id: int,
    document_id: int,
    payload: TenantDocumentUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    tenant = await _get_tenant_or_404(db, tenant_id)
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
    tenant = await _get_tenant_or_404(db, tenant_id)
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
    tenant = await _get_tenant_or_404(db, tenant_id)
    stmt = select(TenantDocument).where(
        TenantDocument.id == document_id, TenantDocument.tenant_id == tenant.id
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    await remove_document(db, document, tenant)
    return {"message": "Document deleted and knowledge base rebuilt."}


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
    tenant = await _get_tenant_or_404(db, tenant_id)
    knowledge_base = await rebuild_knowledge_base(db, tenant)
    return {"message": "Knowledge base rebuilt.", "knowledge_base": knowledge_base}


async def get_dynamic_kb(tenant: Tenant, db: AsyncSession) -> str:
    """Injects live real-time ERP stock inventory into the AI's knowledge base."""
    products_stmt = select(Product).where(Product.tenant_id == tenant.id, Product.is_active == True)
    products_res = await db.execute(products_stmt)
    live_products = products_res.scalars().all()
    
    inventory_str = "\n\n--- LIVE ERP INVENTORY ---\n"
    if live_products:
        for p in live_products:
            status = f"{p.stock_quantity} in stock" if p.stock_quantity > 0 else "OUT OF STOCK"
            inventory_str += f"- {p.name}: ${p.price} ({status})\n"
        inventory_str += "\n[CRITICAL RULE]: When quoting products or prices, ALWAYS refer to the live ERP inventory above. If a customer asks if something is available, strictly check the exact stock here.\n"
    else:
        inventory_str += "No products currently listed.\n"
    
    return (tenant.knowledge_base or "") + inventory_str


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
    tenant = await _get_tenant_or_404(db, tenant_id)
    documents = await get_tenant_documents(db, tenant.id, active_only=True)
    config = build_bot_config(tenant, documents)
    
    dynamic_kb = await get_dynamic_kb(tenant, db)

    reply, is_handoff = await generate_ai_response(
        system_prompt=tenant.system_prompt,
        knowledge_base=dynamic_kb,
        chat_history=[],
        user_message=payload.message,
        config=config,
    )
    reply, attachments = resolve_reply_documents(reply, payload.message, config)
    return {"reply": reply, "handoff": is_handoff, "attachments": attachments}


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
    tenant = await _get_tenant_or_404(db, tenant_id)
    kb = tenant.knowledge_base or ""
    if not kb.strip() or kb.strip() == "No business documentation has been uploaded yet.":
        raise HTTPException(status_code=400, detail="Upload at least one business document before generating a profile.")

    profile = await generate_bot_profile(kb, business_name_hint=tenant.business_name)

    # Auto-apply the profile to the tenant
    tenant.bot_name = profile.bot_name
    tenant.bot_role_title = profile.bot_role_title
    tenant.persona_template = profile.persona_template
    tenant.system_prompt = profile.system_prompt
    tenant.handoff_rules = profile.handoff_rules
    tenant.quick_menu = profile.quick_menu
    tenant.currency = profile.currency
    await db.commit()
    from bot_learning import clear_tenant_cache
    await clear_tenant_cache(db, tenant.id)     # answers cached under the old persona/rules

    return {
        "message": "Bot profile auto-generated and applied!",
        "profile": {
            "business_name": profile.business_name,
            "business_type": profile.business_type,
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
    await _get_tenant_or_404(db, tenant_id)
    stats = await get_learning_stats(db, tenant_id)
    return stats


@app.get("/api/tenants/{tenant_id}/qa-cache", tags=["Bot Learning"])
async def get_bot_qa_cache(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Returns all cached Q&A pairs the bot has learned for this tenant."""
    await _get_tenant_or_404(db, tenant_id)
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
    tenant = await _get_tenant_or_404(db, tenant_id)
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
    try:
        result = await db.execute(select(Tenant))
        tenants_count = len(result.scalars().all())
        return {
            "status": "healthy",
            "database": "connected",
            "tenants_count": tenants_count
        }
    except Exception as e:
        logger.error(f"Health check database error: {e}")
        return {"status": "unhealthy", "database_error": str(e)}


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
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse incoming webhook JSON: {e}")
        return Response(content="EVENT_RECEIVED", status_code=200)

    background_tasks.add_task(_process_webhook_in_background, body)
    return Response(content="EVENT_RECEIVED", status_code=200)


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

            messages = value.get("messages", [])
            if not messages or not phone_number_id:
                continue

            for message in messages:
                try:
                    msg_type = message.get("type")
                    customer_phone = message.get("from")
                    user_text = ""

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

                    if msg_type == "text":
                        user_text = message.get("text", {}).get("body", "").strip()
                    elif msg_type == "interactive":
                        interactive_obj = message.get("interactive", {})
                        i_type = interactive_obj.get("type")
                        if i_type == "button_reply":
                            user_text = interactive_obj.get("button_reply", {}).get("title", "").strip()
                            btn_id = interactive_obj.get("button_reply", {}).get("id", "")
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
                        audio_id = message.get("audio", {}).get("id")
                        if audio_id:
                            logger.info(f"Audio message received (ID: {audio_id}). Downloading and transcribing...")
                            audio_bytes = await download_whatsapp_media(audio_id, tenant.meta_access_token)
                            if audio_bytes:
                                api_key = settings.OPENAI_API_KEY.strip() if settings.OPENAI_API_KEY else ""
                                transcribed_text = await transcribe_audio(audio_bytes, api_key)
                                if transcribed_text:
                                    logger.info(f"Successfully transcribed audio: '{transcribed_text}'")
                                    user_text = transcribed_text
                                else:
                                    # Fallback if transcription fails or no API key
                                    logger.warning("Transcription failed or no API key available.")
                                    user_text = "I sent a voice note, but it couldn't be transcribed."
                                    # We can send an immediate error message back
                                    await send_whatsapp_message(
                                        phone_number_id=tenant.whatsapp_phone_number_id,
                                        recipient_phone=customer_phone,
                                        message_text="I'm sorry, I couldn't listen to your voice note right now. Could you please type your message? 🙏",
                                        meta_access_token=tenant.meta_access_token
                                    )
                                    continue
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
                                            message_text="Thank you for the image! 📸 We don't have any products listed at the moment. Feel free to ask about our banquet packages! 🏛️",
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
                    if msg_upper.startswith("CONFIRM ORD-") or msg_upper.startswith("REJECT ORD-"):
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

                    # Fetch past chat history for context (up to 10 previous turns)
                    history_stmt = (
                        select(ChatHistory)
                        .where(
                            ChatHistory.tenant_id == tenant.id,
                            ChatHistory.customer_phone_number == customer_phone
                        )
                        .order_by(ChatHistory.created_at.asc())
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
                    
                    dynamic_kb = await get_dynamic_kb(tenant, db)

                    # Generate Multilingual AI Response (gpt-4o-mini / Local Simulator)
                    ai_reply, is_handoff_requested = await generate_ai_response(
                        system_prompt=tenant.system_prompt,
                        knowledge_base=dynamic_kb,
                        chat_history=formatted_history,
                        user_message=user_text,
                        config=bot_config
                    )

                    # Remove every [SEND_DOC: ...] tag from the text and pick this tenant's files to
                    # attach: the AI's choice if it named one of THIS tenant's documents, otherwise
                    # the deterministic keyword resolver.
                    ai_reply, docs_to_send = resolve_reply_documents(ai_reply, user_text, bot_config)

                    # Store user message & AI response in database
                    user_history = ChatHistory(
                        tenant_id=tenant.id,
                        customer_phone_number=customer_phone,
                        role="user",
                        content=user_text
                    )
                    ai_history = ChatHistory(
                        tenant_id=tenant.id,
                        customer_phone_number=customer_phone,
                        role="assistant",
                        content=ai_reply
                    )
                    db.add(user_history)
                    db.add(ai_history)
                    await db.commit()

                    # Self-learning: cache the Q&A pair for future fuzzy matching
                    try:
                        await cache_qa_pair(db, tenant.id, user_text, ai_reply)
                        if is_handoff_requested:
                            await record_signal(db, tenant.id, user_text, is_positive=False)
                        else:
                            await record_signal(db, tenant.id, user_text, is_positive=True)
                    except Exception:
                        pass  # learning is best-effort, never block the reply

                    # Send AI response back to customer via Meta Cloud API
                    await send_whatsapp_message(
                        phone_number_id=tenant.whatsapp_phone_number_id,
                        recipient_phone=customer_phone,
                        message_text=ai_reply,
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

                    if should_send_quick_buttons:
                        interactive_sent = True
                        quick_buttons = [
                            {"id": "btn_prices", "title": "📊 View Prices"},
                            {"id": "btn_menus", "title": "🍽️ View Menus"},
                            {"id": "btn_contact", "title": "📞 Contact Manager"}
                        ]
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
                        generic_triggers = ["option", "options", "view prices", "view menus", "package", "packages", "capacity"]
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
                    is_contact_request = msg_lower in ["contact manager", "📞 contact manager"] or any(
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

                    # Handle Human Handoff if requested
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
async def create_tenant(tenant_in: TenantCreate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Create a new business tenant."""
    # Check if phone number ID already exists
    stmt = select(Tenant).where(Tenant.whatsapp_phone_number_id == tenant_in.whatsapp_phone_number_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tenant with whatsapp_phone_number_id='{tenant_in.whatsapp_phone_number_id}' already exists."
        )

    new_tenant = Tenant(**tenant_in.model_dump())
    db.add(new_tenant)
    await db.commit()
    await db.refresh(new_tenant)
    logger.info(f"Created new tenant: {new_tenant.business_name} (ID: {new_tenant.id})")
    return new_tenant


@app.get("/api/tenants", response_model=List[TenantResponse], tags=["Tenant Admin"])
async def list_tenants(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all registered tenants."""
    stmt = select(Tenant).order_by(Tenant.id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.get("/api/tenants/{tenant_id}", response_model=TenantResponse, tags=["Tenant Admin"])
async def get_tenant(tenant_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Get tenant details by ID."""
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@app.put("/api/tenants/{tenant_id}", response_model=TenantResponse, tags=["Tenant Admin"])
async def update_tenant(tenant_id: int, tenant_update: TenantUpdate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Update tenant prompt, knowledge base, or Meta credentials."""
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
    """Delete a tenant by ID."""
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    await db.delete(tenant)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# ORDERS DASHBOARD PAGE
# ---------------------------------------------------------------------------

@app.get("/business-ideas", tags=["Business Ideas"])
async def get_business_ideas_page():
    """Serves the Business Idea Generator page."""
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "business_ideas.html")
    if os.path.exists(page_path):
        return FileResponse(page_path)
    return HTMLResponse("<h2>Error: static/business_ideas.html not found</h2>", status_code=404)


@app.post("/api/generate-business-ideas", tags=["Business Ideas"])
async def generate_business_ideas_pdf(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    admin: AdminUser = Depends(get_current_admin)
):
    """
    Turn a stock/inventory list (PDF, text file or pasted text) into a business-ideas
    report, returned as a downloadable PDF.
    """
    stock_details = (text or "").strip()

    if file:
        content = await file.read()
        filename = (file.filename or "").lower()
        if filename.endswith(".pdf"):
            try:
                stock_details = f"{stock_details}\n{await extract_text_from_pdf(content)}".strip()
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            try:
                stock_details = f"{stock_details}\n{content.decode('utf-8')}".strip()
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="Uploaded file must be a PDF or a UTF-8 text file.")

    if not stock_details:
        raise HTTPException(status_code=400, detail="Upload a stock list or type your inventory details first.")

    try:
        ideas = await generate_business_ideas(stock_details)
        pdf_bytes = create_pdf_from_text(ideas)
    except Exception as e:
        logger.error(f"Business idea generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate the business ideas report.")

    logger.info(f"Business ideas report generated by {admin.username}.")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="business_ideas_report.pdf"'}
    )


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
    stmt = select(Product).where(Product.tenant_id == tenant_id).order_by(Product.id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.post("/api/tenants/{tenant_id}/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED, tags=["Products"])
async def create_product(tenant_id: int, product_in: ProductCreate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Add a new product for a tenant."""
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    new_product = Product(tenant_id=tenant_id, **product_in.model_dump())
    db.add(new_product)
    await db.commit()
    await db.refresh(new_product)
    return new_product


@app.put("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
async def update_product(product_id: int, product_update: ProductUpdate, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Update a product."""
    stmt = select(Product).where(Product.id == product_id)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    update_data = product_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(product, field, value)

    await db.commit()
    await db.refresh(product)
    return product


@app.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
async def delete_product(product_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Delete a product."""
    stmt = select(Product).where(Product.id == product_id)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    await db.delete(product)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# ORDER MANAGEMENT API ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/api/tenants/{tenant_id}/orders", response_model=List[OrderResponse], tags=["Orders"])
async def list_orders(tenant_id: int, status_filter: Optional[str] = Query(None, alias="status"), db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all orders for a tenant, optionally filtered by status."""
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
    if not order:
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
    revenue_by_day = {row[0]: row[1] for row in revenue_rows}

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
async def get_order_invoice(order_id: int, db: AsyncSession = Depends(get_db)):
    """
    Downloads a PDF invoice/receipt for an order. Left unauthenticated on purpose:
    Meta's WhatsApp Graph API must be able to fetch this URL directly (no auth
    header support) when the bot attaches it as a document, and an order_id alone
    reveals nothing beyond that one order's own receipt.
    """
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order:
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

    await ws_manager.connect(tenant_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(tenant_id, websocket)
