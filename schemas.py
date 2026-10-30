from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any

from industry_brain import INDUSTRY_KEYS, normalise_industry


def _industry_key(value: Optional[str]) -> Optional[str]:
    """Accepts a key or a common name ('hotel', 'Real Estate') and stores the key. Empty stays empty."""
    if value is None or not str(value).strip():
        return value
    key = normalise_industry(value)
    if not key:
        raise ValueError(f"industry must be one of: {', '.join(INDUSTRY_KEYS)}")
    return key

# Tenant Schemas
class TenantBase(BaseModel):
    business_name: str = Field(..., example="Kandy Spice Restaurant")
    whatsapp_phone_number_id: str = Field(..., example="109876543210987")
    meta_access_token: str = Field(..., example="EAAXXXXXX...")
    system_prompt: str = Field(
        default="You are a helpful customer support assistant for Kandy Spice Restaurant. Respond politely in the language the customer uses (English, Sinhala, or Singlish).",
        example="You are a friendly customer service AI for Kandy Spice."
    )
    knowledge_base: str = Field(
        default="Hours: 8 AM - 10 PM. Delivery available in Kandy city. Specials: Rice and Curry (Rs. 800), Kottu (Rs. 950).",
        example="Menu: Rice & Curry Rs 800. Location: Main St, Kandy."
    )
    human_handoff_number: Optional[str] = Field(None, example="+94771234567")
    front_office_number: Optional[str] = Field(None, example="+94342275000")
    # The line of business; picks the bot's persona, handoff rules, buttons and safety limits.
    industry: Optional[str] = Field(None, example="restaurant")

class TenantCreate(TenantBase):
    _check_industry = field_validator("industry")(_industry_key)

class TenantUpdate(BaseModel):
    business_name: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    meta_access_token: Optional[str] = None
    system_prompt: Optional[str] = None
    knowledge_base: Optional[str] = None
    human_handoff_number: Optional[str] = None
    front_office_number: Optional[str] = None
    bank_details: Optional[str] = None
    industry: Optional[str] = None

    _check_industry = field_validator("industry")(_industry_key)

class TenantResponse(TenantBase):
    id: int
    created_at: datetime
    updated_at: datetime
    # The token itself never leaves the server: the dashboard only needs to know one is saved.
    meta_access_token: str = Field(default="", exclude=True)
    has_access_token: bool = False

    class Config:
        from_attributes = True

    @model_validator(mode="after")
    def _hide_token(self):
        self.has_access_token = bool(self.meta_access_token) and "placeholder" not in self.meta_access_token
        return self

# Helper schemas for WhatsApp Webhook parsing
class WhatsAppProfile(BaseModel):
    name: Optional[str] = None

class WhatsAppContact(BaseModel):
    wa_id: str
    profile: Optional[WhatsAppProfile] = None

class WhatsAppTextMessage(BaseModel):
    body: str

class WhatsAppMessage(BaseModel):
    from_number: str = Field(..., alias="from")
    id: str
    timestamp: str
    type: str
    text: Optional[WhatsAppTextMessage] = None

    class Config:
        populate_by_name = True

class WhatsAppValueMetadata(BaseModel):
    display_phone_number: Optional[str] = None
    phone_number_id: str

class WhatsAppValue(BaseModel):
    messaging_product: str
    metadata: WhatsAppValueMetadata
    contacts: Optional[List[WhatsAppContact]] = None
    messages: Optional[List[WhatsAppMessage]] = None

class WhatsAppChange(BaseModel):
    value: WhatsAppValue
    field: str

class WhatsAppEntry(BaseModel):
    id: str
    changes: List[WhatsAppChange]

class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: List[WhatsAppEntry]


# ---------------------------------------------------------------------------
# PRODUCT SCHEMAS
# ---------------------------------------------------------------------------

class ProductCreate(BaseModel):
    name: str = Field(..., example="Wooden Chair")
    description: Optional[str] = Field(None, example="Handcrafted teak wood dining chair")
    price: float = Field(..., example=12500.00)
    stock_quantity: int = Field(default=0, example=100)
    low_stock_threshold: int = Field(default=5, example=5)
    image_url: Optional[str] = Field(None, example="https://example.com/chair.jpg")
    is_active: bool = Field(default=True)

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock_quantity: Optional[int] = None
    low_stock_threshold: Optional[int] = None
    image_url: Optional[str] = None
    is_active: Optional[bool] = None

class ProductResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    description: Optional[str] = None
    price: float
    stock_quantity: int
    low_stock_threshold: int
    image_url: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# ORDER SCHEMAS
# ---------------------------------------------------------------------------

class OrderItemResponse(BaseModel):
    id: int
    product_id: Optional[int] = None
    product_name: str
    quantity: int
    unit_price: float
    subtotal: float

    class Config:
        from_attributes = True

