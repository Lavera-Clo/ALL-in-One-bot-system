import base64
import json
import logging
import re
from typing import List, Dict, Tuple, Optional
from openai import AsyncOpenAI
from config import settings
from prompts.persona_system_prompt import build_persona_prompt
import llm_router
from kb_retrieval import select_context

logger = logging.getLogger("uvicorn.error")

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
        "The user is a customer in Sri Lanka asking a local business about products, prices and bookings. "
        "They might speak in English, Sinhala, or Singlish (Romanized Sinhala). "
        "Examples of Singlish: 'package eke ganan kohomada', 'wedding ekata keeyada', 'menu eka ewanna'."
    )
    try:
        return await llm_router.transcribe_audio(audio_bytes, "voice_note.ogg", whisper_prompt)
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return ""

def get_sri_lanka_greeting() -> Tuple[str, str, str]:
    """
    Returns time-aware Sri Lanka greeting tuple (English, Sinhala, Singlish)
    Calculated based on Sri Lanka Time (UTC +5:30).
    """
    try:
        from datetime import datetime, timezone, timedelta
        sl_tz = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(sl_tz)
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

    # 2. Singlish / Romanized Sinhala Check (Comprehensive vocabulary list)
    singlish_keywords = [
        "ayubowan", "ayubowam", "subha", "udasanak", "dahawalak", "sandhawak", 
        "kohomada", "gaana", "gana", "kiyada", "ekak", "apita", "hari", "ewanna", 
        "wistara", "wistarathama", "ganan", "pahan", "monawada", "puluwanda", "puluwan",
        "denna", "kiyanna", "balanna", "neda", "ane", "bro", "macho", "sthuthi", 
        "bohowama", "oya", "oyata", "oyalage", "mata", "wena", "thawa",
        "diyan", "elama", "obata", "obatai", "gollan", "hitanne", "kello", "kollo", "wedding"
    ]
    
    # Check current message for Singlish keywords
    if any(re.search(rf"\b{re.escape(w)}\b", msg) or w in msg for w in singlish_keywords):
        return "singlish"

    # 3. Check Chat History to Maintain Conversation Language Continuity
    if chat_history:
        for past_msg in reversed(chat_history[-6:]):
            past_text = past_msg.get("content", "").lower()
            if any('\u0d80' <= c <= '\u0dff' for c in past_text):
                return "sinhala"
            if any(w in past_text for w in singlish_keywords):
                return "singlish"

    return "english"


