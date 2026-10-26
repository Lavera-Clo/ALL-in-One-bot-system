"""
Understanding WhatsApp voice notes.

Two listeners, used together:

  1. Whisper (Groq, OpenAI as backup) - fast and cheap, and good at English. It reports the
     language it heard and how sure it was.
  2. Gemini, which understands audio directly - far better at Sinhala, Tamil and Singlish
     (Sinhala mixed with English), and gives the meaning in English as well.

Whisper hears every note first. When it is sure and heard English, that's the answer. When it
heard another language, wasn't sure, or produced one of its well-known inventions on a silent or
noisy clip ("Thank you for watching"), Gemini listens again. With only one of the two configured,
that one is used alone.

Both are told the business's own words - its name and its product names - so "Panadol" or
"Grand Ballroom" come out spelled the way the business spells them.

The result says what was said (`text`), in which language, what it means in English, and how
sure the listener was; or that the note was silent, or that nobody could listen to it. main.py
answers each case, and the inbox shows the words even when the bot is switched off for the chat.
"""

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import llm_router
from ai_service import detect_language_intent
from models import ChatHistory, Product, Tenant
from whatsapp_client import download_whatsapp_media

logger = logging.getLogger(__name__)

# Gemini takes audio inline up to 20 MB; WhatsApp voice notes are far smaller (16 MB max).
MAX_INLINE_AUDIO = 19 * 1024 * 1024
# Whisper's own judgement, below which its words are a guess worth checking.
WHISPER_SURE_LOGPROB = -0.6
WHISPER_SILENCE_PROB = 0.5
LOW_CONFIDENCE = 0.5

LANGUAGE_NAMES = {"english": "English", "sinhala": "Sinhala", "singlish": "Singlish", "tamil": "Tamil"}


@dataclass
class VoiceResult:
    status: str                 # "ok" | "no_speech" | "failed"
    text: str = ""              # what was said, as the customer would write it
    language: str = ""          # english | sinhala | singlish | tamil | other
    english: str = ""           # the meaning in English ("" when text is already English)
    confidence: float = 0.0
    engine: str = ""

    @property
    def unclear(self) -> bool:
        return self.status == "ok" and self.confidence < LOW_CONFIDENCE


# --- What Whisper invents on silence and noise -----------------------------------------
# It was trained on subtitled videos, so an empty clip often comes back as a sign-off.
_ALWAYS_INVENTED = ("thank you for watching", "thanks for watching", "please subscribe", "subscribe to",
                    "like and subscribe", "subtitles by", "amara.org", "transcribed by", "copyright")
_INVENTED_WHEN_UNSURE = {"thank you", "thanks", "you", "bye", "okay", "ok", "so", "music", "applause",
                         "silence", "hmm", "uh", "um", "oh", "ah"}


def _plain(text: str) -> str:
    """Lower case, punctuation dropped, single spaces - for comparing transcripts."""
    # Sinhala and Tamil vowel signs aren't \w; keep those scripts whole so words stay words.
    return " ".join(re.sub(r"[^\w\s'඀-෿஀-௿]", " ", (text or "").lower()).split())


def looks_invented(text: str, prompt: str = "", unsure: bool = False) -> bool:
    """True when a Whisper transcript is probably not what the customer said."""
    plain = _plain(text)
    if not plain or not re.search(r"\w", plain):
        return True
    if any(p in plain for p in _ALWAYS_INVENTED):
        return True
    if unsure and plain in _INVENTED_WHEN_UNSURE:
        return True
    words = plain.split()
    # Stuck in a loop: one short phrase repeated over and over.
    if len(words) >= 6:
        for size in (1, 2, 3):
            chunks = [" ".join(words[i:i + size]) for i in range(0, len(words) - size + 1, size)]
            if chunks and max(chunks.count(c) for c in set(chunks)) * size >= 0.7 * len(words):
                return True
    # Read our hint back instead of listening. Only a long stretch of it counts: a customer
    # really may say "Do you have Panadol 500mg?", which the hint also contains.
    # On silence Whisper reads back a piece of its hint - seen live: "Hi shop." and "Delivery
    # thiyenawada?", both reported as sure. Anything made only of the hint's words in order is
    # set aside; Gemini then listens, so a customer who really said it is still understood.
    hint = _plain(prompt)
    if hint and f" {plain} " in f" {hint} ":
        return True
    return False


