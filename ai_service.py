import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from openai import AsyncOpenAI
from config import settings
from prompts.persona_system_prompt import build_persona_prompt
import llm_router
import conversation_memory
import reply_guards
import order_math
from industry_brain import last_topic, playbook_prompt
from kb_retrieval import select_context, _query_words

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# What made the Wasala bot read like a person rather than a chatbot. Each rule came from a real
# chat that went wrong; none of it is business-specific.
# ---------------------------------------------------------------------------

HUMAN_STYLE_RULES = (
    "--- SOUND LIKE A REAL PERSON (APPLIES TO EVERY REPLY) ---\n"
    "You are one person having one continuous chat, not a system answering tickets.\n"
    "- MATCH THEM: Mirror the customer's length, energy and formality. A three-word message gets a short reply. "
    "'bro' or 'machan' gets relaxed; 'sir' or 'madam' gets polite. If they mix languages, match their style.\n"
    "- REACT FIRST: Respond to what they actually said and how they said it (excitement, worry, a joke, a late-night "
    "message) before giving information. One short human beat, not a speech.\n"
    "- NO TEMPLATES: Never reuse an opening, a closing or a stock phrase from your earlier replies in this chat. Look "
    "at your previous messages and start differently. Any example wording in these instructions is illustration only: "
    "never copy it, say it your own way each time.\n"
    "- GREET ONCE: Greet and welcome only at the very start of a conversation. Mid-chat, just carry on. Never "
    "re-introduce yourself or the business.\n"
    "- TALK, DON'T LIST: Default to plain sentences. Use a list only when they ask for a breakdown or a comparison of "
    "three or more things. No label-style lines ('Price:', 'Capacity:') and no headings.\n"
    "- ONE THING AT A TIME: Give the piece they asked for, not everything you know. Leave room for them to ask more.\n"
    "- REMEMBER: Use what they already told you (date, numbers, name, worries) the way someone who was listening "
    "would. Never ask for something they already said.\n"
    "- HAVE A VIEW: When they ask what you would suggest, recommend one option and say why in a line, based on what "
    "they told you. Don't hand the list of options back to them.\n"
    "- NATURAL, NOT POLISHED: Fragments and small words like 'hmm', 'honestly', 'ah okay' are fine in moderation. "
    "Never fake typos.\n"
    "- SMALL TALK: If they are just chatting (how are you, thanks, good night, a joke), answer like a person. Don't "
    "steer every message back to a sale.\n"
    "- FRUSTRATION & DELAYS: If a customer is worried, annoyed, or complaining (late parcel, wrong size, waiting long), "
    "acknowledge the frustration immediately with genuine calm empathy. Never give bureaucratic excuses or pass blame to couriers. "
    "Focus straight on resolving it or offer immediate human manager handoff.\n"
    "- CELEBRATIONS & GOOD VIBES: If they mention a wedding, birthday, trip, or milestone, share a warm, brief one-beat congratulation "
    "before jumping to business.\n"
    "- SMART CLARIFYING: If an inquiry is broad (e.g. 'what items do you have?'), ask ONE helpful question to narrow it down "
    "instead of dumping a huge menu. Guide them like an attentive, knowledgeable shop assistant.\n"
    "- NUMBERS: Give an exact price once, cleanly. Don't repeat the same figure later in the chat unless they ask.\n"
    "- MESSAGE SHAPE: Write the way people text: one to three short messages. Put a blank line between separate "
    "messages, because each block is delivered as its own WhatsApp bubble. Most replies are one or two bubbles.\n"
    "- HONESTY STILL APPLIES: None of this means claiming to be human when someone sincerely asks, or inventing facts "
    "to sound friendly.\n"
    "------------------------------------------\n"
)

SMART_CHAT_RULES = (
    "--- SMART CHAT & MEMORY RULES ---\n"
    "1. UNREADABLE MESSAGES: If the customer sends random characters (e.g. 'asdfg', 'ffsdsff'), do NOT try to answer "
    "it. React the way a person would to a pocket-text: lightly, in your own words, and ask what they meant.\n"
    "2. REPEATED QUESTIONS: If the customer asks something you already answered, never point out that they asked "
    "before. Just answer again, shorter and in different words. They probably missed it or want reassurance.\n"
    "3. RETURNING CUSTOMERS: Only treat someone as coming back when the conversation notes say their previous "
    "message was a long time ago. Then pick the conversation back up like someone who remembers them, mentioning "
    "what you were discussing in your own words (no fixed welcome-back line). In an ongoing chat never greet them "
    "again or say 'welcome back': just continue.\n"
    "4. UNKNOWN PROPRIETARY FACTS VS GENERAL KNOWLEDGE: Never answer a clear question with 'I didn't understand'.\n"
    "   - PROPRIETARY FACTS: If the customer asks about THIS BUSINESS'S private details (an exact price, whether a specific item is in stock right now, "
    "a custom discount, a hall booking confirmation, or private account details) and it is NOT in the knowledge base, say plainly that you do not have "
    "that detail, offer to check with the team, then append [NOT_IN_KNOWLEDGE_BASE] at the very end of your reply. Never guess prices or stock.\n"
    "   - GENERAL KNOWLEDGE: If the customer asks a general question (about fabrics, sizing, styling advice, how delivery or bank transfers work in general, "
    "the date/time, local geography, or friendly small talk), answer warmly and intelligently using your general knowledge, then guide them back to what they need from the business.\n"
    "------------------------------------------\n"
)

GENERAL_KNOWLEDGE_RULES = (
    "--- GENERAL KNOWLEDGE & REAL-WORLD COMMON SENSE ---\n"
    "You possess broad, helpful real-world knowledge relevant to any business, anywhere in the world. "
    "Do NOT act like an empty machine that refuses to answer unless a fact is written in the business knowledge base.\n"
    "1. WHAT YOU CAN FREELY ANSWER FROM GENERAL KNOWLEDGE:\n"
    "   - Real-world advice & explanations: Fabric types (linen, cotton, silk, denim, rayon/viscose, polyester, durability, breathability, washing/care tips), "
    "standard garment sizing & measurements (how to measure chest/waist/hips in inches/cm, standard S/M/L/XL sizing across regions), styling tips, color matching, "
    "food and ingredient basics, health and wellness common sense, technology product categories, vehicle maintenance basics.\n"
    "   - Commerce & Logistics: How Cash on Delivery (COD) works, how bank transfers and online payments work, "
    "general shipping and delivery concepts, courier tracking basics, return and refund processes, customs and duties for international shipping.\n"
    "   - Geography & Local Context: Use whatever the business's location is (from the persona) to give locally relevant advice. "
    "If you know the region, reference local cities, transport, weather patterns, holidays, and cultural norms naturally. "
    "For any country, answer questions about its major cities, travel distances, climate, public holidays, and business customs.\n"
    "   - Time & Calendar: Today's date, day of the week, upcoming weekends, and time of day (referencing the Real-Time Calendar block).\n"
    "   - Conversational Etiquette & Small Talk: Responding warmly to 'how are you', 'thank you', 'good morning/night', polite humor, "
    "compliments, and wishing happy holidays or cultural celebrations appropriate to the customer's locale.\n"
    "   - Unit Conversions & Practical Math: Converting between metric and imperial, currency awareness, basic calculations "
    "the customer might need (area, quantity, approximate totals from listed prices).\n"
    "   - Industry Common Sense: General knowledge relevant to the business's industry — food safety basics for restaurants, "
    "appointment etiquette for clinics, travel tips for tourism, workout basics for gyms, property terminology for real estate.\n"
    "2. THE STRICT DISTINCTION:\n"
    "   - PROPRIETARY BUSINESS FACTS (Strict): Exact product prices, live inventory availability, custom discounts, private bank account "
    "numbers, and official booking agreements MUST come from the Knowledge Base or Inventory. If missing, say you will confirm with the team.\n"
    "   - GENERAL KNOWLEDGE (Helpful): Explaining what a material is, providing styling tips, clarifying how sizes usually work, "
    "answering calendar/time, discussing local geography, or polite conversation: answer directly, warmly, and intelligently without refusing or saying you don't know.\n"
    "----------------------------------------------------\n"
)