def is_gibberish(text: str) -> bool:
    """
    Detects if a message is gibberish / random keystrokes / spam.
    Returns True if the message appears to be meaningless.
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

    # Check 3: No recognizable words at all — check against common words
    common_words = {
        "hi", "hello", "hey", "yes", "no", "ok", "okay", "thanks", "thank", "please",
        "price", "cost", "menu", "hall", "wedding", "event", "book", "date", "pax",
        "how", "what", "when", "where", "who", "why", "can", "will", "want", "need",
        "good", "morning", "afternoon", "evening", "night", "sir", "madam", "bro",
        "ayubowan", "kohomada", "ewanna", "denna", "kiyanna", "puluwan", "hari",
        "oya", "mata", "ganan", "mila", "sthuthi", "start", "help", "contact",
        "manager", "call", "number", "phone", "send", "pdf", "brochure", "detail",
        "silver", "gold", "premier", "grand", "starlight", "ballroom", "view",
        "discount", "negotiate", "cheap", "register", "registration", "package",
    }
    words_in_msg = set(re.findall(r'[a-zA-Z]+', msg.lower()))
    if words_in_msg and not words_in_msg.intersection(common_words):
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


def get_returning_customer_context(chat_history: List[Dict[str, str]]) -> Tuple[bool, str]:
    """
    Checks if this is a returning customer (has significant past history).
    Returns (is_returning, last_topic_summary).
    """
    if not chat_history or len(chat_history) < 4:
        return False, ""

    # Extract the last few user messages to figure out what they were discussing
    topic_keywords = {
        "price": "prices & packages", "cost": "prices & packages", "ganan": "prices & packages",
        "rate": "prices & packages", "budget": "prices & packages", "pax": "prices & packages",
        "menu": "menu options", "food": "menu options", "kema": "menu options",
        "silver": "the Silver package", "gold": "the Gold package", "premier": "the Premier package",
        "hall": "venue options", "ballroom": "venue options", "grand": "the Grand Ballroom",
        "starlight": "the Starlight Ballroom", "venue": "venue options",
        "wedding": "your wedding plans", "event": "your event",
        "register": "registration packages", "regi": "registration packages",
        "date": "dates & availability", "book": "booking",
        "brochure": "our brochures", "pdf": "our brochures",
    }

    last_topic = ""
    # Scan recent user messages in reverse to find the last meaningful topic
    for entry in reversed(chat_history):
        if entry.get("role") != "user":
            continue
        content = entry.get("content", "").lower()
        for keyword, topic in topic_keywords.items():
            if keyword in content:
                last_topic = topic
                break
        if last_topic:
            break

    if not last_topic:
        last_topic = "your event plans"

    return True, last_topic


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
    is_front_office_query = any(w in msg for w in ["reception", "front office", "phone", "call", "contact", "number", "fo", "දුරකථන", "අංකය", "කෝල්", "hotline"])
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
    greeting_en, greeting_si, greeting_sg = get_sri_lanka_greeting()

    # Pure Greeting Check
    greetings_list = ["hi", "hello", "hey", "ayubowan", "good morning", "good afternoon", "good evening", "greetings", "start", "ආයුබෝවන්"]
    is_pure_greeting = msg in greetings_list or (any(g in msg for g in greetings_list) and len(msg.split()) <= 2)

    # Check if this is the first message in the conversation session
    is_first_message = not chat_history or len(chat_history) == 0

    # -----------------------------------------------------------
    # GIBBERISH / SPAM DETECTION (runs before everything else)
    # -----------------------------------------------------------
    if not is_pure_greeting and not is_front_office_query and not is_customization_query:
        if is_gibberish(user_message):
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
        is_returning, last_topic = get_returning_customer_context(chat_history)
        if is_returning:
            if is_sinhala:
                return (
                    f"👋 *ආයුබෝවන්! {greeting_si}!* ආපහු සාදරයෙන් පිළිගනිමු 🌺\n\n"
                    f"පසුගිය වතාවේ අපි _{last_topic}_ ගැන කතා කරමින් හිටියා.\n"
                    f"අද මොකද බලාපොරොත්තු වෙන්නේ?"
                )
            elif is_singlish:
                return (
                    f"👋 *Ayubowan! {greeting_sg}!* Aapu welcome back 🌺\n\n"
                    f"Kalin apa _{last_topic}_ gena katha karanawa hitiya.\n"
                    f"Ada mokada balaporoththu wenne?"
                )
            else:
                return (
                    f"👋 *{greeting_en}!* Welcome back 🌺\n\n"
                    f"Last time we were chatting about _{last_topic}_.\n"
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

    # Match topic keywords
    matched_lines = []
    if any(w in msg for w in ["price", "prices", "rate", "cost", "pax", "silver", "gold", "premier", "ballroom", "starlight", "ganan", "kiyada"]):
        for line in kb_lines:
            if any(k in line.lower() for k in ["pax", "silver", "gold", "premier", "ballroom", "starlight", "plate", "per head", "rs.", "$"]):
                matched_lines.append(f"• {line}")

    if any(w in msg for w in ["menu", "items", "compliment", "policy", "deposit", "corkage", "music", "detail"]):
        for line in kb_lines:
            if any(k in line.lower() for k in ["menu", "extra", "deposit", "corkage", "music", "policy", "brochure"]):
                matched_lines.append(f"• {line}")

    # Response with language consistency & session awareness (Greetings ONLY on first message)
    if matched_lines:
        details = "\n".join(matched_lines[:6])
        if is_sinhala:
            header = f"✨ *{greeting_si}! ඔබ ඉල්ලා සිටි පැකේජ විස්තර මෙන්න:*\n\n" if is_first_message else "✨ *ඔබ ඉල්ලා සිටි පැකේජ විස්තර මෙන්න:*\n\n"
            return f"{header}{details}\n\nමම ඔබ වෙනුවෙන් නිල PDF ලේඛනය පහතින් ලබා දී ඇත! 👇"
        elif is_singlish:
            header = f"✨ *{greeting_sg}! Oyala illa hitapu package details menna:*\n\n" if is_first_message else "✨ *Oyala illa hitapu package details menna:*\n\n"
            return f"{header}{details}\n\nMan official PDF brochure eka pahalin attach kara! 👇"
        else:
            header = f"✨ *{greeting_en}! Thank you for contacting {config.business_name}. Here are the requested details:*\n\n" if is_first_message else "✨ *Here are the requested details:*\n\n"
            return f"{header}{details}\n\nI have attached our official PDF brochure for you below! 👇"
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
            if digits.startswith("94"):
                phones.add("0" + digits[2:])       # local Sri Lankan form, e.g. 0342275000

    check = _URL_RE.sub(" ", reply or "")
    check = re.sub(r"[  -​  　]", " ", check)
    check = _PHONE_LIKE_RE.sub(lambda m: " " if re.sub(r"\D", "", m.group(0)) in phones else m.group(0), check)
    allowed = set(numbers_in(allowed_text)) | phones
    return sorted({n for n in numbers_in(check) if n not in allowed})


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


async def generate_ai_response(
    system_prompt: str,
    knowledge_base: str,
    chat_history: List[Dict[str, str]],
    user_message: str,
    config=None
) -> Tuple[str, bool]:
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

    # No AI provider configured at all -> keyword simulator straight away.
    if not llm_router.role_candidates("chat"):
        reply = smart_knowledge_base_search(user_message, knowledge_base, system_prompt, chat_history, config)
        is_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply
        reply = reply.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
        return reply, is_handoff

    # Reply through the multi-provider router (Groq -> Gemini -> OpenRouter -> ...).
    try:
        greeting_en, greeting_si, greeting_sg = get_sri_lanka_greeting()
        lang = detect_language_intent(user_message, chat_history)
        
        lang_instruction = (
            f"\n\n--- STRICT LANGUAGE CONTINUITY RULE ---\n"
            f"The customer's current detected language is '{lang.upper()}'.\n"
            f"You MUST reply 100% in {lang.upper()} (Singlish / Sinhala / English).\n"
            f"Maintain this language consistently for the entire rest of the conversation.\n"
            f"If the customer speaks Singlish (romanized Sinhala like 'ayubowan', 'kohomada', 'ewanna'), reply ONLY in Singlish.\n"
            f"If the customer speaks Sinhala script, reply ONLY in Sinhala.\n"
            f"If the customer speaks English, reply ONLY in English.\n"
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
            "- Instead say something like 'That's above my pay grade — our manager can sort that out for you'\n"
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
        smart_chat_rules = (
            "\n\n--- SMART CHAT & MEMORY RULES ---\n"
            "1. GIBBERISH DETECTION: If the customer sends random characters (e.g., 'asdfg', 'ffsdsff'), do NOT try to answer. Politely say: 'Sorry, I didn't quite catch that 😅 Could you rephrase what you need?'\n"
            "2. REPEATED QUESTIONS: If the customer asks the exact same question they already asked earlier in the chat history, do NOT just dump the same answer again. Acknowledge it by saying 'You actually asked about this earlier — here's what I shared:' and summarize the past answer briefly, asking if they need more specific details.\n"
            "3. RETURNING CUSTOMERS: If the customer says 'Hi' and there are previous messages in the chat history, greet them as a returning customer ('Welcome back! Last time we were chatting about [Topic]. How can I help today?').\n"
            "4. UNKNOWN ANSWERS: If the customer asks a clear question but the answer is NOT in the knowledge base, say plainly that you don't have that detail and offer to check with the team. Never guess, estimate or imply an answer (capacity, availability, timing, price) — 'it might be a squeeze' is a guess too. Only ask them to rephrase if their message was genuinely unclear.\n"
            "------------------------------------------\n"
        )

        # Build persona prompt (the warm, human-like "Nethmi" persona)
        persona_prompt = build_persona_prompt(config)

        # Only the knowledge-base sections this message needs (see kb_retrieval.py).
        kb_context, kb_info = await select_context(knowledge_base, user_message, chat_history)
        logger.info(f"[chat] knowledge base: {kb_info.get('mode')} {kb_info.get('chars')}"
                    f"/{kb_info.get('of', kb_info.get('chars'))} chars {kb_info.get('sections', '')}")

        full_system_instructions = (
            f"{persona_prompt}\n\n"
            f"{system_prompt}\n\n"
            f"--- KNOWLEDGE BASE (the parts relevant to this message) ---\n"
            f"{kb_context}\n"
            f"----------------------\n"
            f"{lang_instruction}\n"
            f"{negotiation_handoff_rules}\n"
            f"{smart_chat_rules}\n"
            f"{pdf_instructions}"
        )
        messages = [{"role": "system", "content": full_system_instructions}]
        for msg in chat_history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        result = await llm_router.complete("chat", messages, max_tokens=1500, temperature=0.7, timeout=45)
        logger.info(f"[chat] reply by {result.label} in {result.latency:.1f}s")
        reply_content = whatsapp_format(result.text.strip())

        # Number guard: every price, count, time or date the bot states must exist in the
        # knowledge base, the owner's own settings, or the conversation. Models occasionally
        # invent a figure when the fact is missing (measured: "parking for around 120 cars"
        # when the document only says "adequate parking"). A wrong number on a quote is a
        # promise the business can't keep, so: redraft once, then refuse to guess.
        allowed_text = "\n".join([knowledge_base or "", system_prompt or "", config.handoff_rules or "",
                                  user_message] + [m.get("content", "") for m in chat_history])
        invented = unsupported_reply_numbers(reply_content, allowed_text, config)
        if invented:
            logger.warning(f"[chat] {result.label} stated numbers not in the knowledge base {invented}; redrafting")
            strict = list(messages)
            strict[0] = {"role": "system", "content": messages[0]["content"] + (
                "\n\n--- CORRECTION ---\n"
                f"Your previous draft stated these numbers, which are NOT in the knowledge base: {', '.join(invented)}.\n"
                "Write the reply again. Do not state ANY number, price, quantity, capacity, time or date "
                "unless it is written in the knowledge base above. Never calculate totals. If the customer "
                "needs a figure the knowledge base doesn't have, say you'll confirm it with the team.")}
            try:
                retry = await llm_router.complete("chat", strict, max_tokens=1500, temperature=0.2, timeout=45)
                reply_content = whatsapp_format(retry.text.strip())
                invented = unsupported_reply_numbers(reply_content, allowed_text, config)
            except llm_router.LLMUnavailable:
                pass
            if invented:
                logger.warning(f"[chat] redraft still had unsupported numbers {invented}; sending a safe reply")
                return safe_unknown_reply(lang, config), True
        is_human_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply_content
        reply_content = reply_content.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
        return reply_content, is_human_handoff

    except Exception as e:
        logger.warning(f"All AI providers failed ({e}). Using Smart Knowledge-Base Simulator fallback.")
        reply = smart_knowledge_base_search(user_message, knowledge_base, system_prompt, chat_history, config)
        is_handoff = "[HUMAN_HANDOFF_REQUESTED]" in reply
        reply = reply.replace("[HUMAN_HANDOFF_REQUESTED]", "").strip()
        return reply, is_handoff


async def classify_product_from_image(image_bytes: bytes, products: list) -> dict:
    """
    Uses GPT-4o Vision to identify which product from the catalog is shown in an image.
    Returns {"matched": bool, "product_name": str|None, "confidence": str}
    Falls back to {"matched": False} if no API key is configured.
    """
    api_key = settings.OPENAI_API_KEY.strip() if settings.OPENAI_API_KEY else ""
    if not api_key or "placeholder" in api_key or api_key == "your_openai_api_key_here":
        return {"matched": False, "product_name": None, "confidence": "none"}

    if not products:
        return {"matched": False, "product_name": None, "confidence": "none"}

    product_list_str = "\n".join(f"- {p.name}" for p in products)

    prompt = f"""You are a product identification assistant for an online store.
Look at this image and identify which product it shows.

Available products in our catalog:
{product_list_str}

Respond with ONLY a JSON object (no markdown):
{{
    "matched_product": "<exact product name from the list above, or null if no match>",
    "confidence": "high" | "medium" | "low"
}}

Rules:
- matched_product must be an exact name from the list, or null
- Use null if the image doesn't clearly match any product
"""

    try:
        client = AsyncOpenAI(api_key=api_key)
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "low"
                    }}
                ]
            }],
            max_tokens=150,
            temperature=0.1
        )

        reply = response.choices[0].message.content.strip()
        if reply.startswith("```"):
            lines = reply.split("\n")
            reply = "\n".join(lines[1:-1]) if len(lines) > 2 else reply.strip("`")

        result = json.loads(reply)
        matched_name = result.get("matched_product")
        return {
            "matched": matched_name is not None,
            "product_name": matched_name,
            "confidence": result.get("confidence", "low")
        }

    except Exception as e:
        logger.error(f"classify_product_from_image error: {e}")
        return {"matched": False, "product_name": None, "confidence": "none"}
