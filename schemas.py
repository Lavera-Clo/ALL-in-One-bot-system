from datetime import datetime
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Dict, Any

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

class TenantCreate(TenantBase):
    pass

class TenantUpdate(BaseModel):
    business_name: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    meta_access_token: Optional[str] = None
    system_prompt: Optional[str] = None
    knowledge_base: Optional[str] = None
    human_handoff_number: Optional[str] = None
    front_office_number: Optional[str] = None
    bank_details: Optional[str] = None

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
    created_at: datetime
    last_login_at: Optional[datetime] = None

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
    persona_template: Optional[str] = Field(None, example="hospitality")
    human_handoff_number: Optional[str] = Field(None, example="+94779998877")
    front_office_number: Optional[str] = Field(None, example="+94342275000")
    handoff_rules: Optional[str] = Field(None, example="Discounts and negotiation\nComplaints and refunds")
    quick_menu: Optional[str] = Field(None, example="Grand Ballroom | 150-650 guests\nGarden Lawn | Outdoor lake view")


class TenantDocumentUpdate(BaseModel):
    title: Optional[str] = None
    trigger_keywords: Optional[str] = Field(None, example="price, ganan, mila, rate, cost")
    caption: Optional[str] = None
    summary: Optional[str] = None
    doc_type: Optional[str] = Field(None, example="both")
    is_active: Optional[bool] = None


class BotPreviewRequest(BaseModel):
    message: str = Field(..., example="wedding package eke ganan kohomada?")