# Found by playing realistic customer conversations through the bot (realworld_eval.py).
REAL_WORLD_RULES = (
    "--- REAL-WORLD RULES (learned from real customers) ---\n"
    "1. YOU CAN ONLY SEE THE KNOWLEDGE BASE. You cannot look up orders, bookings, payments, deliveries or stock "
    "beyond what it says. Never say 'let me look into your order', 'I'll check the price right now' or 'on it'. "
    "If someone needs a lookup, say a person on the team will check it, and hand over.\n"
    "2. NEVER DESCRIBE A POLICY OR PROCESS THAT IS NOT WRITTEN DOWN. Refunds, who pays what, how long something "
    "takes, how a return works, how payment is returned: if the knowledge base doesn't say it, don't say it, not "
    "even as 'we usually...'. Give what it does say and hand the rest to the team.\n"
    "3. COMPLAINTS, REFUND DEMANDS, MISSING OR DAMAGED ORDERS, AND ANGRY CUSTOMERS: apologise once, plainly, "
    "and hand over to a person straight away. Don't interrogate them first, and don't promise a result.\n"
    "4. BOOKINGS AND ORDERS: you can't confirm a booking or take a payment yourself. Collect what the team "
    "needs, one thing at a time (never ask again for something already given), and once you have it say the team "
    "will confirm, and hand over.\n"
    "5. PRICE HAGGLING: be friendly and honest that the listed price is the price you can quote, and hand "
    "over to the manager who can decide on anything different. Never agree to a different price.\n"
    "6. SAFETY: allergies, medicines, symptoms, injuries and anything where a wrong answer could hurt someone: "
    "never say it is safe or give advice. Share only what the knowledge base says, and hand over to a person.\n"
    "7. EXACTLY ONE QUESTION at the end, never two. If you need two things, ask the more important one now.\n"
    "8. SHORT REPLIES LIKE 'ok', 'thanks', 'hmm': answer in a few words. Don't restart the sales pitch or invent "
    "something you're doing.\n"
    "9. WRITE IN THEIR LANGUAGE AND SCRIPT. Sinhala script in -> Sinhala script out, never romanised. Singlish in "
    "-> Singlish out (romanised Sinhala with the odd English word), not plain English.\n"
    "10. DON'T DO MATHS ON MONEY. Never add up, multiply or estimate a price yourself. If a VERIFIED CALCULATION "
    "block is given below, use its figures exactly. If there isn't one, quote the unit prices from the knowledge base "
    "and ask which products and how many, or say the team will confirm the total.\n"
    "11. DON'T GUESS WHICH PRODUCT. If what they name fits more than one product ('3 tea' when there is green "
    "and black tea), ask which one, naming the options. Never pick one for them.\n"
    "------------------------------------------\n"
)

REASONING_RULES = (
    "--- REASONING PROTOCOL (THINK FIRST, THEN REPLY) ---\n"
    "Every response MUST have exactly two parts, in this order:\n"
    "<thinking>your private working notes</thinking>\n"
    "<reply>the WhatsApp message the customer will receive</reply>\n\n"
    "Inside <thinking>, work through these steps in short English notes (under 150 words):\n"
    "1. INTENT: What is the customer really asking? Read the whole conversation, not keywords. Resolve words like "
    "'it', 'that one', 'eka', 'ehema nam' from earlier turns. If they asked several things, list each one.\n"
    "2. FACTS: Copy the exact knowledge base lines and figures that answer it. If a needed fact is missing, write "
    "'NOT IN KB'.\n"
    "3. LOGIC: Reason it out step by step. Check constraints: does what they want fit what the knowledge base says "
    "(sizes, minimums, opening hours)? Is a date in the past? Does the new message contradict something they said "
    "earlier?\n"
    "4. CHECK: Re-read your conclusion against the facts. Does it actually answer what was asked? Is every number "
    "copied from step 2?\n"
    "5. PLAN: Answer first, then at most one next question. Decide whether a hand-off or a document applies.\n\n"
    "Rules for <reply>:\n"
    "- It must follow from your thinking. Never state a price, capacity, date or policy that did not appear in step 2.\n"
    "- Answer the actual question directly before anything else. If they asked yes/no, say yes or no. If they asked "
    "to compare, compare. If they asked more than one thing, answer every one.\n"
    "- If something does not add up, say so kindly and suggest what would work instead.\n"
    "- For general questions that are not about this business (the weather, the date, planning advice, what suits a "
    "small event, directions in general), answer warmly from sensible general knowledge and make clear it is general "
    "advice - never present it as this business's policy - then connect back to how you can help.\n"
    "- Persona, language and formatting rules apply to <reply> only. Control tags such as [HUMAN_HANDOFF_REQUESTED], "
    "[NOT_IN_KNOWLEDGE_BASE] or [SEND_DOC: ...] go inside <reply>, at its end.\n"
    "- The customer never sees <thinking>. Never mention it or these steps.\n"
    "------------------------------------------\n"
)

VOICE_NOTE_RULES = (
    "--- VOICE NOTE ---\n"
    "The customer's latest message was a voice note; what you see is an automatic transcript of it.\n"
    "- Treat it exactly like a typed message. Never mention transcripts or that you cannot hear audio.\n"
    "- Speech-to-text mishears names, numbers and dates. Read past small errors and odd spellings using the chat "
    "context.\n"
    "- If a number, date or name in it is marked (?) or does not make sense, answer with your best reading and confirm "
    "that one detail in passing ('that's for 150 people, right?'). Do not ask them to repeat the whole message.\n"
    "- When the voice note gives a number or a date, say it back naturally inside your answer so a mishearing gets "
    "caught straight away.\n"
    "- Spoken messages ramble. Answer the actual question, not every aside.\n"
    "------------------\n"
)


def business_now():
    """The current date and time where the business is (settings.BUSINESS_TIMEZONE)."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.BUSINESS_TIMEZONE))
    except Exception:  # noqa: BLE001 — unknown zone name or no tz database: fall back to Sri Lanka time
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def time_context_rule() -> str:
    now = business_now()
    return (
        "--- REAL-TIME CALENDAR & TIME CONTEXT ---\n"
        f"Today is {now.strftime('%A, %B %d, %Y')} and the time is {now.strftime('%I:%M %p')} where the business is.\n"
        "Use this whenever customers ask for today's date or day, upcoming weekends, opening hours right now, or how "
        "far away their date is. Let the time of day colour your tone the way it would for a person (a late-night "
        "message, an early start), without announcing the time.\n"
        "------------------------------------------\n"
    )


def split_reasoning_and_reply(raw_text: str) -> Tuple[str, str]:
    """
    Separates the model's private <thinking> scratchpad from the customer-facing <reply>.
    Returns (thinking, reply). The reply is empty if the model was cut off mid-thought, so the
    caller asks again instead of sending reasoning to a customer.
    """
    text = (raw_text or "").strip()

    thinking_match = re.search(r"<thinking>(.*?)(?:</thinking>|$)", text, flags=re.DOTALL | re.IGNORECASE)
    thinking = thinking_match.group(1).strip() if thinking_match else ""

    reply_match = re.search(r"<reply>(.*?)(?:</reply>|$)", text, flags=re.DOTALL | re.IGNORECASE)
    if reply_match:
        reply = reply_match.group(1)
    elif re.search(r"<thinking>", text, flags=re.IGNORECASE) and not re.search(r"</thinking>", text, flags=re.IGNORECASE):
        reply = ""
    else:
        # Model skipped the <reply> wrapper: whatever sits outside the scratchpad is the reply
        reply = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)

    reply = re.sub(r"</?(?:thinking|reply)>", "", reply, flags=re.IGNORECASE).strip()
    return thinking, reply


async def _complete_reply(messages: List[Dict], temperature: float) -> Tuple[str, str]:
    """
    One reply from the chat models, with the private reasoning removed. Returns (model label, reply).
    A model that runs out of room mid-thought is asked once more for the reply alone.
    """
    result = await llm_router.complete("chat", messages, max_tokens=2048, temperature=temperature, timeout=45)
    thinking, reply = split_reasoning_and_reply(result.text)
    if thinking:
        logger.info(f"[chat] reasoning ({result.label}): {thinking[:600]}")
    if not reply:
        logger.warning(f"[chat] {result.label} was cut off before its reply; asking for the reply only")
        retry = list(messages) + [{"role": "user", "content": (
            "(System note, not from the customer: your last answer was cut off before the <reply>. Answer the "
            "customer's last message again, with only a short <thinking> and then the <reply>.)")}]
        result = await llm_router.complete("chat", retry, max_tokens=2048, temperature=temperature, timeout=45)
        _, reply = split_reasoning_and_reply(result.text)
        if not reply:
            raise llm_router.LLMUnavailable(f"{result.label} returned reasoning without a reply twice")
    logger.info(f"[chat] reply by {result.label} in {result.latency:.1f}s")
    return result.label, reply


def split_reply_into_bubbles(reply: str, max_bubbles: int = 3) -> List[str]:
    """
    Splits a reply on blank lines into separate WhatsApp messages, the way people text.
    Blocks that belong together (a heading and its list, contact lines) stay in one bubble.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", reply or "") if b.strip()]

    bubbles: List[str] = []
    for block in blocks:
        previous = bubbles[-1] if bubbles else ""
        continues_previous = bool(previous) and (
            bool(re.search(r":[\s*_👇]*$", previous))
            or bool(re.match(r"(?:[•\-📞📱💬👉⚠]|\d+[.)]\s)", block))
            or block.startswith("[")
        )
        if continues_previous:
            bubbles[-1] = f"{previous}\n\n{block}"
        else:
            bubbles.append(block)

    if len(bubbles) > max_bubbles:
        bubbles = bubbles[:max_bubbles - 1] + ["\n\n".join(bubbles[max_bubbles - 1:])]
    return bubbles