def only_listed_names(text: str, names: list[str]) -> bool:
    """
    True when a transcript is nothing but the business's own listed words. On a silent clip an
    audio model shown the product list may 'hear' a product name - seen live: silence came back
    as "Panadol", 100% sure. Such an answer is believed only if Whisper heard it too.
    """
    words = set(_plain(text).split()) - {"a", "an", "the", "and", "hi", "hello"}
    listed = set(_plain(" ".join(names)).split())
    return bool(words) and words <= listed


# --- The business's own words ------------------------------------------------------------

def vocabulary(business_name: str, words: Iterable[str], limit_chars: int = 600) -> list[str]:
    """Distinct names, business first, trimmed to what a Whisper hint can hold (~224 tokens)."""
    out, seen, used = [], set(), 0
    for w in [business_name, *words]:
        w = " ".join(str(w or "").split())
        if not w or w.lower() in seen:
            continue
        if used + len(w) + 2 > limit_chars:
            break
        seen.add(w.lower())
        out.append(w)
        used += len(w) + 2
    return out


def whisper_hint(names: list[str]) -> str:
    """
    Whisper takes its prompt as text that came *before* the clip and copies its spelling and
    style - it does not follow instructions. So the hint is written like a chat a Sri Lankan
    customer might have had with this business.
    """
    # A list, not English sentences: a sample like "Do you have X?" is exactly what customers
    # say, and any transcript lifted from the hint is double-checked (looks_invented).
    # The Singlish samples are fine to collide with - Singlish is always checked by Gemini.
    business = names[0] if names else "the shop"
    products = ", ".join(names[1:]) if len(names) > 1 else ""
    hint = f"{business}: {products}. " if products else f"{business}. "
    hint += "Meke ganan kiyada? Details tika ewanna. Delivery thiyenawada? Ada open da? "
    # Delivery questions name towns; heard live, "Kandy" became "Candy" without this.
    hint += "Colombo, Kandy, Galle, Negombo, Kurunegala, Matara, Jaffna, Gampaha."
    return hint


def gemini_prompt(names: list[str], written_style: str) -> str:
    business = names[0] if names else "a local business"
    words = ", ".join(names[1:]) or "(none listed)"
    sinhala_rule = (
        "write Sinhala speech in Roman letters the way this customer types on WhatsApp (Singlish, "
        "e.g. 'meke ganan kiyada')" if written_style == "singlish"
        else "write Sinhala speech in Sinhala script"
    )
    return (
        f"This is a WhatsApp voice note a customer sent to {business} in Sri Lanka.\n"
        f"Names this business uses - ONLY a spelling guide for when you clearly hear one of them; "
        f"they are not part of the audio and must never be output unless actually spoken: {words}\n\n"
        "Report what the customer said. Answer with JSON only:\n"
        '{"speech": true, "transcript": "...", "language": "english|sinhala|singlish|tamil|other", '
        '"english": "...", "confidence": 0.0}\n\n'
        "Rules:\n"
        "- transcript: exactly the words spoken, nothing added, nothing answered. English as English; "
        f"{sinhala_rule}; Sinhala mixed with English words as Singlish in Roman letters; Tamil in Tamil script.\n"
        "- language: 'singlish' when Sinhala and English are mixed.\n"
        "- english: the meaning in plain English (repeat the transcript if it is already English).\n"
        "- confidence: 0 to 1, how sure you are of the words.\n"
        '- If it is silence, noise, music or nothing you can make out: {"speech": false, "transcript": "", '
        '"language": "other", "english": "", "confidence": 0}. Silent and near-silent notes are common; '
        "never guess words for them.\n"
        "- Never reply to the customer and never describe the audio; only report their words."
    )


