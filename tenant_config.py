"""
Per-tenant bot configuration.

THE PROBLEM THIS SOLVES
-----------------------
ai_service.py used to hardcode one client's details — PDF URLs, the manager's
WhatsApp number, the front desk hotline, the business name — directly into the
prompt that EVERY tenant's bot uses. Client #2's customers would have received
client #1's price list and phone number.

The fix: nothing business-specific lives in code any more. Every value the bot
speaks is read off the Tenant row (and its documents) into a BotConfig, which is
passed down through the reply pipeline. Adding a new client is now a database
insert plus a few PDF uploads — no code change, no redeploy.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote


def _digits_only(number: Optional[str]) -> str:
    """'+94 77 999 8877' -> '94779998877', the form wa.me links need."""
    return "".join(ch for ch in (number or "") if ch.isdigit())


@dataclass
class SendableDoc:
    """A document the bot is allowed to send to a customer on WhatsApp."""
    id: int
    title: str
    url: str
    filename: str
    caption: str
    keywords: List[str] = field(default_factory=list)

    def matches(self, message: str) -> bool:
        return any(kw and kw in message for kw in self.keywords)


@dataclass
class BotConfig:
    """Everything the reply pipeline needs to know about *this* business."""
    tenant_id: int
    business_name: str
    bot_name: str = "Assistant"
    bot_role_title: str = "Customer Care Assistant"
    business_location: str = ""
    currency: str = ""
    persona_template: str = "generic"
    manager_whatsapp: Optional[str] = None
    front_office_number: Optional[str] = None
    handoff_rules: str = ""
    documents: List[SendableDoc] = field(default_factory=list)

    # ---------- derived helpers, used when building prompts and replies ----------

    @property
    def has_manager(self) -> bool:
        return bool(_digits_only(self.manager_whatsapp))

    @property
    def has_front_office(self) -> bool:
        return bool(self.front_office_number)

    def manager_link(self, prefill: str = "") -> str:
        """wa.me link to the human manager, or '' when this client set no manager number."""
        digits = _digits_only(self.manager_whatsapp)
        if not digits:
            return ""
        base = f"https://wa.me/{digits}"
        return f"{base}?text={quote(prefill)}" if prefill else base

    def contact_lines(self) -> str:
        """The contact block, built only from numbers this client actually provided."""
        lines = []
        if self.has_front_office:
            lines.append(f"• Hotline: {self.front_office_number}")
        if self.has_manager:
            lines.append(f"• WhatsApp Manager: {self.manager_whatsapp}")
        return "\n".join(lines)

    def display_name(self) -> str:
        return f"{self.business_name}, {self.business_location}" if self.business_location else self.business_name

    def handoff_rule_lines(self) -> List[str]:
        return [ln.strip("-• ").strip() for ln in (self.handoff_rules or "").splitlines() if ln.strip()]

    def docs_matching(self, message: str) -> List[SendableDoc]:
        msg = (message or "").lower().strip()
        return [d for d in self.documents if d.matches(msg)]


# Sensible per-industry defaults, applied only when a client leaves a field blank.
DEFAULT_HANDOFF_RULES = """Discounts, special prices, negotiation or bargaining
Custom or special requests beyond the standard packages
Bookings, date holds and reservation confirmations
Complaints, refunds and cancellations
Any explicit request to speak to a manager, human or staff member"""


def build_bot_config(tenant, documents=None) -> BotConfig:
    """
    Build a BotConfig from a Tenant row and its TenantDocument rows.

    `documents` may be omitted when the caller has not loaded them (for example the
    trainer preview) — the bot then simply has no files to send, rather than falling
    back to somebody else's PDFs.
    """
    sendable: List[SendableDoc] = []
    for doc in documents or []:
        if not doc.is_active or doc.doc_type not in ("sendable", "both"):
            continue
        if not doc.file_url:
            continue  # pasted-text notes have no file to send
        sendable.append(
            SendableDoc(
                id=doc.id,
                title=doc.title,
                url=doc.file_url,
                filename=doc.original_filename or f"{doc.title}.pdf",
                caption=doc.caption or f"{tenant.business_name} - {doc.title}",
                keywords=doc.keyword_list,
            )
        )

    return BotConfig(
        tenant_id=tenant.id,
        business_name=tenant.business_name,
        bot_name=tenant.bot_name or "Assistant",
        bot_role_title=tenant.bot_role_title or "Customer Care Assistant",
        business_location=tenant.business_location or "",
        currency=tenant.currency or "",
        persona_template=tenant.persona_template or "generic",
        manager_whatsapp=tenant.human_handoff_number,
        front_office_number=tenant.front_office_number,
        handoff_rules=tenant.handoff_rules or DEFAULT_HANDOFF_RULES,
        documents=sendable,
    )


def parse_quick_menu(quick_menu: Optional[str]) -> List[dict]:
    """
    Turn a tenant's quick_menu setting into WhatsApp list rows.

    Format is one item per line, "Title | Description" (description optional):

        Grand Ballroom | 150-650 guests, air conditioned
        Garden Lawn    | Outdoor lake view

    WhatsApp caps a list at 10 rows, titles at 24 characters and descriptions at 72,
    so anything longer is trimmed rather than rejected.
    """
    rows: List[dict] = []
    for index, line in enumerate((quick_menu or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        title, _, description = line.partition("|")
        title = title.strip()[:24]
        if not title:
            continue
        rows.append({
            "id": f"quick_{index}",
            "title": title,
            "description": description.strip()[:72],
        })
        if len(rows) == 10:
            break
    return rows