def typing_pause_seconds(text: str) -> float:
    """How long a person would plausibly take to type this message, capped so customers never wait long."""
    return min(4.0, 1.2 + len(text or "") / 80)

GREETING_WORDS = [
    "hi", "hello", "hey", "ayubowan", "good morning", "good afternoon",
    "good evening", "greetings", "start", "ආයුබෝවන්",
]

# Words that mean "send me a file", in English, Sinhala and Singlish. These are generic
# across every business, so they stay in code. WHICH file to send is decided by each
# tenant's own trigger keywords, stored on their documents.
DOC_REQUEST_WORDS = [
    "pdf", "brochure", "document", "file", "send", "share", "ewanna",
    "detail", "details", "list", "ලැයිස්තුව", "එවන්න",
]

ALL_DOCS_WORDS = ["all", "everything", "both", "every", "හැම", "සියලු"]


def is_pure_greeting(message: str) -> bool:
    msg = (message or "").lower().strip()
    return msg in GREETING_WORDS or (any(g in msg for g in GREETING_WORDS) and len(msg.split()) <= 2)


def get_pdfs_for_user_message(user_message: str, config=None) -> List[dict]:
    """
    Deterministic resolver deciding which of THIS TENANT's documents to attach.

    Previously this function returned one hardcoded client's three PDFs to every
    tenant's customers. It now matches the customer's message against the trigger
    keywords each client set on their own uploaded documents (see TenantDocument),
    so a business with no uploads simply sends nothing.
    """
    if config is None or not config.documents:
        return []

    msg = (user_message or "").lower().strip()

    def as_payload(doc) -> dict:
        return {"url": doc.url, "filename": doc.filename, "caption": doc.caption}

    # A bare greeting never triggers a file, unless they also asked for one.
    if is_pure_greeting(msg) and not any(k in msg for k in DOC_REQUEST_WORDS):
        return []

    # "send me everything" -> all of this tenant's sendable documents.
    if any(w in msg for w in ALL_DOCS_WORDS) and any(k in msg for k in DOC_REQUEST_WORDS):
        return [as_payload(d) for d in config.documents]

    matches = config.docs_matching(msg)
    if matches:
        return [as_payload(d) for d in matches]

    # They clearly asked for a file but named nothing specific: if the client uploaded
    # exactly one sendable document, send it. Otherwise stay quiet and let the bot ask.
    if any(k in msg for k in DOC_REQUEST_WORDS) and len(config.documents) == 1:
        return [as_payload(config.documents[0])]

    return []

def get_openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

async def transcribe_audio(audio_bytes: bytes, api_key: str = "") -> str:
    """
    Transcribes a WhatsApp voice note (OGG) to text. Uses Groq Whisper via llm_router, with
    OpenAI Whisper as backup when OPENAI_API_KEY is set. `api_key` is kept for old callers.
    """
    if not audio_bytes:
        return ""
    # Guide Whisper to understand Sri Lankan context and Singlish better
    whisper_prompt = (
        "The user is a customer contacting a business about products, prices and bookings. "
        "They may speak in English or in a local language including Sinhala, Singlish (Romanized Sinhala), "
        "Hindi, Arabic, Spanish, French, or another language. "
        "Examples of Singlish: 'meke ganan kohomada', 'ada open da', 'details tika ewanna'."
    )
    try:
        return await llm_router.transcribe_audio(audio_bytes, "voice_note.ogg", whisper_prompt)
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return ""

def get_time_greeting() -> Tuple[str, str, str]:
    """
    Returns time-aware greeting tuple (English, Sinhala, Singlish).
    Uses the business's configured timezone (settings.BUSINESS_TIMEZONE).
    """
    try:
        now = business_now()
        hour = now.hour
    except Exception:
        hour = 12

    if 5 <= hour < 12:
        en = "Good Morning ☀️"
        si = "සුභ උදෑසනක් ☀️"
        sg = "Good Morning ☀️"
    elif 12 <= hour < 17:
        en = "Good Afternoon 🌤️"
        si = "සුභ දහවල් කාලයක් 🌤️"
        sg = "Good Afternoon 🌤️"
    else:
        en = "Good Evening 🌙"
        si = "සුභ සැන්දෑවක් 🌙"
        sg = "Good Evening 🌙"

    return en, si, sg

# Singlish / Romanized Sinhala vocabulary. No business words belong here: 'wedding' used to be
# on this list, so every English question about a wedding was answered in Singlish.
SINGLISH_KEYWORDS = [
    "ayubowan", "ayubowam", "subha", "udasanak", "dahawalak", "sandhawak",
    "kohomada", "gaana", "gana", "kiyada", "ekak", "apita", "hari", "ewanna",
    "wistara", "wistarathama", "ganan", "pahan", "monawada", "puluwanda", "puluwan",
    "denna", "kiyanna", "balanna", "neda", "ane", "bro", "macho", "sthuthi",
    "bohowama", "oya", "oyata", "oyalage", "mata", "wena", "thawa",
    "diyan", "elama", "obata", "obatai", "gollan", "hitanne", "kello", "kollo",
    "thiyenawa", "thiyenawada", "thiyeda", "thiyanawa", "thiyanawada",
    "meka", "araka", "monada", "mokakda", "mokada", "kawadda",
    "koheda", "kohedha", "danna", "dannako", "ganna", "gandako", "denda",
    "nadda", "naha", "nehe", "naa", "awulak", "ehenam",
    "karanna", "karannada", "gannada", "tikak", "oyala", "oyalata", "onna", "menna",
]

# Singlish glues endings onto words ('ewannako', 'hariyata'), so a keyword may START a word.
# It may not sit inside one: matching anywhere read 'brochure' (bro), 'royal' (oya), 'cleaner'
# (ane) and 'sharing' (hari) as Singlish. The three-letter ones must be the whole word.
_SINGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    + "|".join(re.escape(w) + (r"(?![a-z])" if len(w) <= 3 else "") for w in SINGLISH_KEYWORDS)
    + r")"
)


def detect_language_intent(user_message: str, chat_history: List[Dict[str, str]] = None) -> str:
    """
    Strict Multilingual Language Intent Detector.
    Returns: 'sinhala', 'singlish', or 'english'.
    Performs deep keyword matching and maintains language continuity from chat history.
    """
    msg = user_message.lower().strip()

    # 1. Sinhala Script Check
    if any('\u0d80' <= c <= '\u0dff' for c in user_message) or any(w in msg for w in ["සුභ", "ආයුබෝවන්", "මිල", "ගණන්", "මෙන්නු", "විස්තර", "ස්තුතියි", "කොහොමද"]):
        return "sinhala"

    # 2. Singlish / Romanized Sinhala Check
    if _SINGLISH_RE.search(msg):
        return "singlish"

    # 3. Check Chat History to Maintain Conversation Language Continuity
    if chat_history:
        for past_msg in reversed(chat_history[-6:]):
            past_text = past_msg.get("content", "").lower()
            if any('\u0d80' <= c <= '\u0dff' for c in past_text):
                return "sinhala"
            if _SINGLISH_RE.search(past_text):
                return "singlish"

    return "english"


