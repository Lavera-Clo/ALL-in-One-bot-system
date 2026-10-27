"""
Safety nets applied to every reply AFTER the model has written it - found by playing real,
messy customer conversations through the bot (realworld_eval.py). The prompt asks for the right
behaviour; these make sure the important parts happen even when a model forgets.

  needs_person()       the message is one a human must see, whatever the model answered: a
                       complaint or refund, a missing order, haggling, an allergy, a request for
                       medical advice. The chat is flagged and the manager alerted.
  promises_followup()  the reply says "I'll check / confirm / get back to you". Nobody does that
                       unless a person is alerted, so the chat is handed over.
  one_question()       the reply ends with ONE question; extra questions stacked in a message
                       make people answer the wrong one.
  wrong_language()     the customer wrote Sinhala script or Singlish and the reply is not.
  handoff_note()       a short line, in the customer's language, saying a person will follow up -
                       added only when the reply doesn't already say so.

Everything is plain text matching: fast, free and predictable.
"""

import re
from typing import Optional, Tuple

_SINHALA = re.compile(r"[඀-෿]")

# --- Messages a person must see ------------------------------------------------------------

_COMPLAINT = re.compile(
    r"\b(refund|money back|want my money|damag(ed|e)|broken|spoil(ed|t)|rotten|wrong (item|order|product)|"
    r"not (yet )?(received|delivered|come|arrived)|(has|have|hasn'?t|haven'?t) not (come|arrived|reached)|"
    r"never (came|arrived)|where is my (order|parcel|package|delivery)|late delivery|"
    r"complain(t)?|unacceptable|disgusting|worst|scam|cheat(ed|ing)?|fraud|useless|a joke|terrible service|"
    r"legal action|consumer (affairs|authority)|sue )\b"
    r"|salli (ona|denna)|kudu karanna|paraku|badu (kharab|naha)|parana badu|"
    r"මුදල් (ආපසු|ඕන)|ආපසු දෙන්න|කැඩිලා|නරක් වෙලා|ගෙන්නලා නැහැ|තවම ආවේ නැහැ",
    re.I,
)
_HAGGLE = re.compile(
    r"\b(discount|cheaper|cheap(er)? price|reduce (the )?price|lower (the )?price|less price|best price|"
    r"last price|final price|bargain|negotiat(e|ion)|can you do (rs\.?|lkr)?\s?\d|too (expensive|much|costly)|"
    r"(my )?friend (got|paid)|offer me|special price|wholesale price|bulk (price|discount))\b"
    r"|adu karanna|aduwata|adu gane|kam karanna|wadi wei|discount ekak|මිල අඩු|අඩු කරන්න|වට්ටමක්",
    re.I,
)
_ALLERGY = re.compile(r"\b(allerg(y|ic|ies)|anaphyla|intoleran(t|ce)|celiac|coeliac|gluten[- ]free|nut[- ]free|peanut)\b"
                      r"|allergy ekak|අසාත්මික", re.I)
_MEDICAL = re.compile(
    r"\b(what (medicine|tablet|drug|pill|dose|antibiotic)|which (medicine|tablet|drug|pill|antibiotic)|"
    r"how (many|much) (mg|tablets?|pills?)|should i take|can i take|is it safe (for|to) (take|use|eat|drink)|"
    r"diagnos(e|is)|prescri(be|ption)|side effects?|overdos(e|ed)|"
    r"chest pain|can'?t breathe|cannot breathe|unconscious|heavy bleeding|bleeding (a lot|heavily)|"
    r"suicid(e|al)|want to die)\b"
    r"|බෙහෙත් (මොනවද|ගන්නද)|ඖෂධ",
    re.I,
)
_MEDICAL_INDUSTRIES = {"clinic", "fitness", "restaurant", "generic"}


def needs_person(message: str, industry: str = "generic") -> Optional[str]:
    """Why a human must see this message, or None. The reason is only for the logs."""
    text = message or ""
    if _COMPLAINT.search(text):
        return "complaint or missing order"
    if _HAGGLE.search(text):
        return "price negotiation"
    if _ALLERGY.search(text):
        return "allergy or dietary safety"
    if industry in ("clinic", "fitness") and _MEDICAL.search(text):
        return "medical advice request"
    return None


# --- A reply that promises someone will do something ----------------------------------------