# --- Is there any sound at all? -----------------------------------------------------------
# Listeners invent words for silent clips (seen live, from both Whisper and Gemini, sometimes
# "100% sure"). So the recording itself is measured first: decoded with PyAV (bundles ffmpeg,
# reads WhatsApp's Ogg/Opus) and checked for frames clearly louder than its background noise.

FRAME = 480                 # 30 ms at 16 kHz


def measure_sound(audio: bytes) -> Optional[dict]:
    """Duration and loudness profile of a clip, or None if it can't be decoded here."""
    try:
        import av
        import numpy as np
    except ImportError:
        return None
    try:
        chunks = []
        with av.open(io.BytesIO(audio)) as container:
            stream = next(s for s in container.streams if s.type == "audio")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(stream):
                chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    except Exception as exc:  # noqa: BLE001 — an odd file must never stop the listening
        logger.info(f"Voice note: couldn't measure the sound ({exc}); listening anyway")
        return None
    if not chunks:
        return {"duration": 0.0, "peak_db": -120.0, "floor_db": -120.0, "active": 0.0}
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    usable = len(samples) // FRAME * FRAME
    if usable == 0:
        return {"duration": len(samples) / 16000, "peak_db": -120.0, "floor_db": -120.0, "active": 0.0}
    rms = np.sqrt(np.mean(samples[:usable].reshape(-1, FRAME) ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-9)
    floor = float(np.percentile(db, 10))
    peak = float(np.percentile(db, 98))
    # Voice rises well above the room's hum; a fan or traffic alone stays near its own floor.
    active = float(np.mean(db > max(floor + 10.0, -50.0)))
    return {"duration": len(samples) / 16000, "peak_db": peak, "floor_db": floor, "active": active}


def sounds_silent(m: Optional[dict]) -> bool:
    """True only when the measurement clearly shows nobody spoke. Unknown -> False."""
    if not m:
        return False
    return m["duration"] < 0.3 or m["peak_db"] < -50.0 or m["active"] < 0.03


# --- Listening ---------------------------------------------------------------------------

def _normalise_language(value: str, text: str) -> str:
    value = (value or "").strip().lower()
    if value in ("en", "english"):
        # Whisper calls Singlish "English"; the words tell.
        return "singlish" if detect_language_intent(text) == "singlish" else "english"
    if value in ("si", "sinhala", "sinhalese"):
        return "sinhala" if any("඀" <= c <= "෿" for c in text) else "singlish"
    if value in ("singlish", "tamil"):
        return value
    if value in ("ta",):
        return "tamil"
    return "other"


def _mime_and_name(mime: str) -> tuple[str, str]:
    base = (mime or "audio/ogg").split(";")[0].strip().lower() or "audio/ogg"
    ext = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
           "audio/aac": "m4a", "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/x-wav": "wav",
           "audio/webm": "webm", "audio/amr": "amr", "audio/flac": "flac"}.get(base, "ogg")
    return base, f"voice_note.{ext}"


async def _ask_gemini(audio: bytes, mime: str, names: list[str], written_style: str) -> Optional[VoiceResult]:
    if len(audio) > MAX_INLINE_AUDIO:
        return None
    result = await llm_router.listen_audio(audio, mime, gemini_prompt(names, written_style))
    if not result:
        return None
    try:
        data = llm_router.parse_json(result.text)
    except ValueError:
        logger.warning(f"Voice note: {result.label} answered without JSON")
        return None
    if not isinstance(data, dict):
        return None
    transcript = " ".join(str(data.get("transcript") or "").split())
    if data.get("speech") is False or not transcript:
        return VoiceResult("no_speech", engine=result.label)
    language = str(data.get("language") or "").lower()
    if language not in ("english", "sinhala", "singlish", "tamil"):
        language = "other"
    english = " ".join(str(data.get("english") or "").split())
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        confidence = 0.7
    return VoiceResult("ok", transcript, language, "" if english.lower() == transcript.lower() else english,
                       confidence, result.label)


