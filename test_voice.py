"""
Checks for voice notes (voice_service.py): which listener is trusted when, Whisper's inventions
on silence, the business's own words, replies in the customer's language, and the webhook flow
(including chats where the bot is switched off).

Offline: Whisper, Gemini and the WhatsApp download are replaced by scripted fakes; the database
is a throwaway file. whatsapp_bot.db is never opened.

    python test_voice.py
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
# TEST_DATABASE_URL runs the same checks on PostgreSQL (run_tests_postgres.py).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "voice_test.db").replace("\\", "/")

import httpx  # noqa: E402

import ai_service  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
import voice_service  # noqa: E402
import whatsapp_client  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import ChatHistory, Conversation, Product, Tenant  # noqa: E402
from sqlalchemy import select  # noqa: E402
from voice_service import VoiceResult, understand_voice_note  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


# --- Fakes -----------------------------------------------------------------------------

class Ears:
    """What the fake Whisper and Gemini will 'hear', and what they were asked."""

    def __init__(self):
        self.whisper = None        # dict returned by transcribe_audio_detailed, or None
        self.gemini = None         # JSON text Gemini answers, or None (unavailable)
        self.whisper_calls = []
        self.gemini_calls = []

    async def fake_whisper(self, audio, filename="voice_note.ogg", prompt=""):
        self.whisper_calls.append({"filename": filename, "prompt": prompt})
        return self.whisper

    async def fake_listen(self, audio, mime, prompt, timeout=45.0):
        self.gemini_calls.append({"mime": mime, "prompt": prompt})
        if self.gemini is None:
            return None
        return llm_router.LLMResult(text=self.gemini, provider="gemini", model="gemini-flash-latest", latency=0.1)


ears = Ears()
llm_router.transcribe_audio_detailed = ears.fake_whisper
llm_router.listen_audio = ears.fake_listen


def whisper(text, language="english", logprob=-0.15, silence=0.02):
    return {"text": text, "language": language, "avg_logprob": logprob, "no_speech_prob": silence, "engine": "groq/whisper"}


def gemini(transcript, language="english", english="", confidence=0.9, speech=True):
    import json
    return json.dumps({"speech": speech, "transcript": transcript, "language": language,
                       "english": english, "confidence": confidence})


async def hear(**kwargs):
    ears.whisper_calls.clear()
    ears.gemini_calls.clear()
    return await understand_voice_note(b"OggS-fake-audio", kwargs.pop("mime", "audio/ogg; codecs=opus"),
                                       kwargs.pop("business", "Lanka Care Pharmacy"),
                                       kwargs.pop("words", ["Panadol 500mg", "Vitamin C"]), kwargs.pop("style", ""))


SINHALA = "මේකේ ගණන කීයද?"


def _wav(noise: float, voice: bool = False, seconds: float = 3.0) -> bytes:
    """A test clip: background noise at `noise` level, plus voice-like bursts if `voice`."""
    import io
    import math
    import random
    import struct
    import wave
    rnd = random.Random(7)
    rate = 16000
    frames = []
    for i in range(int(rate * seconds)):
        x = rnd.gauss(0, noise) if noise else 0.0
        t = i / rate
        if voice and 0.5 < t < 2.5 and math.sin(2 * math.pi * 3 * t) > 0:      # syllables
            x += 0.3 * math.sin(2 * math.pi * 180 * t) * math.sin(2 * math.pi * 3 * t)
        frames.append(struct.pack("<h", int(max(-1, min(1, x)) * 32767)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))
    return buf.getvalue()


async def test_listening():
    print("\nWhich listener is trusted")
    ears.whisper, ears.gemini = whisper("Do you have Panadol 500mg?"), gemini("should not be used")
    r = await hear()
    check(r.status == "ok" and r.text == "Do you have Panadol 500mg?" and r.engine == "groq/whisper",
          "clear English: Whisper's words are used")
    check(not ears.gemini_calls, "and Gemini isn't asked (no extra cost or delay)")

    ears.whisper = whisper("mehe ganana kiyada", language="sinhala", logprob=-0.9)
    ears.gemini = gemini(SINHALA, "sinhala", "How much is this?", 0.92)
    r = await hear()
    check(len(ears.gemini_calls) == 1 and r.text == SINHALA and r.language == "sinhala"
          and r.english == "How much is this?", "Sinhala: Gemini listens again, its words and meaning are used")

    ears.whisper = whisper("meke ganan kiyada panadol", language="english", logprob=-0.2)
    ears.gemini = gemini("meke ganan kiyada Panadol 500mg", "singlish", "How much is Panadol 500mg?")
    r = await hear()
    check(len(ears.gemini_calls) == 1 and r.language == "singlish" and "Panadol 500mg" in r.text,
          "Singlish that Whisper calls English is recognised and checked again")

    ears.whisper = whisper("do you have vitamin see", logprob=-1.1)
    ears.gemini = gemini("Do you have Vitamin C?")
    r = await hear()
    check(len(ears.gemini_calls) == 1 and r.text == "Do you have Vitamin C?", "unsure English: checked again")

    ears.gemini = None
    r = await hear()
    check(r.status == "ok" and r.text == "do you have vitamin see" and r.unclear,
          "Gemini down: Whisper's guess is used, marked unclear")

    ears.whisper, ears.gemini = None, gemini("Are you open on Sunday?")
    r = await hear()
    check(r.status == "ok" and r.text == "Are you open on Sunday?", "no Whisper key: Gemini alone works")

    ears.whisper, ears.gemini = None, None
    r = await hear()
    check(r.status == "failed", "nobody can listen: 'failed', never an exception")

    ears.whisper, ears.gemini = whisper("Do you deliver?", language="sinhala", logprob=-1.0), "sorry I can't help"
    r = await hear()
    check(r.status == "ok" and r.text == "Do you deliver?", "Gemini answers without JSON: Whisper's words are kept")

    print("\nWhisper's inventions on silence and noise")
    ears.whisper, ears.gemini = whisper("Thank you for watching!", silence=0.8), gemini("", speech=False)
    r = await hear()
    check(r.status == "no_speech", "'Thank you for watching' on a silent clip -> silent, not a message")

    ears.whisper, ears.gemini = whisper("Thank you.", logprob=-0.9, silence=0.7), None
    r = await hear()
    check(r.status == "no_speech", "a bare 'Thank you.' from an unsure Whisper -> silent")

    ears.whisper = whisper("Thank you.", logprob=-0.1, silence=0.01)
    r = await hear()
    check(r.status == "ok" and r.text == "Thank you.", "a clearly spoken 'Thank you.' is kept")

    ears.whisper, ears.gemini = whisper("ok ok ok ok ok ok ok ok ok"), None
    r = await hear()
    check(r.status == "no_speech", "a phrase stuck in a loop is ignored")

    hint = voice_service.whisper_hint(voice_service.vocabulary("Lanka Care Pharmacy", ["Panadol 500mg", "Vitamin C"]))
    ears.whisper, ears.gemini = whisper(" ".join(hint.split("? ")[:2]) + "?"), None
    r = await hear()
    check(r.status == "no_speech", "Whisper reading our hint back is ignored")

    check(not voice_service.looks_invented(SINHALA), "Sinhala words are never mistaken for an invention")

    # Both seen live on a silent clip: Whisper continued from its hint, Gemini 'heard' a product.
    ears.whisper, ears.gemini = whisper("Lanka Care Pharmacy.", silence=0.0), gemini("Panadol 500mg", confidence=1.0)
    r = await hear()
    check(r.status == "no_speech", "silence: Whisper echoing the hint + Gemini naming a product -> silent")
    ears.whisper, ears.gemini = whisper("Delivery thiyenawada?", logprob=-0.4), gemini("", speech=False)
    r = await hear()
    check(r.status == "no_speech", "silence: a sentence lifted from the middle of the hint -> silent")
    ears.whisper, ears.gemini = whisper("Delivery thiyenawada?"), gemini("Delivery thiyenawada?", "singlish", "Do you deliver?")
    r = await hear()
    check(r.status == "ok" and r.text == "Delivery thiyenawada?", "the same words really spoken: Gemini confirms them")
    ears.whisper, ears.gemini = whisper("Panadol 500mg please"), gemini("Panadol 500mg please")
    ears.whisper["avg_logprob"] = -0.9
    r = await hear()
    check(r.status == "ok" and r.text == "Panadol 500mg please", "a product request both listeners heard is believed")
    ears.whisper, ears.gemini = None, gemini("Vitamin C", confidence=1.0)
    r = await hear()
    check(r.status == "ok" and r.unclear, "a bare product name nobody can confirm is flagged unclear")
    ears.whisper, ears.gemini = whisper("hmm", language="sinhala", logprob=-1.2), gemini("Panadol thiyenawada?", "singlish")
    r = await hear()
    check(r.status == "ok" and r.text == "Panadol thiyenawada?", "a real sentence with a product name is not doubted")

    print("\nIs anyone speaking? (the recording itself is measured)")
    if voice_service.measure_sound(_wav(0.0)) is None:
        print("  --  PyAV not installed here: sound check skipped (the bot still works without it)")
    else:
        ears.whisper, ears.gemini = whisper("Thank you for watching"), gemini("Panadol 500mg", confidence=1.0)
        for label, clip in (("silence", _wav(0.0)), ("a steady hum", _wav(0.05, voice=False))):
            ears.whisper_calls.clear()
            ears.gemini_calls.clear()
            r = await understand_voice_note(clip, "audio/wav", "Lanka Care Pharmacy", ["Panadol 500mg"])
            check(r.status == "no_speech" and not ears.whisper_calls and not ears.gemini_calls,
                  f"{label}: 'silent' without asking any listener (nothing to invent, nothing to pay for)")
        ears.whisper = whisper("Do I need a prescription?")
        ears.whisper_calls.clear()
        r = await understand_voice_note(_wav(0.01, voice=True), "audio/wav", "Lanka Care Pharmacy", [])
        check(r.status == "ok" and ears.whisper_calls, "a voice over a quiet room goes to the listeners")
        check(voice_service.measure_sound(b"not audio at all") is None, "an undecodable file is listened to anyway")

    print("\nThe business's own words")
    ears.whisper, ears.gemini = whisper("hello", language="tamil", logprob=-0.5), gemini("வணக்கம்", "tamil", "Hello")
    await hear(style="singlish")
    prompt = ears.whisper_calls[0]["prompt"]
    check("Lanka Care Pharmacy" in prompt and "Panadol 500mg" in prompt and "Vitamin C" in prompt,
          "Whisper's hint names the business and its products")
    g = ears.gemini_calls[0]["prompt"]
    check("Panadol 500mg" in g and "Roman letters the way this customer types" in g,
          "Gemini gets the product names, and writes Sinhala as Singlish for a Singlish typist")
    await hear(style="sinhala")
    check("Sinhala speech in Sinhala script" in ears.gemini_calls[0]["prompt"], "and in Sinhala script for a Sinhala typist")
    check(ears.gemini_calls[0]["mime"] == "audio/ogg" and ears.whisper_calls[0]["filename"] == "voice_note.ogg",
          "WhatsApp's 'audio/ogg; codecs=opus' is sent as audio/ogg")
    names = voice_service.vocabulary("Shop", [f"Product number {i}" for i in range(200)])
    check(names[0] == "Shop" and sum(len(n) + 2 for n in names) <= 600, "a long catalogue is trimmed to fit the hint")

    print("\nWhat the bot and the inbox see")
    tamil = VoiceResult("ok", "வணக்கம், விலை என்ன?", "tamil", "Hello, what is the price?", 0.9)
    check(voice_service.bot_text(tamil).endswith("(Hello, what is the price?)"), "Tamil: the bot also gets the English meaning")
    sinhala = VoiceResult("ok", SINHALA, "sinhala", "How much is this?", 0.9)
    check(voice_service.bot_text(sinhala) == SINHALA, "Sinhala goes to the bot as spoken, so it answers in Sinhala")
    check(voice_service.inbox_text(sinhala) == f"[Voice note · Sinhala] {SINHALA}\n→ How much is this?",
          "the inbox shows the words, the language and the meaning")
    check("(unclear audio)" in voice_service.inbox_text(VoiceResult("ok", "maybe", "english", "", 0.3)),
          "unclear audio is flagged in the inbox")
    check("Oyage" in voice_service.fallback_reply("no_speech", "singlish")
          and "ඔබගේ" in voice_service.fallback_reply("failed", "sinhala")
          and voice_service.fallback_reply("failed", "").startswith("Sorry"),
          "the 'couldn't hear you' reply is in the language the customer writes in")


# --- Webhook flow ----------------------------------------------------------------------

asked_ai = []


async def scripted_reply(system_prompt, knowledge_base, chat_history, user_message, config):
    asked_ai.append(user_message)
    return ai_service.BotReply(f"Answer to: {user_message}")


main.generate_bot_reply = scripted_reply
sent = []


async def capture(pid, to, body, *rest):
    sent.append(body)


whatsapp_client.add_send_listener(capture)
download = {"bytes": b"OggS-fake-audio"}


async def fake_download(media_id, token):
    return download["bytes"]


voice_service.download_whatsapp_media = fake_download


def voice_webhook(msg_id, phone="94771112222"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "5555"},
        "contacts": [{"wa_id": phone, "profile": {"name": "Saman"}}],
        "messages": [{"from": phone, "id": msg_id, "type": "audio",
                      "audio": {"id": f"media-{msg_id}", "mime_type": "audio/ogg; codecs=opus", "voice": True}}]}}]}]}


def text_webhook(msg_id, body, phone="94771112222"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "5555"},
        "contacts": [{"wa_id": phone, "profile": {"name": "Saman"}}],
        "messages": [{"from": phone, "id": msg_id, "type": "text", "text": {"body": body}}]}}]}]}


async def last_customer_row(phone="94771112222"):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ChatHistory).where(
            ChatHistory.customer_phone_number == phone, ChatHistory.sender == "customer")
            .order_by(ChatHistory.id.desc()))).scalars().first()


async def test_webhook():
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lanka Care Pharmacy", whatsapp_phone_number_id="5555",
                   meta_access_token="EAA_placeholder_token", system_prompt="x", knowledge_base="x")
        db.add(t)
        await db.flush()
        db.add(Product(tenant_id=t.id, name="Panadol 500mg", price=120, stock_quantity=10))
        await db.commit()
        tenant_id = t.id

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\nVoice notes through the webhook")
        ears.whisper = whisper("mehe ganana", language="sinhala", logprob=-1.0)
        ears.gemini = gemini(SINHALA, "sinhala", "How much is this?")
        await client.post("/webhook", json=voice_webhook("v1"))
        check(asked_ai[-1] == SINHALA, "the bot answers what was said, in the customer's language")
        row = await last_customer_row()
        check(row.content.startswith("[Voice note · Sinhala]") and "How much is this?" in row.content,
              "the inbox shows the words and their meaning")
        check("Panadol 500mg" in ears.whisper_calls[-1]["prompt"], "the business's real product names guide the listening")

        await client.post("/webhook", json=text_webhook("t1", "meke ganan kiyada"))
        before = len(asked_ai)
        download["bytes"] = b""
        await client.post("/webhook", json=voice_webhook("v2"))
        check(len(asked_ai) == before and "oyage voice note eka" in sent[-1],
              "a note that can't be downloaded gets a 'please type it' reply in Singlish, for a Singlish typist")
        check("couldn't be transcribed" in (await last_customer_row()).content, "and the inbox says so")
        download["bytes"] = b"OggS-fake-audio"

        ears.whisper, ears.gemini = whisper("Thanks for watching!", silence=0.9), gemini("", speech=False)
        await client.post("/webhook", json=voice_webhook("v3"))
        check(len(asked_ai) == before and "mukuth ahenne na" in sent[-1], "a silent note: the customer is asked to send it again")

        async with AsyncSessionLocal() as db:
            conv = (await db.execute(select(Conversation).where(Conversation.tenant_id == tenant_id))).scalar_one()
            conv.bot_enabled = False
            await db.commit()
        ears.whisper, ears.gemini = whisper("Can you deliver to Kandy today?"), None
        replies = len(sent)
        await client.post("/webhook", json=voice_webhook("v4"))
        check((await last_customer_row()).content == "[Voice note] Can you deliver to Kandy today?",
              "bot off for the chat: the voice note is still written out for the person answering")
        check(len(sent) == replies and len(asked_ai) == before, "and nothing is sent automatically")


async def main_async():
    await init_db()
    await test_listening()
    await test_webhook()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