# Words that show a message is real language, whatever the business. The small everyday words
# matter most: without them "do you have parking?" matched nothing and was called gibberish.
# Words special to one business (its products, its halls) are NOT listed here - they arrive
# through `known_words`, taken from that tenant's own knowledge base and industry.
_EVERYDAY_WORDS = {
    "a", "i", "an", "am", "is", "are", "do", "does", "the", "to", "of", "in", "on", "at", "it", "me",
    "my", "we", "you", "your", "and", "or", "for", "with", "this", "that", "have", "has", "there",
    "any", "much", "many", "get", "give", "tell", "about", "from", "not", "now",
    "hi", "hello", "hey", "yes", "no", "ok", "okay", "thanks", "thank", "please",
    "how", "what", "when", "where", "who", "why", "which", "can", "could", "will", "would", "want", "need",
    "good", "morning", "afternoon", "evening", "night", "today", "tomorrow", "sir", "madam", "bro",
    "price", "prices", "cost", "rate", "fee", "discount", "negotiate", "cheap", "offer",
    "book", "booking", "date", "time", "open", "close", "hours", "available", "location", "address",
    "start", "help", "contact", "manager", "call", "number", "phone", "send", "pdf", "brochure",
    "detail", "details", "info", "list", "menu", "package", "service", "order", "buy", "delivery",
    "size", "sizes", "fabric", "material", "color", "colour", "cotton", "linen", "cod", "cash",
    "bank", "slip", "payment", "account", "transfer", "exchange", "return", "refund", "track",
    "tracking", "courier", "dress", "shirt", "pant", "pants", "trouser", "tshirt", "fit",
    "inches", "cm", "islandwide", "stock", "item", "items", "sample", "model",
    "ayubowan", "kohomada", "ewanna", "denna", "kiyanna", "puluwan", "hari", "oya", "mata",
    "ganan", "mila", "kiyada", "sthuthi", "eka", "eke", "ekak", "meka", "ada", "heta",
    "thiyenawa", "thiyenawada", "koheda", "danna", "ganna", "nadda", "awulak",
}


def is_gibberish(text: str, known_words=frozenset()) -> bool:
    """
    Detects if a message is gibberish / random keystrokes / spam.
    Returns True if the message appears to be meaningless.

    `known_words` are extra lowercase words that count as real for this tenant.
    """
    msg = text.strip()

    # Very short messages (1-2 chars) that aren't common words
    if len(msg) <= 2 and msg.lower() not in ["hi", "ok", "no", "ya", "hm"]:
        return True

    # Only whitespace or empty
    if not msg:
        return True

    # Strip non-alpha characters for analysis
    alpha_only = re.sub(r'[^a-zA-Z]', '', msg)

    # If it has Sinhala script, it's NOT gibberish (let language detector handle it)
    if any('\u0d80' <= c <= '\u0dff' for c in msg):
        return False

    # If it's mostly numbers or emojis, not gibberish (could be a phone number, etc.)
    if len(alpha_only) < 2:
        return False

    # Check 1: Too many consecutive identical characters — "hhhhhh", "aaaaaa"
    if re.search(r'(.)\1{3,}', alpha_only.lower()):
        return True

    # Check 2: Vowel ratio — real English/Singlish words have vowels
    vowels = sum(1 for c in alpha_only.lower() if c in 'aeiou')
    vowel_ratio = vowels / len(alpha_only) if alpha_only else 0
    if len(alpha_only) >= 4 and vowel_ratio < 0.1:
        # Almost no vowels in 4+ characters — likely gibberish ("ffsdsff", "bcdfgh")
        return True

    # Check 3: No recognizable words at all — check against everyday words plus this tenant's own
    words_in_msg = set(re.findall(r'[a-zA-Z]+', msg.lower()))
    if words_in_msg and not (words_in_msg & _EVERYDAY_WORDS or words_in_msg & known_words):
        # None of the words match any known word — likely gibberish
        # But only flag if all words are short random sequences
        avg_word_len = sum(len(w) for w in words_in_msg) / len(words_in_msg)
        if avg_word_len < 8:  # Long words might be legit terms we don't know
            return True

    return False


def find_repeated_question(user_message: str, chat_history: List[Dict[str, str]]) -> Tuple[bool, str]:
    """
    Checks if the customer has asked a very similar question before.
    Returns (is_repeat, previous_answer) tuple.
    
    Uses simple word overlap — if 60%+ of the words match a previous user message,
    it's considered a repeat.
    """
    if not chat_history or len(chat_history) < 2:
        return False, ""

    msg_words = set(re.findall(r'[a-zA-Z\u0d80-\u0dff]+', user_message.lower()))

    # Skip very short messages (greetings etc.) — no point detecting "hi" as a repeat
    if len(msg_words) <= 2:
        return False, ""

    # Scan past user messages and find the assistant reply that followed
    for i, entry in enumerate(chat_history):
        if entry.get("role") != "user":
            continue

        past_words = set(re.findall(r'[a-zA-Z\u0d80-\u0dff]+', entry.get("content", "").lower()))
        if len(past_words) <= 2:
            continue

        # Calculate word overlap
        overlap = msg_words.intersection(past_words)
        overlap_ratio = len(overlap) / max(len(msg_words), 1)

        if overlap_ratio >= 0.6:
            # Found a repeat — get the assistant reply that followed
            if i + 1 < len(chat_history) and chat_history[i + 1].get("role") == "assistant":
                prev_answer = chat_history[i + 1].get("content", "")
                # Truncate to first 200 chars for the summary
                if len(prev_answer) > 200:
                    prev_answer = prev_answer[:200] + "..."
                return True, prev_answer

    return False, ""


def get_returning_customer_context(chat_history: List[Dict[str, str]], config=None) -> Tuple[bool, str]:
    """
    Checks if this is a returning customer (has significant past history).
    Returns (is_returning, last_topic_summary). The topic is named in this tenant's own terms -
    their industry's vocabulary and their quick-menu titles - and is '' when nothing they said
    is recognisable, so a salon's customer is never welcomed back to "your event plans".
    """
    if not chat_history or len(chat_history) < 4:
        return False, ""

    if config is None:
        from tenant_config import BotConfig
        config = BotConfig(tenant_id=0, business_name="our business")

    # Scan user messages newest-first to find the last meaningful topic
    past_messages = [e.get("content", "") for e in reversed(chat_history) if e.get("role") == "user"]
    return True, last_topic(past_messages, config.pack, config.menu_titles)


# Asking what something costs, in English, Singlish and Sinhala. Whole words only, so that
# "coffee" (fee) and "celebrate" (rate) are not read as price questions.
_PRICE_RE = re.compile(
    r"(?<![a-z])(?:prices?|pricing|rates?|costs?|fees?|charges?|how much|ganan|gaana|kiyada|keeyada|mila)(?![a-z])"
    r"|මිල|ගණන්|කීයද"
)
# A line that states an amount: a currency mark beside a number, or any number of 3+ digits.
_AMOUNT_RE = re.compile(r"(?:rs\.?|lkr|inr|usd|eur|gbp|aed|aud|cad|sgd|myr|thb|jpy|krw|\$|€|£|¥|₹|₱|฿|﷼|RM\s?)\s?\d|\d\s?/-|\d[\d,]{2,}", re.IGNORECASE)


def relevant_kb_lines(message: str, kb_lines: List[str], limit: int = 6) -> List[str]:
    """
    The knowledge-base lines that share the most words with the customer's message, for the
    offline fallback. A price question also pulls in lines that state an amount.

    This replaced a fixed list of one client's words ("silver", "ballroom", "corkage"), which
    found nothing at all in any other business's knowledge base. A matching FAQ question is
    answered with its "A:" line. Results come back in document order.
    """
    words = _query_words(message)
    wants_price = bool(_PRICE_RE.search(message.lower()))
    scored = []
    for position, line in enumerate(kb_lines):
        # Not facts: source banners, headings, instructions to the AI, and answers (reached via their Q:)
        if line.startswith(("=====", "---", "#", "[", "A:")):
            continue
        score = len(words & _query_words(line))
        if wants_price and _AMOUNT_RE.search(line):
            score += 1
        if score:
            scored.append((score, position))
    scored.sort(key=lambda item: (-item[0], item[1]))

    chosen = []
    for _, position in sorted(scored[:limit], key=lambda item: item[1]):
        line = kb_lines[position]
        if line.startswith("Q:") and position + 1 < len(kb_lines) and kb_lines[position + 1].startswith("A:"):
            line = kb_lines[position + 1][2:]
        line = line.lstrip("-•* ").strip()
        if line and line not in chosen:
            chosen.append(line)
    return chosen