async def understand_voice_note(audio: bytes, mime: str = "audio/ogg", business_name: str = "",
                                words: Iterable[str] = (), written_style: str = "",
                                second_opinion_seconds: Optional[float] = None) -> VoiceResult:
    """
    Hear a voice note. Never raises: a failure comes back as status 'failed'.

    `second_opinion_seconds`: on a live call someone is waiting, so the second listener (Gemini, for
    Sinhala or an unsure Whisper) gets only this long; after that Whisper's words are used. A voice
    note has no deadline (None): getting it right matters more than a few seconds.
    """
    if not audio:
        return VoiceResult("failed")
    sound = await asyncio.to_thread(measure_sound, audio)
    if sounds_silent(sound):
        logger.info(f"Voice note: no voice in the recording ({sound}); not sending it to any listener")
        return VoiceResult("no_speech", engine="sound check")
    mime, filename = _mime_and_name(mime)
    names = vocabulary(business_name, words)
    hint = whisper_hint(names)

    heard = None
    try:
        heard = await llm_router.transcribe_audio_detailed(audio, filename, hint)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Voice note: Whisper failed: {exc}")

    whisper_result = None
    whisper_silent = False
    if heard:
        logprob, silence = heard.get("avg_logprob"), heard.get("no_speech_prob")
        unsure = (logprob is not None and logprob < WHISPER_SURE_LOGPROB) or (silence is not None and silence >= WHISPER_SILENCE_PROB)
        text = " ".join((heard.get("text") or "").split())
        if looks_invented(text, hint, unsure):
            whisper_silent = True
            logger.info(f"Voice note: ignoring Whisper's '{text[:60]}' (probably invented on silence/noise)")
        else:
            language = _normalise_language(heard.get("language", ""), text)
            # avg_logprob -0.1 → 0.9 sure, -0.6 → 0.4: a rough but honest scale.
            confidence = max(0.05, min(0.98, 1.0 + logprob)) if logprob is not None else (0.4 if unsure else 0.85)
            whisper_result = VoiceResult("ok", text, language, "", confidence, heard.get("engine", "whisper"))
            if language == "english" and not unsure:
                return whisper_result          # sure, and English: nothing to improve on

    try:
        second = await asyncio.wait_for(_ask_gemini(audio, mime, names, written_style), timeout=second_opinion_seconds)
    except asyncio.TimeoutError:
        logger.info(f"Voice note: the second listener took over {second_opinion_seconds}s; using Whisper's words")
        second = None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Voice note: Gemini failed: {exc}")
        second = None

    if second and second.status == "ok" and only_listed_names(second.text, names):
        whisper_words = set(_plain(whisper_result.text).split()) if whisper_result else set()
        if not set(_plain(second.text).split()) <= whisper_words:
            logger.info(f"Voice note: doubting '{second.text[:60]}' - only listed names, not confirmed by Whisper")
            if whisper_result:
                return whisper_result
            if whisper_silent:
                return VoiceResult("no_speech", engine=second.engine)
            second.confidence = min(second.confidence, 0.3)   # nothing to check against: flag it
            return second
    if second and second.status == "ok":
        return second
    if whisper_result:
        return whisper_result                  # Whisper's words, even if unsure, beat nothing
    if second and second.status == "no_speech":
        return second
    if whisper_silent:
        return VoiceResult("no_speech", engine=(heard or {}).get("engine", "whisper"))
    return VoiceResult("failed")


# --- Wiring into a conversation ------------------------------------------------------------

async def written_style(db: AsyncSession, tenant_id: int, phone: str) -> str:
    """How this customer writes: 'sinhala', 'singlish' or 'english' ('' when we don't know yet)."""
    rows = (await db.execute(
        select(ChatHistory.content).where(
            ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone,
            ChatHistory.role == "user", ChatHistory.message_type == "text",
        ).order_by(ChatHistory.id.desc()).limit(6)
    )).scalars().all()
    text = " ".join(r or "" for r in rows).strip()
    return detect_language_intent(text) if text else ""


