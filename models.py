import time
from datetime import datetime, timezone
from sqlalchemy import BigInteger, String, Text, DateTime, ForeignKey, Integer, Float, Boolean, UniqueConstraint, event
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
    # Which line of business this is: the key of an IndustryPack in industry_brain.py
    # ('hospitality', 'restaurant', 'clinic', ...). It picks the bot's persona, default handoff
    # rules, greeting buttons and safety limits. Every other field here can still override it.
    industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Who chose the industry: 'owner' (never changed automatically) or 'auto' / empty (the
    # system may refine it as documents are added — see understanding_service.py).
    industry_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # JSON: what the system worked out about this business from its documents, how sure it is,
    # the evidence, and which profile fields it filled in. Shown on the portal's setup card.
    understanding: Mapped[str | None] = mapped_column(Text, nullable=True)
    persona_template: Mapped[str | None] = mapped_column(String(50), nullable=True)  # optional override; empty = the industry's persona
    handoff_rules: Mapped[str | None] = mapped_column(Text, nullable=True)  # one rule per line, injected into the prompt
    # WhatsApp quick-pick list, one item per line as "Title | Description".
    # Empty means this client simply gets no list menu (instead of another client's halls).
    quick_menu: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The two reply buttons sent with a greeting, one title per line. Empty = the industry's defaults.
    quick_buttons: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The master switch for automatic replies. Off = every incoming message is saved to the inbox
    # and nothing is sent automatically; a person replies. Each chat also has its own switch
    # (Conversation.bot_enabled); the bot answers only when both are on.
    bot_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # How many days of messages the shared database keeps (sync_service.py cleans up daily).
    # Contacts and daily message counts are kept regardless.
    history_days: Mapped[int] = mapped_column(Integer, default=21, nullable=False)
    # WhatsApp catalogue (catalog_service.py): the Meta Commerce catalogue linked to this number.
    # Products are synced to it, so customers can browse and send a cart from WhatsApp itself.
    wa_catalog_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catalog_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    chat_histories: Mapped[list["ChatHistory"]] = relationship("ChatHistory", back_populates="tenant", cascade="all, delete-orphan")
    products: Mapped[list["Product"]] = relationship("Product", back_populates="tenant", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="tenant", cascade="all, delete-orphan")
    documents: Mapped[list["TenantDocument"]] = relationship("TenantDocument", back_populates="tenant", cascade="all, delete-orphan")
    knowledge_gaps: Mapped[list["KnowledgeGap"]] = relationship("KnowledgeGap", cascade="all, delete-orphan")
    test_runs: Mapped[list["BotTestRun"]] = relationship("BotTestRun", cascade="all, delete-orphan")

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
    # 'playbook'  = how the owner wants conversations to go (goals, style, objections, example
    #               chats). Never part of the knowledge base: its numbers are not facts.
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

    # For the live inbox (inbox_service.py). `role` stays what the AI reads ('user' / 'assistant');
    # `sender` says who actually wrote it: 'customer', 'bot', or 'agent' (a person replying from
    # the inbox). Rows from before the inbox have no sender: role tells it then.
    sender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # text, image, audio, document, buttons...
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)         # outgoing: SENT | FAILED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Shared database (sync_service.py): the id a phone or browser gave a message it sent, so an
    # upload repeated after a dropped connection is saved once; and the change stamp devices
    # sync by ("everything changed after X").
    client_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    changed_us: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # Photos, videos, voice notes, documents and stickers, sent or received (media_service.py):
    # a copy kept on this server, so the inbox can show it after WhatsApp's own link expires.
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    media_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # WhatsApp's id for a received file, kept until the copy is downloaded: if the download failed
    # (an expired access token, Meta down) it is tried again later - Meta keeps files for 30 days.
    media_wa_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="chat_histories")

    def __repr__(self) -> str:
        return f"<ChatHistory(tenant_id={self.tenant_id}, customer='{self.customer_phone_number}', role='{self.role}')>"


class BotQACache(Base):
    __tablename__ = "bot_qa_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    positive_signals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    negative_signals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    tenant: Mapped["Tenant"] = relationship("Tenant")

    def __repr__(self) -> str:
        return f"<BotQACache(id={self.id}, tenant_id={self.tenant_id}, question='{self.question[:20]}')>"


class Conversation(Base):
    """
    One customer's chat with one business, as the live inbox shows it.

    The messages themselves are ChatHistory rows; this holds what belongs to the chat as a
    whole: the customer's WhatsApp name, whether the bot may answer them, and whether the bot
    asked for a person.
    """
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_phone", name="uq_conversation_tenant_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)   # off = a person handles this chat
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # the bot handed this customer over
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)   # first contact
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    # The contact, kept after old messages are cleaned up: the name the business gave them
    # (shown before their WhatsApp name), notes, and how many messages they've exchanged.
    saved_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_us: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # What the bot remembers about this conversation (conversation_memory.py): the customer's
    # goal, how far along they are, details they already gave, what is still open. JSON text,
    # refreshed in the background; memory_upto_id = the last message it has read.
    memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_upto_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Learning from the team's replies (learning_service.learn_from_chat): the last message looked at.
    learned_upto_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def display_name(self) -> str:
        return self.saved_name or self.customer_name or ""

    def __repr__(self) -> str:
        return f"<Conversation(tenant_id={self.tenant_id}, phone='{self.customer_phone}', bot={self.bot_enabled})>"