def smart_knowledge_base_search(user_message: str, knowledge_base: str, system_prompt: str, chat_history: List[Dict[str, str]] = None, config=None) -> str:
    """
    Smart Local Knowledge-Base AI Simulator with Strict Multilingual Continuity,
    Front Office Contact Sharing, and Human WhatsApp Handoff.

    Runs when OpenAI is unavailable. Every business detail it speaks - name, hotline,
    manager link - comes from `config` (the tenant's own row), never from constants.
    """
    if config is None:
        from tenant_config import BotConfig
        config = BotConfig(tenant_id=0, business_name="our business")
    msg = user_message.lower().strip()
    kb_lines = [line.strip() for line in knowledge_base.split("\n") if line.strip()]

    # Detect language intent strictly with history continuity
    lang = detect_language_intent(user_message, chat_history)
    is_sinhala = (lang == "sinhala")
    is_singlish = (lang == "singlish")

    # Front Office Phone & Customization Handoff Check
    # "fo" (front office) only as a word of its own - as a substring it matched "for", "info", "food".
    is_front_office_query = bool(re.search(r"\bfo\b", msg)) or any(w in msg for w in ["reception", "front office", "phone", "call", "contact", "number", "දුරකථන", "අංකය", "කෝල්", "hotline"])
    is_customization_query = any(w in msg for w in [
        "customize", "customise", "custom", "swap", "discount", "special menu",
        "change menu", "manager", "human", "person", "staff",
        "negotiate", "negotiation", "lower price", "reduce", "cheap", "cheaper",
        "less money", "bargain", "deal", "best price", "better price", "offer",
        "special price", "can you reduce", "too expensive", "too much", "overpriced",
        "complaint", "complain", "unhappy", "not happy", "problem", "issue",
        "refund", "cancel", "cancellation",
        "වෙනස්", "කතා කරන්න", "වට්ටම්", "අඩු කරන්න", "මිල අඩු",
        "kam karanna", "godak", "wadiya", "epa bro", "mehema bari",
        "wattam", "ada karanna", "mila adu", "negotiate karanna",
        "talk to someone", "real person", "speak to someone", "call me",
        "date hold", "hold the date", "reserve", "reservation",
    ])

    # Fetch time-aware Sri Lankan greetings
    greeting_en, greeting_si, greeting_sg = get_time_greeting()

    # Pure Greeting Check
    greetings_list = ["hi", "hello", "hey", "ayubowan", "good morning", "good afternoon", "good evening", "greetings", "start", "ආයුබෝවන්"]
    is_pure_greeting = msg in greetings_list or (any(g in msg for g in greetings_list) and len(msg.split()) <= 2)

    # Check if this is the first message in the conversation session
    is_first_message = not chat_history or len(chat_history) == 0

    # -----------------------------------------------------------
    # GIBBERISH / SPAM DETECTION (runs before everything else)
    # -----------------------------------------------------------
    if not is_pure_greeting and not is_front_office_query and not is_customization_query:
        # Real words for THIS business: its industry's vocabulary and whatever its own documents say.
        known_words = config.pack.vocabulary | set(re.findall(r"[a-z]+", knowledge_base.lower()))
        if is_gibberish(user_message, known_words):
            if is_sinhala:
                return "මට ඒක හරියට තේරුණේ නැ 😅\nඔබට අවශ්‍ය දේ නැවත ටයිප් කරන්න පුළුවන්ද?"
            elif is_singlish:
                return "Sorry, ekata man hariyata theruna naa 😅\nOyata one de aluthen type karanna puluwan da?"
            else:
                return "Sorry, I didn't quite catch that 😅\nCould you rephrase what you need? I'm here to help with anything at all"

    # -----------------------------------------------------------
    # REPEATED QUESTION DETECTION
    # -----------------------------------------------------------
    if not is_pure_greeting and chat_history:
        is_repeat, prev_answer = find_repeated_question(user_message, chat_history)
        if is_repeat and prev_answer:
            if is_sinhala:
                return (
                    f"ඔබ මීට කලින් ඒ ගැනම අහලා තිබුණා — මම පෙර දුන් පිළිතුර මෙන්න:\n\n"
                    f"_{prev_answer}_\n\n"
                    f"ඊට වඩා විස්තර ඕනෑ නම් කියන්න 🙏"
                )
            elif is_singlish:
                return (
                    f"Oya kalin me gamama ahala thibuna — man kalin dunna answer eka menna:\n\n"
                    f"_{prev_answer}_\n\n"
                    f"Ekata wada wistara one nam kiyanna 🙏"
                )
            else:
                return (
                    f"You actually asked something similar earlier — here's what I shared:\n\n"
                    f"_{prev_answer}_\n\n"
                    f"Want me to go deeper on any part of that?"
                )

    # -----------------------------------------------------------
    # RETURNING CUSTOMER GREETING (on pure greetings only)
    # -----------------------------------------------------------
    if is_pure_greeting and not is_first_message:
        is_returning, topic = get_returning_customer_context(chat_history, config)
        if is_returning:
            # The "last time we talked about..." line is only said when the topic is actually known.
            if is_sinhala:
                recap = f"පසුගිය වතාවේ අපි _{topic}_ ගැන කතා කරමින් හිටියා.\n" if topic else ""
                return (
                    f"👋 *ආයුබෝවන්! {greeting_si}!* ආපහු සාදරයෙන් පිළිගනිමු 🌺\n\n"
                    f"{recap}"
                    f"අද මොකද බලාපොරොත්තු වෙන්නේ?"
                )
            elif is_singlish:
                recap = f"Kalin apa _{topic}_ gena katha karanawa hitiya.\n" if topic else ""
                return (
                    f"👋 *Ayubowan! {greeting_sg}!* Aapu welcome back 🌺\n\n"
                    f"{recap}"
                    f"Ada mokada balaporoththu wenne?"
                )
            else:
                recap = f"Last time we were chatting about _{topic}_.\n" if topic else ""
                return (
                    f"👋 *{greeting_en}!* Welcome back 🌺\n\n"
                    f"{recap}"
                    f"How can I help you today?"
                )

    # 1. Front Office Contact Query Response — numbers come from the tenant's own row
    if is_front_office_query and not is_customization_query:
        contacts = config.contact_lines()
        if not contacts:
            contacts = "• (contact number not set yet)"
        if is_sinhala:
            return (
                f"📞 *{config.business_name} සම්බන්ධතා තොරතුරු:* \n\n"
                f"{contacts}\n\n"
                f"ඕනෑම තොරතුරක් සඳහා අප හා සම්බන්ධ කර ගත හැක."
            )
        elif is_singlish:
            return (
                f"📞 *{config.business_name} Contact Details:* \n\n"
                f"{contacts}\n\n"
                f"Onema wistarakatama apata call karanna puluwan."
            )
        else:
            return (
                f"📞 *{config.business_name} Contact Details:* \n\n"
                f"{contacts}\n\n"
                f"Our team is available to assist you with any inquiries."
            )

    # 2. Customization / Human Manager Handoff Response
    if is_customization_query:
        manager_wa_link = config.manager_link(f"Hi, I have a question about {config.business_name}")
        fallback_line = (
            f"Or call us directly at {config.front_office_number}." if config.has_front_office else ""
        )
        if manager_wa_link:
            handoff_target = f"👉 {manager_wa_link}"
        elif config.has_front_office:
            handoff_target = f"👉 {config.front_office_number}"
        else:
            handoff_target = "👉 Our team will get back to you shortly."

        if is_sinhala:
            return (
                f"✨ *විශේෂ අවශ්‍යතා සහ වෙනස් කිරීම්:* \n\n"
                f"අපගේ කළමනාකාරවරයා සමඟ සෘජුවම සම්බන්ධ වන්න:\n"
                f"{handoff_target}\n\n"
                f"{fallback_line} [HUMAN_HANDOFF_REQUESTED]"
            )
        elif is_singlish:
            return (
                f"✨ *Special Requests & Customizations:* \n\n"
                f"Ape manager ekka kelinma katha karanna:\n"
                f"{handoff_target}\n\n"
                f"{fallback_line} [HUMAN_HANDOFF_REQUESTED]"
            )
        else:
            return (
                f"✨ *Special Requests & Customizations:* \n\n"
                f"You can chat directly with our manager here:\n"
                f"{handoff_target}\n\n"
                f"{fallback_line} [HUMAN_HANDOFF_REQUESTED]"
            )

    # 3. Pure Greeting Response (Strict Language - NO PDF)
    if is_pure_greeting:
        business = config.display_name()
        if is_sinhala:
            return f"👋 *ආයුබෝවන්! {greeting_si}!* {business} වෙත සාදරයෙන් පිළිගනිමු. 🌺\n\nමම ඔබට උදව් කරන්නේ කෙසේද?"
        elif is_singlish:
            return f"👋 *Ayubowan! {greeting_sg}!* {business} ekata sadarayen piligannawa. 🌺\n\nApata oyata udaw karanne kohomada?"
        else:
            return f"👋 *{greeting_en}!* Welcome to {business}. 🌺\n\nHow may we help you today?"

    # The lines of THIS tenant's knowledge base that the message is about
    matched_lines = relevant_kb_lines(msg, kb_lines)

    # Response with language consistency & session awareness (Greetings ONLY on first message)
    if matched_lines:
        details = "\n".join(f"• {line}" for line in matched_lines)
        # Only say a file is attached when one of this tenant's documents really will be.
        will_attach = bool(get_pdfs_for_user_message(user_message, config))
        if is_sinhala:
            header = f"✨ *{greeting_si}! ඔබ ඉල්ලා සිටි විස්තර මෙන්න:*\n\n" if is_first_message else "✨ *ඔබ ඉල්ලා සිටි විස්තර මෙන්න:*\n\n"
            attach_note = "\n\nමම ඔබ වෙනුවෙන් නිල PDF ලේඛනය පහතින් ලබා දී ඇත! 👇" if will_attach else ""
        elif is_singlish:
            header = f"✨ *{greeting_sg}! Oyala illa hitapu details menna:*\n\n" if is_first_message else "✨ *Oyala illa hitapu details menna:*\n\n"
            attach_note = "\n\nMan official PDF eka pahalin attach kara! 👇" if will_attach else ""
        else:
            header = f"✨ *{greeting_en}! Thank you for contacting {config.business_name}. Here are the requested details:*\n\n" if is_first_message else "✨ *Here are the requested details:*\n\n"
            attach_note = "\n\nI have attached our official PDF for you below! 👇" if will_attach else ""
        return f"{header}{details}{attach_note}"
    else:
        # Can't find a relevant answer in KB — ask them to clarify instead of immediately handing off
        if is_sinhala:
            return (
                f"මට ඒක හරියට තේරුණේ නැහැ. මට ඔබට උදව් කරන්න පුළුවන් වෙන්න, "
                f"කරුණාකර ඒ ගැන නැවත පැහැදිලිව කියන්න පුළුවන්ද? 🙏"
            )
        elif is_singlish:
            return (
                f"Mata eka hariyata therune naa. Mata oyata udaw karanna puluwan wenna, "
                f"karunakerela eka gana aayeth kiyanna puluwanda? 🙏"
            )
        else:
            return (
                f"I didn't quite understand that. To help you better, "
                f"could you please tell me again or rephrase your question? 🙏"
            )