async def business_words(db: AsyncSession, tenant: Tenant) -> list[str]:
    names = (await db.execute(
        select(Product.name).where(Product.tenant_id == tenant.id, Product.is_active == True)  # noqa: E712
        .order_by(Product.id).limit(60)
    )).scalars().all()
    extra = [tenant.bot_name] if getattr(tenant, "bot_name", None) else []
    for line in (getattr(tenant, "quick_menu", "") or "").splitlines():
        title = line.split("|")[0].strip()
        if title:
            extra.append(title)
    return [*names, *extra]


async def hear_voice_note(db: AsyncSession, tenant: Tenant, phone: str, message: dict) -> tuple[VoiceResult, str]:
    """Download and understand the voice note in `message`. Returns (result, customer's written style)."""
    audio_info = message.get("audio") or {}
    style = await written_style(db, tenant.id, phone)
    media_id = audio_info.get("id")
    if not media_id:
        return VoiceResult("failed"), style
    audio = await download_whatsapp_media(media_id, tenant.meta_access_token)
    if not audio:
        logger.warning(f"Voice note {media_id}: could not download it from WhatsApp")
        return VoiceResult("failed"), style
    result = await understand_voice_note(audio, audio_info.get("mime_type", "audio/ogg"), tenant.business_name,
                                         await business_words(db, tenant), style)
    logger.info(f"Voice note from {phone}: {result.status} via {result.engine or '-'} "
                f"[{result.language or '-'}, {result.confidence:.2f}] '{result.text[:80]}'")
    return result, style


def inbox_text(result: VoiceResult) -> str:
    """What the inbox shows for a voice note: the words, their language, and their meaning."""
    if result.status == "no_speech":
        return "[Voice note] (silent - nothing could be heard)"
    if result.status != "ok":
        return "[Voice note] (couldn't be transcribed - listen to it on the phone)"
    label = "[Voice note"
    if result.language and result.language != "english":
        label += f" · {LANGUAGE_NAMES.get(result.language, 'other language')}"
    label += "]"
    text = f"{label} {result.text}"
    if result.unclear:
        text += " (unclear audio)"
    if result.english and result.language != "english":
        text += f"\n→ {result.english}"
    return text


def bot_text(result: VoiceResult) -> str:
    """
    What the bot reads. Sinhala and Singlish go as spoken - the bot answers in the customer's
    language. For Tamil and other languages the English meaning is added so it can still help.
    """
    if result.language in ("tamil", "other") and result.english:
        return f"{result.text}\n({result.english})"
    return result.text


_FALLBACK = {
    "failed": {
        "english": "Sorry, I couldn't listen to your voice note just now 🙏 Could you type your message, please?",
        "singlish": "Sorry, oyage voice note eka dan ahanna bari una 🙏 Karunakarala eka type karala ewanna puluwanda?",
        "sinhala": "සමාවෙන්න, ඔබගේ හඬ පණිවිඩය දැන් අහන්න බැරි වුණා 🙏 කරුණාකර එය type කරලා එවන්න පුළුවන්ද?",
    },
    "no_speech": {
        "english": "I couldn't hear anything in your voice note 🎙️ Could you send it again, or type your message? 🙏",
        "singlish": "Oyage voice note eke mukuth ahenne na 🎙️ Ayeth ewanna puluwanda, nathnam type karanna 🙏",
        "sinhala": "ඔබගේ හඬ පණිවිඩයේ කිසිවක් ඇහුණේ නැහැ 🎙️ කරුණාකර නැවත එවන්න, නැත්නම් type කරන්න 🙏",
    },
}


def fallback_reply(status: str, style: str = "") -> str:
    """The reply when a voice note can't be understood, in the language the customer writes in."""
    options = _FALLBACK.get(status, _FALLBACK["failed"])
    return options.get(style or "english", options["english"])
