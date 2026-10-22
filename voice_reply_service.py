"""
Answering a voice note with a voice note.

A customer who spoke gets spoken to: the bot's text reply is reworded for the ear, read aloud by
a free Microsoft Edge neural voice, converted to OGG/Opus (the only format WhatsApp shows as a
voice-note bubble) and sent before the same reply in text. Built for the Wasala Nature Resort bot.

Singlish (Sinhala typed in English letters) cannot be pronounced by any voice, so it is first
rewritten into Sinhala script. If that rewrite fails, no voice note is sent - the text still is.
"""

import asyncio
import io
import logging
import re
from typing import Tuple

import llm_router
from ai_service import detect_language_intent

logger = logging.getLogger("uvicorn.error")

SINHALA_VOICE = "si-LK-ThiliniNeural"
TAMIL_VOICE = "ta-LK-SaranyaNeural"
ENGLISH_VOICE = "en-US-JennyNeural"

SPOKEN_REPLY_PROMPT = (
    "You prepare a WhatsApp text reply to be read aloud as a voice note by a text-to-speech voice.\n"
    "Rewrite the reply so it sounds natural spoken, keeping every fact, number and question exactly the same. "
    "Do not add, remove or answer anything.\n"
    "- If the reply is Sinhala typed in English letters (Singlish), you MUST convert every Sinhala word into Sinhala "
    "script, because the voice cannot pronounce Sinhala written in English letters. Only genuine English words stay in "
    "English letters (price, package, booking, delivery, order, names of products).\n"
    "  Example input: Gold package eka Rs. 753,000 wenne. Booking eka ganna hithan inne mona dawasakada?\n"
    "  Example output: Gold package එක රුපියල් 753,000 වෙන්නේ. Booking එක ගන්න හිතන් ඉන්නේ මොන දවසකද?\n"
    "- If the reply is already in Sinhala script, keep it that way. If it is in English or Tamil, keep that language.\n"
    "- Remove emojis, asterisks, underscores, bullet symbols, links and anything inside square brackets.\n"
    "- Turn lists into flowing sentences.\n"
    "- Write prices as the number followed by 'rupees' (English) or preceded by 'රුපියල්' (Sinhala). Keep the digits.\n"
    "- Do not read phone numbers aloud. Say the number is in the message below instead.\n"
    "- If the reply mentions an attached file, say it is being sent in the chat.\n"
    "Output only the text to be spoken."
)

_SINHALA_LETTER = re.compile(r"[඀-෿]")
_TAMIL_LETTER = re.compile(r"[஀-௿]")


def strip_text_for_speech(reply_text: str) -> str:
    """Removes what a voice should not read out: control tags, links, markdown symbols and emojis."""
    text = re.sub(r"\[[^\]]*\]", " ", reply_text or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_~`•#>|]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", text)
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"\.(\s*\.)+", ".", text)
    text = re.sub(r"([?!])\s*\.", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .")
    return text + "." if text and text[-1] not in "?!" else text


def sinhala_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(bool(_SINHALA_LETTER.match(c)) for c in letters) / max(len(letters), 1)


async def build_spoken_reply(reply_text: str) -> str:
    """
    The reply worded for a voice note, or "" when it cannot be spoken well (Singlish that could not
    be converted to Sinhala script).
    """
    plain_text = strip_text_for_speech(reply_text)
    if not plain_text:
        return ""
    is_singlish = sinhala_share(plain_text) == 0 and detect_language_intent(plain_text) == "singlish"

    if llm_router.role_candidates("fast"):
        prompt = (
            "This reply is Sinhala typed in English letters. Rewrite it for speech with every Sinhala word in "
            f"Sinhala script:\n\n{plain_text}" if is_singlish else plain_text
        )
        try:
            result = await llm_router.complete(
                "fast",
                [{"role": "system", "content": SPOKEN_REPLY_PROMPT}, {"role": "user", "content": prompt}],
                max_tokens=1200, temperature=0.0, timeout=30,
            )
            spoken = strip_text_for_speech(result.text)
            if spoken and not (is_singlish and sinhala_share(spoken) < 0.15):
                return spoken
            logger.warning(f"[voice reply] {result.label} left Singlish unconverted; no voice note")
        except llm_router.LLMUnavailable as e:
            logger.warning(f"[voice reply] could not reword the reply for speech: {e}")

    return "" if is_singlish else plain_text


def _voice_for(text: str) -> Tuple[str, str]:
    """(Edge voice, speaking rate) for the script the text is written in."""
    if _SINHALA_LETTER.search(text):
        return SINHALA_VOICE, "-5%"     # slightly slower Sinhala is clearer
    if _TAMIL_LETTER.search(text):
        return TAMIL_VOICE, "-5%"
    return ENGLISH_VOICE, "+0%"


def _mp3_to_ogg_opus(mp3_bytes: bytes) -> bytes:
    """WhatsApp only shows audio as a voice note (not a music file) when it is OGG/Opus."""
    import av

    output = io.BytesIO()
    with av.open(io.BytesIO(mp3_bytes)) as source, av.open(output, "w", format="ogg") as target:
        stream = target.add_stream("libopus", rate=48000)
        stream.bit_rate = 32000
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000, frame_size=960)
        for frame in source.decode(audio=0):
            for resampled in resampler.resample(frame):
                target.mux(stream.encode(resampled))
        for resampled in resampler.resample(None):
            target.mux(stream.encode(resampled))
        target.mux(stream.encode(None))
    return output.getvalue()


async def synthesize_voice_note(text: str) -> Tuple[bytes, str]:
    """(audio_bytes, mime_type) ready to upload to WhatsApp, or (b"", "") when speech could not be made."""
    if not text:
        return b"", ""
    try:
        import edge_tts

        voice, rate = _voice_for(text)
        if voice == SINHALA_VOICE:
            text = re.sub(r"Rs\.?\s*", "රුපියල් ", text)
            text = re.sub(r"/=", " යි", text)
        mp3_bytes = b""
        async for chunk in edge_tts.Communicate(text=text, voice=voice, rate=rate).stream():
            if chunk["type"] == "audio":
                mp3_bytes += chunk["data"]
        if not mp3_bytes:
            return b"", ""
    except Exception as e:  # noqa: BLE001 — no speech is fine, the text reply still goes out
        logger.error(f"[voice reply] could not generate speech: {e}")
        return b"", ""

    try:
        return await asyncio.to_thread(_mp3_to_ogg_opus, mp3_bytes), "audio/ogg"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[voice reply] could not convert to OGG/Opus, sending MP3 instead: {e}")
        return mp3_bytes, "audio/mpeg"
