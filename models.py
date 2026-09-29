from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base

def utc_now():
    return datetime.now(timezone.utc)

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    whatsapp_phone_number_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    meta_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_base: Mapped[str] = mapped_column(Text, nullable=False)
    human_handoff_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    front_office_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bank_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Bot identity & behaviour, per client. Everything here used to be hardcoded in
    # ai_service.py / prompts, which meant every tenant's bot spoke as the same business. ---
    bot_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bot_role_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    business_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    persona_template: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 'hospitality' | 'retail' | 'generic'
    handoff_rules: Mapped[str | None] = mapped_column(Text, nullable=True)  # one rule per line, injected into the prompt
    # WhatsApp quick-pick list, one item per line as "Title | Description".
    # Empty means this client simply gets no list menu (instead of another client's halls).
    quick_menu: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    chat_histories: Mapped[list["ChatHistory"]] = relationship("ChatHistory", back_populates="tenant", cascade="all, delete-orphan")
    products: Mapped[list["Product"]] = relationship("Product", back_populates="tenant", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="tenant", cascade="all, delete-orphan")
    documents: Mapped[list["TenantDocument"]] = relationship("TenantDocument", back_populates="tenant", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Tenant(id={self.id}, business_name='{self.business_name}', phone_id='{self.whatsapp_phone_number_id}')>"


class TenantDocument(Base):
    """
    One piece of training material a client uploaded (a PDF, or a pasted note).

    Uploads are ADDITIVE: each upload becomes its own row, and the tenant's
    knowledge_base column is rebuilt by concatenating every active row's summary.
    Deleting or deactivating one document rebuilds the knowledge base without it,
    so a client can never silently lose last week's menu by uploading this week's policy.
    """
    __tablename__ = "tenant_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="pdf", nullable=False)  # 'pdf' | 'text'
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stored_path: Mapped[str | None] = mapped_column(Text, nullable=True)   # on-disk path, relative to project root
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)      # public URL, used when sending to customers

    # 'knowledge' = bot reads it only. 'sendable' = bot sends the file only.
    # 'both'      = bot answers from it AND can send the original PDF.
    doc_type: Mapped[str] = mapped_column(String(20), default="both", nullable=False)

    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)   # kept so the KB can be regenerated later
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)    # structured chunk injected into the prompt
    trigger_keywords: Mapped[str] = mapped_column(Text, default="", nullable=False)  # comma-separated, matched on customer messages
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)          # caption shown when the file is sent on WhatsApp

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Multi-model training runs in the background (training_pipeline.py), so the portal
    # polls these. A document keeps its previous summary until a retrain succeeds.
    status: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)  # queued | processing | ready | failed
    training_stage: Mapped[str | None] = mapped_column(Text, nullable=True)   # human-readable progress / error
    training_progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0-100
    training_report: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: models used, numbers verified...
    extraction_method: Mapped[str | None] = mapped_column(String(30), nullable=True)  # vision | vision+text | text | pasted
    public_path: Mapped[str | None] = mapped_column(Text, nullable=True)      # public copy served to WhatsApp, if sendable

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="documents")

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in (self.trigger_keywords or "").split(",") if k.strip()]

    def __repr__(self) -> str:
        return f"<TenantDocument(id={self.id}, tenant_id={self.tenant_id}, title='{self.title}', type='{self.doc_type}')>"


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'user', 'assistant', 'system'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="chat_histories")

    def __repr__(self) -> str:
        return f"<ChatHistory(tenant_id={self.tenant_id}, customer='{self.customer_phone_number}', role='{self.role}')>"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="products")
    cart_items: Mapped[list["CartItem"]] = relationship("CartItem", back_populates="product", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, name='{self.name}', price={self.price}, stock={self.stock_quantity})>"


class CartItem(Base):
    """An item sitting in a customer's active WhatsApp cart, before checkout."""
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    product: Mapped["Product"] = relationship("Product", back_populates="cart_items")

    def __repr__(self) -> str:
        return f"<CartItem(customer='{self.customer_phone}', product_id={self.product_id}, qty={self.quantity})>"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    order_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    # Status flow: pending_payment → payment_uploaded → confirmed → shipped → delivered → cancelled
    status: Mapped[str] = mapped_column(String(30), default="pending_payment", nullable=False, index=True)
    payment_receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_receipt_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_amount_detected: Mapped[float | None] = mapped_column(Float, nullable=True)
    payment_ref_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 'bank_transfer' | 'stripe'
    payment_link_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Order(id={self.id}, order_number='{self.order_number}', status='{self.status}', total={self.total_amount})>"


class OrderItem(Base):
    """A single product line within an order. An order can hold multiple items (cart checkout)."""
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")

    def __repr__(self) -> str:
        return f"<OrderItem(order_id={self.order_id}, product='{self.product_name}', qty={self.quantity})>"


class AdminUser(Base):
    """A staff/owner account that can log into the admin dashboards."""
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="staff", nullable=False)  # 'owner' | 'staff'
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["AdminSession"]] = relationship("AdminSession", back_populates="admin_user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<AdminUser(id={self.id}, username='{self.username}', role='{self.role}')>"


class AdminSession(Base):
    """A logged-in session token for an AdminUser."""
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    admin_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    admin_user: Mapped["AdminUser"] = relationship("AdminUser", back_populates="sessions")

    def __repr__(self) -> str:
        return f"<AdminSession(admin_user_id={self.admin_user_id}, expires_at={self.expires_at})>"

