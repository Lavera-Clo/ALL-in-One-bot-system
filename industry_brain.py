"""
The industry layer of the bot's brain.

THE PROBLEM THIS SOLVES
-----------------------
tenant_config.py moved every business DETAIL (name, phone numbers, documents) onto the
tenant's own row. The bot's INSTINCTS, though, were still those of the first client, a
banquet hall: every tenant greeted customers with "View Prices / View Menus" buttons, a
dentist's returning patient was welcomed back to "your event plans", and a photo sent to a
shoe shop was answered with "ask about our banquet packages".

An IndustryPack holds the instincts for one line of business — what customers usually come
for, what staff need to know before they can help, what must never be said, and which
shortcuts to offer. The tenant row stores WHICH pack applies (tenants.industry); the pack
supplies defaults, and anything a client typed into their own profile still wins.

Adding an industry is one entry in _PACKS, plus a persona of the same name in
prompts/persona_system_prompt.py if it should sound different. No other file changes.

This module is pure data and string handling: no database, no AI calls, no other project
imports, so database.py and schemas.py can both use it.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Sequence, Tuple

CONTACT_BUTTON_ID = "btn_contact"
MAX_BUTTON_TITLE = 20       # Meta's limit for a reply-button title

# Rules every business shares; each pack puts its own between the first and the last two.
_DISCOUNTS = "Discounts, special prices, negotiation or bargaining"
_COMPLAINTS = "Complaints, refunds and cancellations"
_HUMAN = "Any explicit request to speak to a manager, human or staff member"


def _rules(*specific: str) -> Tuple[str, ...]:
    return (_DISCOUNTS, *specific, _COMPLAINTS, _HUMAN)


# Topics any customer of any business brings up. A pack's own topics are checked first.
_COMMON_TOPICS = {
    "price": "prices", "cost": "prices", "rate": "prices", "fee": "prices",
    "ganan": "prices", "mila": "prices", "kiyada": "prices", "මිල": "prices", "ගණන්": "prices",
    "open": "opening hours", "hours": "opening hours", "close": "opening hours",
    "location": "how to find us", "address": "how to find us", "where": "how to find us",
    "deliver": "delivery & shipping", "courier": "delivery & shipping", "shipping": "delivery & shipping",
    "cod": "cash on delivery", "cash on delivery": "cash on delivery",
    "pay": "payment options", "bank": "payment & bank transfer", "account": "payment & bank transfer",
    "size": "sizing & measurements", "fit": "sizing & fit", "measurement": "sizing & measurements",
    "exchange": "returns & exchanges", "return": "returns & exchanges", "refund": "returns & exchanges",
    "date": "today's schedule & calendar", "today": "today's schedule & calendar",
}


@dataclass(frozen=True)
class IndustryPack:
    key: str
    label: str                        # shown in the portal's industry dropdown
    persona: str                      # persona template in prompts/persona_system_prompt.py
    role_title: str                   # the bot's job title when the client leaves it blank
    handoff_rules: Tuple[str, ...]    # default "pass this to a human" topics
    buttons: Tuple[str, str]          # the two greeting buttons; the third is always the contact button
    contact_button: str
    intake: Tuple[str, ...]           # what staff need to know, so the bot asks useful next questions
    topics: Dict[str, str] = field(default_factory=dict)   # word in a past message -> what to call that topic
    guardrails: Tuple[str, ...] = ()  # things this kind of business must never say
    aliases: Tuple[str, ...] = ()     # other names for it (legacy persona values, AI output)
    signals: Tuple[str, ...] = ()     # words that suggest this industry, for detect_industry()

    @property
    def handoff_text(self) -> str:
        """One rule per line, the format the portal edits and tenants.handoff_rules stores."""
        return "\n".join(self.handoff_rules)

    @property
    def vocabulary(self) -> frozenset:
        """Words that are real words in this line of business (keeps them from being read as gibberish)."""
        words = set()
        for phrase in (*self.topics, *self.signals):
            words.update(re.findall(r"[a-z]+", phrase.lower()))
        return frozenset(words)


_PACKS: Tuple[IndustryPack, ...] = (
    IndustryPack(
        key="hospitality",
        label="Hotels, resorts & event venues",
        persona="hospitality",
        role_title="Reservations & Events Consultant",
        handoff_rules=_rules(
            "Custom or special requests beyond the standard packages",
            "Bookings, date holds and reservation confirmations",
        ),
        buttons=("📊 View Prices", "🍽️ View Menus"),
        contact_button="📞 Contact Manager",
        intake=(
            "What kind of stay or event it is (wedding, conference, family stay...)",
            "The date, or roughly when",
            "How many guests",
            "Which room, hall or package they are leaning towards",
        ),
        topics={
            "price": "prices & packages", "cost": "prices & packages", "ganan": "prices & packages",
            "rate": "prices & packages", "budget": "prices & packages", "pax": "prices & packages",
            "menu": "menu options", "food": "menu options", "kema": "menu options",
            "hall": "venue options", "ballroom": "venue options", "venue": "venue options",
            "room": "rooms", "stay": "your stay",
            "wedding": "your wedding plans", "event": "your event",
            "regist": "registration packages", "regi": "registration packages",
            "date": "dates & availability", "book": "booking",
            "brochure": "our brochures", "pdf": "our brochures",
        },
        guardrails=(
            "Never say a date, room or hall is available, held or booked. Only the team can confirm that.",
        ),
        aliases=("hotel", "hotels", "resort", "banquet", "banquets", "venue", "events", "villa", "guesthouse"),
        signals=("hotel", "resort", "banquet", "ballroom", "wedding", "venue", "guests", "rooms", "suite",
                 "concierge", "check-in", "villa", "events", "reception hall"),
    ),
    IndustryPack(
        key="restaurant",
        label="Restaurants, cafés & food delivery",
        persona="restaurant",
        role_title="Guest Relations Assistant",
        handoff_rules=_rules(
            "Large group bookings, private events and catering quotes",
            "Food allergy or dietary-safety assurances",
            "Problems with an order or a delivery",
        ),
        buttons=("🍽️ See Menu", "🪑 Book a Table"),
        contact_button="📞 Call Us",
        intake=(
            "Whether it is dine-in, takeaway or delivery",
            "The day and time",
            "How many people",
            "For delivery: the area or address",
        ),
        topics={
            "menu": "the menu", "dish": "the menu", "food": "the menu", "kema": "the menu",
            "table": "a table booking", "reserv": "a table booking", "book": "a table booking",
            "deliver": "delivery", "takeaway": "takeaway", "catering": "catering",
            "order": "your order",
        },
        guardrails=(
            "Never promise a dish is free of an allergen. Share what the knowledge base lists and pass allergy questions to the team.",
            "Never confirm a table or a delivery time. Take the details and let the team confirm.",
        ),
        aliases=("cafe", "café", "food", "bakery", "catering", "bar", "food_and_beverage", "f&b", "eatery"),
        signals=("restaurant", "cafe", "café", "bakery", "cuisine", "dishes", "dine", "dining", "takeaway",
                 "kitchen", "chef", "buffet", "kottu", "rice and curry"),
    ),
    IndustryPack(
        key="retail",
        label="Shops & online stores",
        persona="retail",
        role_title="Sales Assistant",
        handoff_rules=_rules(
            "Warranty claims, returns and exchanges",
            "Bulk or wholesale orders",
            "Damaged, missing or late deliveries",
        ),
        buttons=("🛍️ Browse Products", "🚚 Delivery Info"),
        contact_button="📞 Contact Us",
        intake=(
            "Which product, and the size, colour or model",
            "How many",
            "Where it needs to be delivered",
        ),
        topics={
            "stock": "what's in stock", "available": "what's in stock", "thiyenawada": "what's in stock",
            "deliver": "delivery", "shipping": "delivery", "courier": "delivery",
            "warranty": "warranty", "return": "returns & exchanges", "exchange": "returns & exchanges",
            "size": "sizes", "colour": "colours", "color": "colours",
            "order": "your order",
        },
        guardrails=(
            "Stock and prices come only from the live inventory or the knowledge base. Never say something is in stock from memory.",
            "Never promise a delivery date the knowledge base does not state.",
        ),
        aliases=("shop", "store", "ecommerce", "e_commerce", "boutique", "supermarket", "online_store"),
        signals=("shop", "store", "boutique", "products", "in stock", "warranty", "delivery island-wide",
                 "courier", "sizes", "wholesale", "showroom"),
    ),
    IndustryPack(
        key="clinic",
        label="Clinics, dental & healthcare",
        persona="clinic",
        role_title="Patient Care Coordinator",
        handoff_rules=(
            "Medical advice, symptoms, diagnosis, test results or medication questions",
            "Emergencies or urgent symptoms",
            "Appointment confirmations, changes and cancellations",
            "Insurance, billing disputes and refunds",
            "Complaints about care or staff",
            _HUMAN,
        ),
        buttons=("📅 Book Appointment", "🕒 Opening Hours"),
        contact_button="📞 Call Reception",
        intake=(
            "Which service or doctor they need",
            "The day and time that suits them",
            "The patient's name",
            "Whether they have visited before",
        ),
        topics={
            "appointment": "an appointment", "channel": "an appointment", "book": "an appointment",
            "doctor": "our doctors", "dr": "our doctors", "specialist": "our doctors",
            "report": "tests & reports", "test": "tests & reports", "scan": "tests & reports", "lab": "tests & reports",
            "dental": "dental care", "teeth": "dental care", "tooth": "dental care",
            "fee": "consultation fees", "charge": "consultation fees",
        },
        guardrails=(
            "Never diagnose, interpret symptoms or test results, or recommend or dose any medicine. Say plainly that a doctor has to answer that, and offer to arrange it.",
            "If someone describes an emergency (chest pain, trouble breathing, heavy bleeding, a serious injury, thoughts of self-harm), tell them to call emergency services or go to the nearest emergency room now, give the contact number, and hand off.",
            "Ask only for the health details needed to point them to the right service. Never ask for more.",
            "Never confirm an appointment. Take the details and let reception confirm.",
        ),
        aliases=("hospital", "dental", "dentist", "medical", "healthcare", "health", "doctor", "pharmacy",
                 "medical_center", "medical_centre", "veterinary", "vet"),
        signals=("clinic", "hospital", "dental", "dentist", "doctor", "patients", "patient", "consultation",
                 "channelling", "medical", "physiotherapy", "pharmacy", "opd", "treatment"),
    ),
    IndustryPack(
        key="salon",
        label="Salons, spas & beauty",
        persona="salon",
        role_title="Bookings Assistant",
        handoff_rules=_rules(
            "Appointment confirmations, changes and cancellations",
            "Bridal, group or package bookings",
            "Skin, scalp or allergy concerns about a treatment",
        ),
        buttons=("💇 Services & Prices", "📅 Book a Slot"),
        contact_button="📞 Contact Us",
        intake=(
            "Which service or treatment",
            "The day and time that suits them",
            "Whether they prefer a particular stylist or therapist",
        ),
        topics={
            "haircut": "hair services", "hair": "hair services", "cut": "hair services",
            "colour": "hair colouring", "color": "hair colouring",
            "facial": "facials & skin care", "skin": "facials & skin care",
            "nail": "nails", "manicure": "nails", "pedicure": "nails",
            "bridal": "bridal packages", "bride": "bridal packages",
            "massage": "spa treatments", "spa": "spa treatments",
            "appointment": "an appointment", "book": "an appointment",
        },
        guardrails=(
            "Never promise a result (\"this will fix it\", \"guaranteed\"). Describe the service as the knowledge base does.",
            "Allergy, skin-reaction or pregnancy questions about a treatment go to the team.",
            "Never confirm a slot. Take the details and let the team confirm.",
        ),
        aliases=("spa", "beauty", "barber", "barbershop", "beauty_salon", "bridal", "nails", "saloon"),
        signals=("salon", "saloon", "spa", "beauty", "barber", "haircut", "hair", "stylist", "facial", "bridal",
                 "manicure", "pedicure", "makeup", "massage"),
    ),
    IndustryPack(
        key="real_estate",
        label="Real estate & property",
        persona="real_estate",
        role_title="Property Consultant",
        handoff_rules=(
            "Price negotiation, offers and payment plans",
            "Arranging a viewing or site visit",
            "Legal, title, deed, loan or tax questions",
            _COMPLAINTS,
            _HUMAN,
        ),
        buttons=("🏠 View Listings", "📅 Book a Viewing"),
        contact_button="📞 Talk to an Agent",
        intake=(
            "Whether they want to buy or rent",
            "The area they are looking in",
            "Their budget range",
            "The type and size of property (bedrooms, or land extent)",
        ),
        topics={
            "rent": "renting", "rental": "renting", "lease": "renting",
            "buy": "buying", "sale": "properties for sale",
            "land": "land", "plot": "land", "perch": "land", "idam": "land",
            "apartment": "apartments", "house": "houses", "villa": "houses",
            "view": "a viewing", "visit": "a viewing",
            "budget": "your budget",
        },
        guardrails=(
            "Never state a property's legal or title status, expected returns, or anyone's loan eligibility unless the knowledge base says it word for word.",
            "Never say a property is still available, reserved or sold. Only an agent can confirm that.",
            "No investment advice. Describe the property; do not predict its value.",
        ),
        aliases=("realestate", "property", "properties", "realty", "real_estate_agency", "housing", "land_sales"),
        signals=("real estate", "property", "properties", "apartment", "apartments", "land", "perches", "perch",
                 "bedrooms", "rent", "lease", "listing", "listings", "deed", "site visit"),
    ),
    IndustryPack(
        key="education",
        label="Schools, institutes & courses",
        persona="education",
        role_title="Admissions Assistant",
        handoff_rules=_rules(
            "Admission decisions, eligibility exceptions and credit transfers",
            "Fee concessions, scholarships and instalment plans",
            "Anything about one student's results, attendance or records",
        ),
        buttons=("📚 Courses & Fees", "📝 How to Enrol"),
        contact_button="📞 Contact Us",
        intake=(
            "Which course or level they are interested in",
            "The student's age, grade or current qualification",
            "Which intake or start date they are aiming for",
            "Whether they prefer online or in-person classes",
        ),
        topics={
            "course": "our courses", "class": "our classes", "program": "our courses", "diploma": "our courses",
            "intake": "upcoming intakes", "batch": "upcoming intakes",
            "enrol": "enrolment", "enroll": "enrolment", "regist": "enrolment", "admission": "admissions",
            "exam": "exams", "schedule": "the class schedule", "timetable": "the class schedule",
            "certificate": "certificates",
        },
        guardrails=(
            "Never promise admission, exam results, a visa or a job.",
            "Never share one student's results, attendance or records with anyone on chat.",
        ),
        aliases=("school", "institute", "tuition", "academy", "university", "college", "training", "courses",
                 "preschool", "montessori"),
        signals=("school", "institute", "academy", "tuition", "course", "courses", "students", "student",
                 "classes", "intake", "syllabus", "diploma", "lecturer", "enrol", "admissions"),
    ),
    IndustryPack(
        key="fitness",
        label="Gyms & fitness studios",
        persona="fitness",
        role_title="Membership Assistant",
        handoff_rules=_rules(
            "Membership freezes, cancellations and refunds",
            "Injuries, medical conditions, diet or supplement advice",
            "Personal training and corporate packages",
        ),
        buttons=("💪 Memberships", "🗓️ Class Schedule"),
        contact_button="📞 Contact Us",
        intake=(
            "What they want to achieve",
            "The times of day they can train",
            "How long a membership they have in mind",
        ),
        topics={
            "member": "memberships", "package": "memberships", "join": "joining",
            "class": "the class schedule", "schedule": "the class schedule", "timetable": "the class schedule",
            "trainer": "personal training", "personal": "personal training",
            "trial": "a trial session", "yoga": "yoga", "zumba": "classes",
        },
        guardrails=(
            "No medical, injury, diet or supplement advice. A trainer has to answer that in person.",
            "Never promise a result or a timeline for one.",
        ),
        aliases=("gym", "yoga", "fitness_center", "fitness_centre", "crossfit", "pilates"),
        signals=("gym", "fitness", "membership", "memberships", "trainer", "trainers", "workout", "yoga",
                 "zumba", "crossfit", "pilates", "personal training"),
    ),
    IndustryPack(
        key="automotive",
        label="Vehicle sales, service & repairs",
        persona="automotive",
        role_title="Service Advisor",
        handoff_rules=_rules(
            "Repair quotes and fault diagnosis beyond the listed prices",
            "Service bookings and job status",
            "Warranty and insurance claims",
            "Trade-ins, finance and leasing",
        ),
        buttons=("🔧 Services & Prices", "📅 Book a Service"),
        contact_button="📞 Contact Us",
        intake=(
            "The vehicle's make, model and year",
            "What they need done, or what is going wrong",
            "The day that suits them",
        ),
        topics={
            "service": "servicing", "repair": "repairs", "fix": "repairs",
            "part": "spare parts", "spare": "spare parts",
            "tyre": "tyres", "tire": "tyres", "battery": "batteries",
            "quote": "a quote", "book": "a service booking",
            "warranty": "warranty", "lease": "finance & leasing", "finance": "finance & leasing",
        },
        guardrails=(
            "Never state what is wrong with a vehicle, or quote a repair that is not in the knowledge base. A technician has to look at it.",
            "If the problem sounds unsafe to drive with (brakes, steering, overheating, a warning light with smoke or noise), say so plainly and give the contact number.",
        ),
        aliases=("auto", "garage", "car", "cars", "vehicle", "vehicles", "motor", "motors", "car_sale",
                 "car_service", "workshop", "bike", "motorbike"),
        signals=("garage", "vehicle", "vehicles", "car", "cars", "motor", "service centre", "service center",
                 "spare parts", "tyres", "tyre", "engine", "workshop", "mileage", "showroom"),
    ),
    IndustryPack(
        key="travel",
        label="Travel agencies & tours",
        persona="travel",
        role_title="Travel Consultant",
        handoff_rules=_rules(
            "Custom itineraries and group quotes",
            "Booking confirmations, changes and cancellations",
            "Visa, insurance and immigration questions",
        ),
        buttons=("🧳 Tour Packages", "📅 Check Dates"),
        contact_button="📞 Talk to an Agent",
        intake=(
            "Where they want to go",
            "Their travel dates, or roughly when",
            "How many travellers",
            "Their budget range",
        ),
        topics={
            "tour": "tour packages", "package": "tour packages", "trip": "your trip",
            "visa": "visas", "flight": "flights", "ticket": "flights",
            "hotel": "hotels", "safari": "safaris", "transfer": "transfers",
            "date": "travel dates", "itinerary": "your itinerary",
        },
        guardrails=(
            "Never promise a visa will be approved, or that a seat, room or price is still available. An agent has to check.",
        ),
        aliases=("tour", "tours", "tourism", "travel_agency", "travels", "airline", "ticketing", "safari"),
        signals=("travel", "tour", "tours", "itinerary", "visa", "flight", "flights", "safari", "destination",
                 "destinations", "travellers", "airport transfer", "ticketing"),
    ),
    IndustryPack(
        key="professional",
        label="Professional & other services",
        persona="services",
        role_title="Client Services Assistant",
        handoff_rules=_rules(
            "Quotes for custom work",
            "Legal, tax or financial advice about their own situation",
            "Appointments and deadlines",
        ),
        buttons=("📋 Our Services", "📅 Book a Consult"),
        contact_button="📞 Contact Us",
        intake=(
            "What they need done",
            "When they need it by",
            "The best way and time to reach them",
        ),
        topics={
            "service": "our services", "quote": "a quote", "consult": "a consultation",
            "appointment": "an appointment", "deadline": "timelines", "document": "the documents needed",
        },
        guardrails=(
            "General information only. Never give legal, tax, financial or insurance advice about someone's own situation, and never promise an outcome.",
        ),
        aliases=("services", "service", "finance", "financial", "legal", "law", "accounting", "agency",
                 "consulting", "consultancy", "insurance", "cleaning", "repair", "repairs", "bank", "it"),
        signals=("consulting", "consultancy", "accounting", "audit", "legal", "law firm", "lawyers",
                 "insurance", "agency", "clients", "tax", "cleaning service"),
    ),
    IndustryPack(
        key="generic",
        label="Other / not listed",
        persona="generic",
        role_title="Customer Care Assistant",
        handoff_rules=_rules(
            "Custom or special requests beyond the standard packages",
            "Bookings, date holds and reservation confirmations",
        ),
        buttons=("📊 Prices", "ℹ️ About Us"),
        contact_button="📞 Contact Us",
        intake=(
            "What they need",
            "When they need it",
        ),
        topics={"book": "a booking", "deliver": "delivery", "order": "your order"},
        aliases=("other", "general", "unknown", "none"),
    ),
)

_BY_KEY: Dict[str, IndustryPack] = {p.key: p for p in _PACKS}
_BY_ALIAS: Dict[str, str] = {alias: p.key for p in _PACKS for alias in p.aliases}

INDUSTRY_KEYS: List[str] = [p.key for p in _PACKS]
INDUSTRY_CHOICES: List[dict] = [{"key": p.key, "label": p.label} for p in _PACKS]


def get_pack(key: str = "") -> IndustryPack:
    """The pack for an industry key; the generic pack for anything unknown."""
    return _BY_KEY.get(key or "", _BY_KEY["generic"])


def normalise_industry(value) -> str:
    """
    'Real Estate' -> 'real_estate', 'hotel' -> 'hospitality', 'gym' -> 'fitness'.
    Returns '' for anything that is not an industry (for example a tone word like 'friendly').
    """
    slug = re.sub(r"[\s\-/]+", "_", str(value or "").strip().lower()).strip("_")
    if not slug:
        return ""
    return slug if slug in _BY_KEY else _BY_ALIAS.get(slug, "")


def resolve_industry(industry, legacy_persona=None) -> str:
    """
    The industry a tenant row is running as. Rows created before tenants.industry existed
    carried it in persona_template ('hospitality', 'retail', 'services'), so that is the fallback.
    """
    return normalise_industry(industry) or normalise_industry(legacy_persona) or "generic"


def _mentions(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![^\W_]){re.escape(phrase)}(?![^\W_])", text) is not None


@lru_cache(maxsize=None)
def _topic_pattern(keyword: str) -> re.Pattern:
    """
    Long keywords are stems ('deliver' also finds 'delivery'). Short ones must be the whole
    word, give or take an ending: 'fee' finds 'fees' but not 'feel', 'part' not 'party'.
    Sinhala words take their endings glued on, so they are always matched as stems.
    """
    start = rf"(?<![^\W_]){re.escape(keyword)}"
    if len(keyword) >= 5 or not keyword.isascii():
        return re.compile(start)
    return re.compile(start + r"(?:s|es|ed|ing)?(?![^\W_])")


def detect_industry(text: str, name: str = "") -> str:
    """
    Best guess at the industry from a business's own words, or '' when it is not clear.

    Counts each pack's signal words (a word in the business NAME counts triple) and only
    answers when one industry is clearly ahead — a wrong guess would put a clinic's limits on
    a shop, so staying generic is the safe default.
    """
    body, title = (text or "").lower(), (name or "").lower()
    scores = []
    for pack in _PACKS:
        score = sum(1 for s in pack.signals if _mentions(s, body)) + 3 * sum(1 for s in pack.signals if _mentions(s, title))
        scores.append((score, pack.key))
    scores.sort(reverse=True)
    (best, key), (runner_up, _) = scores[0], scores[1]
    return key if best >= 2 and best > runner_up else ""


def button_phrase(title: str) -> str:
    """'📊 View Prices' -> 'view prices': the text a tap on that button arrives as, minus the icon."""
    return re.sub(r"^[\W_]+", "", (title or "").strip()).lower()


def parse_quick_buttons(raw) -> List[str]:
    """A tenant's own greeting-button titles: one per line, at most two, trimmed to Meta's limit."""
    titles = []
    for line in (raw or "").splitlines():
        title = line.strip()[:MAX_BUTTON_TITLE].strip()
        if title and title not in titles:
            titles.append(title)
    return titles[:2]


def quick_buttons(pack: IndustryPack, custom_titles: Sequence[str] = ()) -> List[dict]:
    """
    The reply buttons sent with a greeting: the client's own two if they set any, otherwise
    the industry's, followed by the contact button (always last, always the same id, so the
    webhook recognises a tap on it whatever it is called).
    """
    titles = list(custom_titles)[:2] or list(pack.buttons)
    buttons = [{"id": f"btn_quick_{i}", "title": title} for i, title in enumerate(titles, start=1)]
    buttons.append({"id": CONTACT_BUTTON_ID, "title": pack.contact_button})
    return buttons


def last_topic(user_messages: Iterable[str], pack: IndustryPack, own_topics: Sequence[str] = ()) -> str:
    """
    What a returning customer was last asking about, in this business's own words.
    `user_messages` is newest first. `own_topics` are names the client uses themselves (their
    quick-menu titles) and beat the industry's general ones. '' when nothing recognisable was said.
    """
    topics = {**pack.topics, **{k: v for k, v in _COMMON_TOPICS.items() if k not in pack.topics}}
    for message in user_messages:
        content = (message or "").lower()
        for name in own_topics:
            if name and name.lower() in content:
                return name
        for keyword, topic in topics.items():
            if _topic_pattern(keyword).search(content):
                return topic
    return ""


def playbook_prompt(pack: IndustryPack) -> str:
    """The block added to the system prompt so the model has this industry's instincts and limits."""
    lines = [
        f"--- INDUSTRY PLAYBOOK ({pack.label}) ---",
        "Before the team can properly help a customer of this kind of business, they usually need to know:",
        *(f"- {item}" for item in pack.intake),
        "Find these out naturally, one question at a time, and only the ones that matter for what the",
        "customer asked. Never ask for something they already told you, and never list them like a form.",
    ]
    if pack.guardrails:
        lines.append("")
        lines.append("Limits for this kind of business, on top of every rule above:")
        lines.extend(f"- {rule}" for rule in pack.guardrails)
    lines.append("------------------------------------------")
    return "\n".join(lines)