_PHONE_LIKE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")   # \s: models often use non-breaking spaces
_URL_RE = re.compile(r"https?://\S+")


def unsupported_reply_numbers(reply: str, allowed_text: str, config) -> List[str]:
    """
    Numbers in the reply that appear nowhere in `allowed_text` (knowledge base, owner settings,
    conversation). The business's own phone numbers are allowed in any spacing, and links are
    ignored (wa.me links and file URLs are full of digits).
    """
    from training_pipeline import numbers_in

    phones = set()
    for number in (getattr(config, "front_office_number", None), getattr(config, "manager_whatsapp", None)):
        digits = re.sub(r"\D", "", number or "")
        if digits:
            phones.add(digits)
            # Common international format: strip country code and add local leading zero
            # Works for +94 (Sri Lanka), +91 (India), +44 (UK), +1 (US/CA), etc.
            if len(digits) > 6:
                for prefix_len in (1, 2, 3):  # try 1-3 digit country codes
                    phones.add("0" + digits[prefix_len:])

    check = _URL_RE.sub(" ", reply or "")
    check = re.sub(r"[  -​  　]", " ", check)
    check = _PHONE_LIKE_RE.sub(lambda m: " " if re.sub(r"\D", "", m.group(0)) in phones else m.group(0), check)
    allowed = set(numbers_in(allowed_text)) | phones
    return sorted({n for n in numbers_in(check) if n not in allowed})


def _has_knowledge(knowledge_base: str) -> bool:
    from training_service import EMPTY_KNOWLEDGE_BASE
    text = (knowledge_base or "").strip()
    return bool(text) and text != EMPTY_KNOWLEDGE_BASE


def no_knowledge_reply(lang: str, config) -> str:
    """For a business with nothing uploaded yet: promise a person, never an answer."""
    contacts = config.contact_lines() if config else ""
    if lang == "sinhala":
        text = "ඔබගේ පණිවිඩයට ස්තුතියි! මම මේ ගැන අපේ කණ්ඩායමෙන් විමසා ඔබට දන්වන්නම් 🙏"
    elif lang == "singlish":
        text = "Message ekata sthuthi! Man meka ape team eken ahala oyata kiyannam 🙏"
    else:
        text = "Thanks for your message! Let me check that with our team and get back to you 🙏"
    return f"{text}\n\n{contacts}".strip() if contacts else text


def safe_unknown_reply(lang: str, config) -> str:
    """Used when the model keeps stating figures we can't verify: say so honestly, offer a human."""
    contacts = config.contact_lines() if config else ""
    if lang == "sinhala":
        text = "නිවැරදිම තොරතුර ඔබට දෙන්න, මම ඒ ගැන අපේ කණ්ඩායමෙන් තහවුරු කරගන්නම් 🙏"
    elif lang == "singlish":
        text = "Hariyatama details denna, man eka ape team eken confirm karagannam 🙏"
    else:
        text = "I want to give you the exact figure, so let me confirm that with our team 🙏"
    return f"{text}\n\n{contacts}".strip() if contacts else text


# Tolerant: models write "[SEND_DOC: …]", "[ SEND_DOC : …]", "[send_doc: …]".
_SEND_DOC_RE = re.compile(r"\[\s*SEND_DOC\s*:\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]", re.IGNORECASE)