class OrderResponse(BaseModel):
    id: int
    tenant_id: int
    order_number: str
    customer_phone: str
    customer_name: Optional[str] = None
    product_id: Optional[int] = None
    product_name: str
    quantity: int
    total_amount: float
    status: str
    payment_receipt_url: Optional[str] = None
    payment_receipt_analysis: Optional[str] = None
    payment_amount_detected: Optional[float] = None
    payment_ref_number: Optional[str] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemResponse] = []

    class Config:
        from_attributes = True

class OrderStatusUpdate(BaseModel):
    status: str = Field(..., example="confirmed")
    confirmed_by: Optional[str] = Field(None, example="Manager Ali")


# ---------------------------------------------------------------------------
# BROADCAST & ANALYTICS SCHEMAS
# ---------------------------------------------------------------------------

class BroadcastRequest(BaseModel):
    message: str = Field(..., example="🎉 New Silver package prices just dropped! Ask us for details.")


# ---------------------------------------------------------------------------
# ADMIN AUTH SCHEMAS
# ---------------------------------------------------------------------------

class AdminLoginRequest(BaseModel):
    username: str = Field(..., example="admin")
    password: str = Field(..., example="admin123")

class AdminUserCreate(BaseModel):
    username: str = Field(..., example="jane")
    password: str = Field(..., example="a-strong-password")
    role: str = Field(default="staff", example="staff")

class AdminUserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

class AdminUserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    tenant_id: Optional[int] = None      # set for a business owner's ('client') account
    full_name: Optional[str] = None
    created_at: datetime
    last_login_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SuperAdminAccountCreate(BaseModel):
    username: str = Field(..., example="manager@example.com")
    password: str = Field(..., example="SecurePassword123!")
    role: str = Field(default="staff", example="staff")  # 'owner' | 'staff' | 'client'
    full_name: Optional[str] = None
    tenant_id: Optional[int] = None

class SuperAdminAccountUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    tenant_id: Optional[int] = None
    password: Optional[str] = None

class AdminUserAccountDetail(AdminUserResponse):
    tenant_name: Optional[str] = None

class ForgotPasswordSubmit(BaseModel):
    username: str = Field(..., example="user@example.com")

class PasswordResetResolve(BaseModel):
    new_password: Optional[str] = None
    notes: Optional[str] = None

class PasswordResetReject(BaseModel):
    reason: Optional[str] = None

class PasswordResetResponse(BaseModel):
    id: int
    username: str
    status: str
    requested_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    temp_password: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True




# ---------------------------------------------------------------------------
# BOT PORTAL SCHEMAS — per-tenant identity and document library
# ---------------------------------------------------------------------------

class BotProfileUpdate(BaseModel):
    """Every field here used to be a constant in ai_service.py, shared by all tenants."""
    business_name: Optional[str] = Field(None, example="Wasala Nature Resort")
    bot_name: Optional[str] = Field(None, example="Nethmi")
    bot_role_title: Optional[str] = Field(None, example="Senior Banquet & Events Consultant")
    business_location: Optional[str] = Field(None, example="Bentota, Sri Lanka")
    currency: Optional[str] = Field(None, example="LKR")
    industry: Optional[str] = Field(None, example="hospitality")
    persona_template: Optional[str] = Field(None, example="hospitality")
    human_handoff_number: Optional[str] = Field(None, example="+94779998877")
    front_office_number: Optional[str] = Field(None, example="+94342275000")
    handoff_rules: Optional[str] = Field(None, example="Discounts and negotiation\nComplaints and refunds")
    quick_menu: Optional[str] = Field(None, example="Grand Ballroom | 150-650 guests\nGarden Lawn | Outdoor lake view")
    quick_buttons: Optional[str] = Field(None, example="📊 View Prices\n🍽️ View Menus")
    # Where the approved templates live (WhatsApp Manager -> Account tools). Normally picked up by itself.
    wa_business_account_id: Optional[str] = Field(None, max_length=64, pattern=r"^\d*$", example="123456789012345")

    _check_industry = field_validator("industry")(_industry_key)


class TemplateSend(BaseModel):
    """Send an approved template: its name and language, and what goes in each blank."""
    name: str = Field(..., min_length=1, max_length=512)
    language: str = Field(..., min_length=2, max_length=15, example="en_US")
    body: Dict[str, str] = Field(default_factory=dict, example={"1": "Nimal", "2": "ORD-12"})
    header: Dict[str, str] = Field(default_factory=dict)
    client_msg_id: Optional[str] = Field(None, max_length=64)


class WebsiteTrain(BaseModel):
    """Train the bot from the business's own website."""
    url: str = Field(..., min_length=4, max_length=500, example="https://www.laveraclo.lk")


class TenantDocumentUpdate(BaseModel):
    title: Optional[str] = None
    trigger_keywords: Optional[str] = Field(None, example="price, ganan, mila, rate, cost")
    caption: Optional[str] = None
    summary: Optional[str] = None
    doc_type: Optional[str] = Field(None, example="both")
    is_active: Optional[bool] = None