_PROMISE = re.compile(
    r"\b(i('| wi)ll|let me|i('m| am) going to|we('| wi)ll|our team (will|can)|the team (will|can)|someone (will|from)|"
    r"a (member|person|colleague|manager))\b[^.!?\n]{0,60}\b("
    r"check|confirm|verify|look into|find out|get back|follow up|call you|contact you|reach out|"
    r"arrange|reserve|hold|book (it|that|this|you)|put together|prepare|sort (it|this|that) out|pass (it|this) (on|to))\b"
    r"|\b(get back to you|follow up with you|be in touch|check (that|this|it) (for you|with))\b"
    r"|balala kiyannam|ahala balala|passe kiyannam|contact karai|katha karai|"
    r"මම බලලා|බලලා කියන්නම්|ඔබව අමතයි|සම්බන්ධ වෙයි",
    re.I,
)
# Phrases that only OFFER the follow-up ("shall I check with the team?") do not count.
_OFFER = re.compile(r"(shall i|should i|would you like me to|want me to|do you want me to|may i|can i)\b[^.!?\n]{0,50}\?", re.I)


def promises_followup(reply: str) -> bool:
    text = reply or ""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not sentence.strip():
            continue
        if _PROMISE.search(sentence) and not _OFFER.search(sentence):
            return True
    return False


# --- One question per message --------------------------------------------------------------

def one_question(reply: str) -> str:
    """Keep only the FIRST question in a reply and drop the questions stacked after it
    ("Dine-in or delivery? Which do you prefer?" -> "Dine-in or delivery?"). Statements stay."""
    text = (reply or "").strip()
    if text.count("?") < 2:
        return text
    parts = re.split(r"(?<=[.!?])(\s+)", text)       # sentence, separator, sentence, ...
    out, seen, drop_sep = [], False, False
    for i, part in enumerate(parts):
        if i % 2 == 1:                               # a separator between sentences
            if not drop_sep:
                out.append(part)
            drop_sep = False
            continue
        if part.rstrip().endswith("?"):
            if seen:
                drop_sep = True                      # drop this question and the gap before it
                if out and out[-1].isspace():
                    out.pop()
                continue
            seen = True
        out.append(part)
    cleaned = "".join(out)
    cleaned = re.sub(r"([?!])(?=[A-Za-z])", r"\1 ", cleaned)      # a dropped question can leave "?Thanks"
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or text


def without_questions(reply: str) -> str:
    """The reply with its question sentences removed (statements stay). Used when we hand over to
    a person: "Want me to check a slot for you?" makes no sense next to "a person will follow up"."""
    text = (reply or "").strip()
    if "?" not in text:
        return text
    parts = re.split(r"(?<=[.!?])(\s+)", text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
        elif not part.rstrip().endswith("?"):
            out.append(part)
        elif out and out[-1].isspace():
            out.pop()                                   # also drop the gap before the removed question
    cleaned = re.sub(r"[ \t]+\n", "\n", "".join(out))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# --- Language ----------------------------------------------------------------------------

_SMALL_TALK = {"hi", "hello", "hey", "ok", "okay", "thanks", "thank you", "yes", "no", "menu", "price", "price?"}


def wrong_language(customer_lang: str, reply: str, message: str = "") -> Tuple[bool, str]:
    """(is it wrong, what the reply was). Only judged for real replies to real sentences."""
    words = re.findall(r"[A-Za-z඀-෿]{2,}", reply or "")
    if len(words) < 6 or (message or "").strip().lower() in _SMALL_TALK:
        return False, ""
    if customer_lang == "sinhala":
        # Mostly Sinhala script, not one Sinhala word in a romanised sentence.
        letters = [c for c in (reply or "") if c.isalpha()]
        share = sum(1 for c in letters if _SINHALA.match(c)) / max(len(letters), 1)
        return (share < 0.35), ("mostly romanised or english" if share < 0.35 else "sinhala")
    if customer_lang == "singlish":
        if _SINHALA.search(reply or ""):
            return True, "sinhala script"
        from self_test_service import reply_language
        got = reply_language(reply or "")
        return got == "english", got
    return False, ""


# --- A line telling them a person will follow up ----------------------------------------

_MENTIONS_PERSON = re.compile(
    r"\b(team|manager|colleague|someone|staff|person|human|owner|front office|supervisor|specialist)\b"
    r"|කණ්ඩායම|කෙනෙක්|කළමනාකරු|apage (team|manager)|kenek|manager", re.I)

_NOTES = {
    "english": "I've passed this to our team so a person can look at it properly - they'll follow up with you shortly.",
    "singlish": "Mama meka apage team ekata yawwa - kenek ikmanin oyata contact karai.",
    "sinhala": "මම මේක අපේ කණ්ඩායමට යොමු කළා - ඉක්මනින්ම කෙනෙක් ඔබට සම්බන්ධ වෙයි.",
}


def handoff_note(lang: str, reply: str) -> str:
    """The note to add, or '' when the reply already mentions a person or the team."""
    if _MENTIONS_PERSON.search(reply or ""):
        return ""
    return _NOTES.get(lang, _NOTES["english"])