class KnowledgeGap(Base):
    """
    A question customers asked that the bot could not answer from the knowledge base.

    This is how the bot learns from real chats WITHOUT learning anything wrong: the gap is
    shown to the owner, the owner types the right answer once, and that answer joins the
    knowledge base (see document_service.rebuild_knowledge_base). Nothing the bot said itself
    is ever reused as a fact. Similar questions are counted on one row, so the owner sees
    what customers ask most.
    """
    __tablename__ = "knowledge_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    bot_reply: Mapped[str | None] = mapped_column(Text, nullable=True)      # what the bot said instead
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    times_asked: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_customer: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="chat", nullable=False)   # 'chat' | 'test' | 'owner'
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)  # open | answered | dismissed
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)         # the owner's answer, once given
    # What a person on the team told a caller (call_service.learn_from_call). Only a suggestion:
    # it is used once the owner checks it and saves it as the answer.
    suggested_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self) -> str:
        return f"<KnowledgeGap(id={self.id}, tenant_id={self.tenant_id}, status='{self.status}', asked={self.times_asked})>"


class BotTestRun(Base):
    """One automatic test of a tenant's bot (self_test_service.py): questions, replies and checks."""
    __tablename__ = "bot_test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)   # running | done | failed
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)        # human-readable progress / error
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # questions finished
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 0-100, once done
    results: Mapped[str | None] = mapped_column(Text, nullable=True)     # JSON list, one entry per question
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<BotTestRun(id={self.id}, tenant_id={self.tenant_id}, status='{self.status}', score={self.score})>"


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
    """
    An account that can sign in to the portal.

    'owner' / 'staff' run the PLATFORM and can see every business. 'client' is a business
    owner who signed up themselves: tenant_id is their business, and every page and API call
    is limited to it (see main.ensure_tenant_access).
    """
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)  # an email for clients
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="staff", nullable=False)  # 'owner' | 'staff' | 'client'
    tenant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
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


class PasswordResetRequest(Base):
    """
    A request submitted by a user who forgot their password.
    Super admin can review, approve (reset password with temporary password) or reject.
    """
    __tablename__ = "password_reset_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # 'pending' | 'resolved' | 'rejected'
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    temp_password: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PasswordResetRequest(id={self.id}, username='{self.username}', status='{self.status}')>"


class DailyMessageStat(Base):
    """
    Messages per business per day, kept for good. Old messages are deleted after the business's
    history_days; these counts are what's left of them, so activity charts still go back further.
    """
    __tablename__ = "daily_message_stats"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_daily_stat_tenant_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)          # "YYYY-MM-DD" (UTC)
    incoming: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outgoing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# --- Change stamps for syncing --------------------------------------------------------
# Every insert or update of a message or a contact gets a fresh, ever-increasing stamp
# (microseconds). A phone syncs by asking for everything stamped after the last one it saw.
_last_stamp = 0


def next_change_stamp() -> int:
    global _last_stamp
    _last_stamp = max(time.time_ns() // 1000, _last_stamp + 1)
    return _last_stamp


def _stamp(mapper, connection, target) -> None:
    target.changed_us = next_change_stamp()


for _model in (ChatHistory, Conversation):
    event.listen(_model, "before_insert", _stamp)
    event.listen(_model, "before_update", _stamp)


class Booking(Base):
    """
    A booking or appointment request a customer made in chat (booking_service.py). The bot collects
    what's needed and records it here as 'pending'; a person confirms or declines it, and the customer
    is told. The bot never confirms a booking itself.
    """
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    service: Mapped[str | None] = mapped_column(String(255), nullable=True)      # what: a table, a dental check-up, a hall
    when_text: Mapped[str | None] = mapped_column(String(255), nullable=True)    # when, in the customer's words
    party_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)  # pending|confirmed|declined|cancelled
    handled_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self) -> str:
        return f"<Booking(id={self.id}, tenant={self.tenant_id}, {self.customer_phone}, {self.status})>"


class Call(Base):
    """
    A WhatsApp voice call (call_service.py), incoming or outgoing. The AI answers and talks; for
    complaints, or when the caller asks for a person, it rings the manager and joins the two calls.
    """
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    wa_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)          # incoming | outgoing
    # awaiting_permission | allowed | ringing | in_progress | transferring | with_team | completed | missed | rejected | failed
    status: Mapped[str] = mapped_column(String(24), default="ringing", nullable=False, index=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)             # outgoing: why we are calling
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)          # JSON [{"who": "customer|bot|team", "text": ...}]
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    transferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    answered_by: Mapped[str | None] = mapped_column(String(150), nullable=True)   # the team member who talked
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)        # OGG, caller left / business right
    recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # after CALL_RECORDING_DAYS
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def __repr__(self) -> str:
        return f"<Call(id={self.id}, tenant={self.tenant_id}, {self.direction} {self.customer_phone}, {self.status})>"