class SignupRequest(BaseModel):
    """A business owner creating their own account and business from the portal."""
    full_name: Optional[str] = Field(None, example="Nimal Perera")
    email: str = Field(..., example="nimal@citycare.lk")
    password: str = Field(..., example="a-long-password")
    business_name: str = Field(..., example="City Care Medical Centre")


class WhatsAppConnectRequest(BaseModel):
    phone_number_id: str = Field(..., example="109876543210987")
    access_token: str = Field(..., example="EAA...")


# --- Live inbox ---------------------------------------------------------------

class InboxSendRequest(BaseModel):
    """A reply typed by a person in the inbox."""
    text: str = Field(..., example="Hi Nimal, yes - we can deliver on Saturday.")
    # The sending device's own id for this message. Sending again with the same id (after a
    # dropped connection) returns the first result instead of messaging the customer twice.
    client_msg_id: Optional[str] = Field(None, max_length=64, example="a1b2c3d4-...")


class DeviceMessageRecord(BaseModel):
    """A message a phone sent by itself (with its own WhatsApp token), saved so every device sees it."""
    text: str = Field("", max_length=8000)
    type: str = Field("text", max_length=20, example="image")
    client_msg_id: str = Field(..., min_length=1, max_length=64)
    wa_message_id: Optional[str] = Field(None, max_length=128)
    status: str = Field("SENT", example="SENT")
    error: Optional[str] = Field(None, max_length=2000)
    sent_at: Optional[int] = Field(None, description="epoch milliseconds on the device")


class ContactUpdate(BaseModel):
    saved_name: Optional[str] = Field(None, max_length=150, example="Nimal (wholesale)")
    notes: Optional[str] = Field(None, max_length=5000)


class HistorySettings(BaseModel):
    history_days: int = Field(21, example=21)


class BookingUpdate(BaseModel):
    """Answer a booking request. For 'confirmed' and 'declined' the customer is messaged."""
    status: str = Field(..., example="confirmed")
    # Your own words to the customer instead of the standard message.
    message: Optional[str] = Field(None, max_length=1000)


class ShareContact(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone: str = Field(..., min_length=5, max_length=30, example="+94771234567")


class InboxShare(BaseModel):
    """Send something other than text or a file: a location, contact cards, the catalogue, products."""
    type: str = Field(..., example="location")          # location | contacts | catalog | products
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    name: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = Field(None, max_length=300)
    contacts: Optional[List[ShareContact]] = None
    product_ids: Optional[List[int]] = Field(None, max_length=30)
    text: Optional[str] = Field(None, max_length=1000)
    client_msg_id: Optional[str] = Field(None, max_length=64)


class CatalogConnect(BaseModel):
    """The catalogue ID from Meta Commerce Manager (Catalogue -> Settings)."""
    catalog_id: str = Field(..., min_length=5, max_length=40, example="1234567890123456")


class BrowserCallOffer(BaseModel):
    """The website's WebRTC offer (microphone + speaker), with its ICE candidates already gathered."""
    sdp: str = Field(..., min_length=20, max_length=20000)


class TeamDial(BaseModel):
    """A person on the team calls a customer from the website."""
    phone: str = Field(..., example="94771234567")
    sdp: str = Field(..., min_length=20, max_length=20000)


class CallStart(BaseModel):
    """Call a customer on WhatsApp. The AI opens with `purpose`."""
    phone: str = Field(..., example="94771234567")
    purpose: str = Field(..., min_length=3, max_length=500, example="Your order #12 is ready to collect.")


class ChatBotUpdate(BaseModel):
    """Switch the bot on or off for one customer's chat."""
    bot_enabled: bool


class BusinessBotUpdate(BaseModel):
    """The master switch: off = no automatic replies to anyone."""
    enabled: bool


class ManualOrderLine(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=1, le=10000)


class ManualOrderCreate(BaseModel):
    """An order entered by hand (a phone call, a walk-in), not through the WhatsApp cart."""
    customer_phone: Optional[str] = Field(None, example="94771234567")
    customer_name: Optional[str] = None
    items: List[ManualOrderLine]
    status: str = Field("pending_payment", example="pending_payment")


class GapAnswerRequest(BaseModel):
    """The owner's answer to a question the bot couldn't answer. `question` lets them tidy its wording."""
    answer: str = Field(..., example="Yes, there is free parking for about 80 cars next to the hall.")
    question: Optional[str] = None


class TeachRequest(BaseModel):
    question: str = Field(..., example="Do you have parking?")
    answer: str = Field(..., example="Yes, free parking for about 80 cars next to the hall.")


class BotPreviewRequest(BaseModel):
    message: str = Field(..., example="wedding package eke ganan kohomada?")