def whatsapp_format(text: str) -> str:
    """
    Models write Markdown; WhatsApp does not render it. **bold** shows literal asterisks and
    "# Heading" shows a hash. Convert to WhatsApp's own syntax.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"_\1_", text)                        # __it__   -> _it_
    text = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", r"*\1*", text)      # # Head   -> *Head*
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)                # - item   -> • item
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def resolve_reply_documents(reply: str, user_message: str, config) -> Tuple[str, List[dict]]:
    """
    Strip every [SEND_DOC: ...] tag from the AI's reply (customers must never see them) and
    decide which of THIS tenant's files to attach.

    The AI sees the whole conversation, so its choice wins when it made one — keyword
    matching alone sent the banquet menu for "Silver menu price" because 'menu' is a keyword.
    Tags are only honoured for URLs that belong to this tenant's own documents, so a model
    can never make the bot send an arbitrary link. With no valid tag, the deterministic
    keyword resolver decides, as before.
    """
    tagged_urls = _SEND_DOC_RE.findall(reply or "")
    clean = _SEND_DOC_RE.sub("", reply or "")
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()

    allowed = {d.url: d for d in (config.documents if config else [])}
    chosen, seen = [], set()
    for url in tagged_urls:
        doc = allowed.get(url.strip())
        if doc and doc.url not in seen:
            seen.add(doc.url)
            chosen.append({"url": doc.url, "filename": doc.filename, "caption": doc.caption})
    if not chosen:
        chosen = get_pdfs_for_user_message(user_message, config)
    return clean, chosen


@dataclass
class BotReply:
    text: str
    handoff: bool = False
    # The knowledge base did not have the answer. Real chats record these as KnowledgeGaps,
    # so the owner can teach the bot (see learning_service.py).
    unknown: bool = False


# Added by the model at the end of a reply when the knowledge base lacked the answer. Tolerant
# of the spacing and case models use. Always removed before a customer sees the reply.
_UNKNOWN_TAG_RE = re.compile(r"\[\s*NOT[\s_]*IN[\s_]*KNOWLEDGE[\s_]*BASE\s*\]", re.IGNORECASE)


# Backup for when the model admits it doesn't know but forgets the tag (measured: "I honestly am
# not sure if we have dedicated parking... let me check with the front desk team" came untagged).
# Only used on replies that are not hand-offs, so "our manager will sort out the discount" is
# never filed as a missing fact. A false match costs the owner one "Ignore" click.
_UNSURE_RE = re.compile(
    r"not (?:\w+ )?sure|don'?t (?:have|know) (?:that|this|the exact|those|any)? ?(?:detail|info|information|number|figure)"
    r"|don'?t know|let me (?:check|confirm|find out)|i'?ll (?:check|confirm|find out)"
    r"|check (?:that |this |it )?with (?:the|our) (?:\w+ )?(?:team|desk|manager|staff)"
    r"|team eken (?:ahala|check)|danne naha|dannawa naha|කණ්ඩායමෙන් විමසා|තහවුරු කරගන්නම්|දන්නේ නැහැ",
    re.IGNORECASE,
)


def _sounds_unsure(text: str) -> bool:
    return bool(_UNSURE_RE.search(text or ""))


def _take_unknown_tag(text: str) -> Tuple[str, bool]:
    found = bool(_UNKNOWN_TAG_RE.search(text or ""))
    return (_UNKNOWN_TAG_RE.sub("", text or "").strip(), found)


async def generate_ai_response(
    system_prompt: str,
    knowledge_base: str,
    chat_history: List[Dict[str, str]],
    user_message: str,
    config=None,
    from_voice: bool = False,
) -> Tuple[str, bool]:
    """(reply text, hand-off requested). See generate_bot_reply, which also says whether the bot knew the answer."""
    reply = await generate_bot_reply(system_prompt, knowledge_base, chat_history, user_message, config, from_voice)
    return reply.text, reply.handoff


async def generate_bot_reply(
    system_prompt: str,
    knowledge_base: str,
    chat_history: List[Dict[str, str]],
    user_message: str,
    config=None,
    from_voice: bool = False,
) -> BotReply:
    """
    Generates AI response using OpenAI gpt-4o-mini, with automatic fallback
    to Smart Knowledge-Base Simulator if OpenAI key is unconfigured or credits exhausted.

    `config` is the tenant's BotConfig (see tenant_config.build_bot_config). The persona,
    handoff numbers and sendable documents are all read from it, so two tenants running on
    the same server never see each other's details.
    """
    if config is None:
        from tenant_config import BotConfig
        config = BotConfig(tenant_id=0, business_name="our business")

    # A business that hasn't uploaded anything yet has nothing true to say about itself. Measured:
    # with an empty knowledge base, a model told two customers "Yes, we have a pharmacy right
    # inside the clinic". So until there is knowledge, every real question goes to the team and
    # onto the owner's list — only greetings get a normal reply.
    if not _has_knowledge(knowledge_base) and not is_pure_greeting(user_message):
        lang = detect_language_intent(user_message, chat_history)
        has_contact = config.has_manager or config.has_front_office
        return BotReply(no_knowledge_reply(lang, config), handoff=has_contact, unknown=True)

    # No AI provider configured at all -> keyword simulator straight away.
    if not llm_router.role_candidates("chat"):
        reply = smart_knowledge_base_search(user_message, knowledge_base, system_prompt, chat_history, config)
        is_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply
        reply = reply.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
        return BotReply(reply, is_handoff)

    # Reply through the multi-provider router (Groq -> Gemini -> OpenRouter -> ...).
    try:
        greeting_en, greeting_si, greeting_sg = get_time_greeting()
        lang = detect_language_intent(user_message, chat_history)
        
        lang_instruction = (
            f"\n\n--- STRICT LANGUAGE CONTINUITY RULE ---\n"
            f"The customer's current detected language is '{lang.upper()}'.\n"
            f"You MUST reply in the SAME language the customer is using.\n"
            f"Maintain this language consistently for the entire conversation.\n"
            f"If the customer writes in a non-English language (Sinhala, Singlish, Hindi, Arabic, Spanish, French, "
            f"Portuguese, Malay, Thai, Tamil, etc.), reply fluently in THAT language.\n"
            f"If the customer speaks English, reply in English.\n"
            f"Language-specific guidelines:\n"
            f"- Singlish (romanized Sinhala): say 'api' (we/us), not 'apa'. Spell the way people type on WhatsApp "
            f"(mata, oyata, puluwan, thiyenawa) and leave everyday English words in English.\n"
            f"- For any language: use natural spoken phrasing, not formal literary style. Match how locals actually text.\n"
            f"- If the customer mixes two languages (code-switching), match their style naturally.\n"
            f"----------------------------------------\n"
        )
        # --- Handoff rules: the trigger list and the contact details are the CLIENT'S own,
        #     read from their tenant row. Nothing here is hardcoded to one business. ---
        rule_lines = "\n".join(f"- {rule}" for rule in config.handoff_rule_lines())
        contact_instructions = []
        manager_link = config.manager_link()
        if manager_link:
            contact_instructions.append(f"- Share the WhatsApp manager link: {manager_link}")
        if config.has_front_office:
            contact_instructions.append(f"- Share the contact number: {config.front_office_number}")
        if not contact_instructions:
            contact_instructions.append("- Tell them a team member will follow up shortly (no contact number is configured)")

        negotiation_handoff_rules = (
            "\n\n--- HANDOFF RULES ---\n"
            "If the customer asks about ANY of the following, you MUST hand off to a human:\n"
            f"{rule_lines}\n\n"
            "When handing off, be warm and make it feel like an upgrade:\n"
            + "\n".join(contact_instructions) + "\n"
            "- Append [HUMAN_HANDOFF_REQUESTED] at the very end of your reply\n"
            "- NEVER say 'I am unable to process' or 'Please contact a human representative'\n"
            "- Instead explain in your own words, worded differently each time, that the manager is the right "
            "person for this and can give them a real answer\n"
            "-------------------------------------------\n"
        )

        # --- Document rules: built from THIS tenant's uploaded, sendable documents. ---
        if config.documents:
            doc_lines = []
            for i, doc in enumerate(config.documents, start=1):
                triggers = ", ".join(doc.keywords) if doc.keywords else doc.title.lower()
                doc_lines.append(
                    f"{i}. If the customer asks (in English, Sinhala or Singlish) about: {triggers}\n"
                    f"   Append exactly: [SEND_DOC: {doc.url} | {doc.filename} | {doc.caption}]"
                )
            pdf_instructions = (
                "\n\n--- DOCUMENT SENDING RULES ---\n"
                + "\n".join(doc_lines)
                + "\nNever invent a document link. Only the links listed above may be sent.\n"
                "------------------------------------------\n"
            )
        else:
            pdf_instructions = (
                "\n\n--- DOCUMENT SENDING RULES ---\n"
                "This business has not uploaded any sendable documents. Never promise to send a PDF,\n"
                "brochure or price list, and never invent a link. Answer from the knowledge base instead.\n"
                "------------------------------------------\n"
            )
        smart_chat_rules = SMART_CHAT_RULES

        # Build persona prompt (warm and human, in the voice of this tenant's industry)
        persona_prompt = build_persona_prompt(config)

        # What customers of this kind of business need, and what it must never say.
        industry_playbook = playbook_prompt(config.pack)

        # The owner's own playbook (example chats, sales approach), when they uploaded one.
        # Its prices were blanked to "[amount]" in training: style comes from here, facts never do.
        owner_playbook = ""
        if config.playbook:
            from playbook_pipeline import MAX_PLAYBOOK_CHARS
            owner_playbook = (
                "--- THIS BUSINESS'S CONVERSATION PLAYBOOK (how the owner wants chats to go) ---\n"
                "Follow these goals, style and sales approach - they come from the owner and take priority over the "
                "general persona examples. The example conversations show HOW to talk, never WHAT is true: every "
                "price, date, product and policy must come from the knowledge base, and \"[amount]\" marks a figure "
                "you must look up there or say you'll confirm. Never copy an example reply word for word.\n"
                f"{config.playbook[:MAX_PLAYBOOK_CHARS]}\n"
                "------------------------------------------\n\n"
            )

        # Only the knowledge-base sections this message needs (see kb_retrieval.py).
        kb_context, kb_info = await select_context(knowledge_base, user_message, chat_history)
        logger.info(f"[chat] knowledge base: {kb_info.get('mode')} {kb_info.get('chars')}"
                    f"/{kb_info.get('of', kb_info.get('chars'))} chars {kb_info.get('sections', '')}")

        # Who this is and where the conversation stands (conversation_memory.py), so the reply
        # continues the chat instead of starting over.
        conversation_ctx = getattr(config, "conversation", None)
        conversation_notes = conversation_memory.context_prompt(conversation_ctx)

        # Totals: when the customer asks "how much altogether?" the CODE adds up the live prices
        # (order_math.py); the model only reads the verified result out.
        quote_block = ""
        quote_text = ""
        if getattr(config, "catalog", None):
            try:
                quote = await order_math.build_quote(chat_history, user_message, config.catalog, config.currency)
            except Exception as exc:  # noqa: BLE001 - a failed calculation must never stop the reply
                logger.warning(f"[chat] order total not calculated: {exc}")
                quote = None
            if quote:
                quote_block = order_math.quote_prompt(quote)
                quote_text = order_math.quote_text(quote)
                logger.info(f"[chat] verified total: {quote_text.splitlines()[-1]}")

        full_system_instructions = (
            f"{persona_prompt}\n\n"
            f"{system_prompt}\n\n"
            f"{industry_playbook}\n\n"
            f"{owner_playbook}"
            f"{conversation_notes}\n"
            f"{quote_block}"
            f"{conversation_memory.CONTINUITY_RULES}\n"
            f"--- KNOWLEDGE BASE (the parts relevant to this message) ---\n"
            f"{kb_context}\n"
            f"----------------------\n"
            f"{lang_instruction}\n"
            f"{negotiation_handoff_rules}\n"
            f"{smart_chat_rules}\n"
            f"{GENERAL_KNOWLEDGE_RULES}\n"
            f"{pdf_instructions}\n"
            f"{time_context_rule()}\n"
            f"{HUMAN_STYLE_RULES}\n"
            f"{REAL_WORLD_RULES}\n"
            f"{VOICE_NOTE_RULES if from_voice else ''}"
            f"{REASONING_RULES}"
        )
        messages = [{"role": "system", "content": full_system_instructions}]
        # More of the chat than before (16 messages, was 10), so earlier details stay in view; the
        # conversation notes cover anything older.
        for msg in chat_history[-16:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        model_label, reply_text = await _complete_reply(messages, temperature=0.5)
        reply_content, is_unknown = _take_unknown_tag(whatsapp_format(reply_text))

        # Number guard: every price, count, time or date the bot states must exist in the
        # knowledge base, the owner's own settings, or the conversation. Models occasionally
        # invent a figure when the fact is missing (measured: "parking for around 120 cars"
        # when the document only says "adequate parking"). A wrong number on a quote is a
        # promise the business can't keep, so: redraft once, then refuse to guess.
        # Numbers the customer gave earlier (kept in the conversation notes after old messages are
        # cleaned up) may be repeated back to them: "for your 120 guests".
        brief_details = " ".join(str(v) for v in ((getattr(conversation_ctx, "brief", None) or {}).get("details") or {}).values())
        time_rule = time_context_rule()
        # Common general knowledge numbers that any assistant may mention:
        # Standard calendar, sizes (24-48), hours/minutes, standard percentages, common years
        common_general_numbers = "10 12 14 15 16 18 20 22 24 25 26 28 30 32 34 36 38 40 42 44 46 48 50 60 100 2024 2025 2026 2027 2028 24/7"
        allowed_text = "\n".join([knowledge_base or "", system_prompt or "", config.handoff_rules or "",
                                  user_message, brief_details, time_rule, quote_text, common_general_numbers] + [m.get("content", "") for m in chat_history])
        invented = unsupported_reply_numbers(reply_content, allowed_text, config)
        if invented:
            logger.warning(f"[chat] {model_label} stated numbers not in the knowledge base {invented}; redrafting")
            strict = list(messages)
            strict[0] = {"role": "system", "content": messages[0]["content"] + (
                "\n\n--- CORRECTION ---\n"
                f"Your previous draft stated these numbers, which are NOT in the knowledge base: {', '.join(invented)}.\n"
                "Write the reply again. Do not state ANY number, price, quantity, capacity, time or date "
                "unless it is written in the knowledge base above. Never calculate totals. If the customer "
                "needs a figure the knowledge base doesn't have, say you'll confirm it with the team.")}
            try:
                _, retry_text = await _complete_reply(strict, temperature=0.2)
                reply_content, retry_unknown = _take_unknown_tag(whatsapp_format(retry_text))
                is_unknown = is_unknown or retry_unknown
                invented = unsupported_reply_numbers(reply_content, allowed_text, config)
            except llm_router.LLMUnavailable:
                pass
            if invented:
                logger.warning(f"[chat] redraft still had unsupported numbers {invented}; sending a safe reply")
                # The figure they asked for isn't in the knowledge base: that is a gap to fill too.
                return BotReply(safe_unknown_reply(lang, config), handoff=True, unknown=True)
        is_human_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply_content
        reply_content = reply_content.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()

        # Language guard: a customer who wrote Sinhala script or Singlish gets the same back
        # (measured: Sinhala script was answered in romanised Singlish, Singlish in English).
        wrong, got = reply_guards.wrong_language(lang, reply_content, user_message)
        if wrong:
            logger.warning(f"[chat] customer wrote {lang}, reply was {got}; redrafting in {lang}")
            how = ("in Sinhala script (අකුරු), never in English letters" if lang == "sinhala"
                   else "in Singlish: romanised Sinhala with the odd English word, not plain English")
            fixed = list(messages)
            fixed[0] = {"role": "system", "content": messages[0]["content"] + (
                "\n\n--- CORRECTION ---\n"
                f"Your previous draft was in the wrong language. The customer wrote {lang}. Write the reply again {how}. "
                "Keep every fact and figure exactly as before.")}
            try:
                _, redo_text = await _complete_reply(fixed, temperature=0.4)
                redo, redo_unknown = _take_unknown_tag(whatsapp_format(redo_text))
                still_wrong, _ = reply_guards.wrong_language(lang, redo, user_message)
                if not still_wrong and not unsupported_reply_numbers(redo, allowed_text, config):
                    is_human_handoff = is_human_handoff or "[HUMAN_HANDOFF_REQUESTED]" in redo
                    reply_content = redo.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
                    is_unknown = is_unknown or redo_unknown
            except llm_router.LLMUnavailable:
                pass

        # Things a person must see whatever the model wrote, and promises nobody would keep:
        # alert the team (the chat is flagged "Needs you" and the manager is messaged).
        must_see = reply_guards.needs_person(user_message, getattr(config, "industry", "generic"))
        promised = reply_guards.promises_followup(reply_content)
        reply_content = reply_guards.one_question(reply_content)
        forced = (must_see or promised) and not is_human_handoff
        if forced:
            logger.info(f"[chat] handing over to a person: {must_see or 'the reply promised a follow-up'}")
            is_human_handoff = True
            # The model didn't hand over itself, so its reply may still be asking the customer
            # something or offering to check: say what it knows, then that a person will follow up.
            trimmed = reply_guards.without_questions(reply_content)
            reply_content = trimmed or reply_content
        if is_human_handoff:
            note = reply_guards.handoff_note(lang, reply_content)
            if note:
                reply_content = f"{reply_content}\n\n{note}".strip()
        if not is_unknown and not is_human_handoff and _sounds_unsure(reply_content):
            is_unknown = True
        # Update the conversation notes in the background (never delays this reply).
        conversation_memory.refresh_later(conversation_ctx, config.business_name)
        return BotReply(reply_content, is_human_handoff, is_unknown)

    except Exception as e:
        logger.warning(f"All AI providers failed ({e}). Using Smart Knowledge-Base Simulator fallback.")
        reply = smart_knowledge_base_search(user_message, knowledge_base, system_prompt, chat_history, config)
        is_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply
        reply = reply.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
        return BotReply(reply, is_handoff)


async def classify_product_from_image(image_bytes: bytes, products: list) -> dict:
    """
    Identify which catalog product a customer's photo shows, using the vision models in
    llm_router (Gemini first). Returns {"matched": bool, "product_name": str|None, "confidence": str}.

    The model may only answer with a name from the catalog; anything else counts as no match,
    so a confident-sounding invented product can never reach the cart.
    """
    if not products or not image_bytes or not llm_router.role_candidates("vision"):
        return {"matched": False, "product_name": None, "confidence": "none"}

    names = [p.name for p in products]
    product_list_str = "\n".join(f"- {name}" for name in names)
    prompt = f"""You are a product identification assistant for an online store.
Look at this image and identify which product it shows.

Available products in our catalog:
{product_list_str}

Respond with ONLY a JSON object:
{{"matched_product": "<exact product name from the list above, or null>", "confidence": "high" | "medium" | "low"}}

Use null if the image doesn't clearly show one of these products."""

    try:
        result, by = await llm_router.complete_json(
            "vision", llm_router.image_message(prompt, image_bytes), max_tokens=400, temperature=0)
    except llm_router.LLMUnavailable as e:
        logger.error(f"classify_product_from_image unavailable: {e}")
        return {"matched": False, "product_name": None, "confidence": "none"}

    answer = (result.get("matched_product") or "").strip() if isinstance(result, dict) else ""
    # Accept only real catalog names (case-insensitive), never an invented one.
    exact = next((n for n in names if n.lower() == answer.lower()), None)
    logger.info(f"[vision] product photo -> {exact!r} by {by.label}")
    return {
        "matched": exact is not None,
        "product_name": exact,
        "confidence": result.get("confidence", "low") if exact else "none",
    }
