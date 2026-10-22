"""
The behaviour carried over from the Wasala Nature Resort bot: private reasoning, the "sound like a
person" rules, the date and time, typing indicators, reply bubbles and voice-note replies.
"""
import asyncio
import itertools
from types import SimpleNamespace

import pytest

import ai_service
import llm_router
import main
import voice_reply_service
from ai_service import BotReply, split_reasoning_and_reply, split_reply_into_bubbles
from config import settings
from voice_service import VoiceResult

KB = "Opening hours: 9 AM to 6 PM daily.\nDelivery: free inside Colombo."
_ids = itertools.count(1)


class FakeChatModels:
    """Stands in for llm_router.complete: returns queued answers and records the prompts it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, role, messages, **kwargs):
        self.calls.append((role, messages))
        return SimpleNamespace(text=self.answers.pop(0), label="fake:model", latency=0.1)


@pytest.fixture
def chat_models(monkeypatch):
    def install(*answers):
        fake = FakeChatModels(*answers)
        monkeypatch.setattr(llm_router, "role_candidates", lambda role: [("fake", "model")])
        monkeypatch.setattr(llm_router, "complete", fake)
        return fake
    return install


def reply_to(message, history=(), from_voice=False):
    return asyncio.run(ai_service.generate_bot_reply("", KB, list(history), message, None, from_voice))


def test_reasoning_is_split_from_the_reply():
    assert split_reasoning_and_reply("<thinking>they want hours</thinking><reply>We're open till 6.</reply>") == (
        "they want hours", "We're open till 6.")
    # Model skipped the <reply> wrapper
    assert split_reasoning_and_reply("<thinking>x</thinking>\nOpen till 6.") == ("x", "Open till 6.")
    # Plain answer with no tags at all
    assert split_reasoning_and_reply("Open till 6.") == ("", "Open till 6.")
    # Cut off mid-thought: no reply, so nothing half-finished reaches the customer
    assert split_reasoning_and_reply("<thinking>step 1, step 2") == ("step 1, step 2", "")


def test_customer_never_sees_the_reasoning(chat_models):
    chat_models("<thinking>They ask about delivery. KB: free inside Colombo.</thinking>"
                "<reply>Yes, delivery is free inside Colombo.\n\nWhere should it go?</reply>")
    reply = reply_to("do you deliver?")
    assert reply.text == "Yes, delivery is free inside Colombo.\n\nWhere should it go?"
    assert "thinking" not in reply.text.lower() and "KB:" not in reply.text


def test_reply_cut_off_mid_thought_is_asked_for_again(chat_models):
    fake = chat_models("<thinking>long notes that never finish", "<thinking>short</thinking><reply>Till 6 today.</reply>")
    assert reply_to("open now?").text == "Till 6 today."
    assert len(fake.calls) == 2


def test_prompt_carries_the_wasala_rules(chat_models):
    fake = chat_models("<reply>Hi there.</reply>")
    reply_to("hello")
    system_prompt = fake.calls[0][1][0]["content"]

    for rule in ("REASONING PROTOCOL", "SOUND LIKE A REAL PERSON", "GREET ONCE", "HAVE A VIEW",
                 "REAL-TIME CALENDAR", "general knowledge", "never point out that they asked"):
        assert rule in system_prompt, rule
    assert ai_service.business_now().strftime("%B %d, %Y") in system_prompt
    # The robotic versions the Wasala bot replaced must be gone
    assert "You actually asked about this earlier" not in system_prompt
    assert "That's above my pay grade" not in system_prompt.split("STYLE EXAMPLES")[0]
    # Voice rules only for voice notes
    assert "VOICE NOTE" not in system_prompt


def test_voice_note_rules_are_added_for_voice_notes(chat_models):
    fake = chat_models("<reply>That's for 150 people, right?</reply>")
    reply_to("booking for 150 (?) people", from_voice=True)
    assert "--- VOICE NOTE ---" in fake.calls[0][1][0]["content"]


def test_reply_bubbles():
    assert split_reply_into_bubbles("Sure.\n\nWhat date?") == ["Sure.", "What date?"]
    # A line ending in ':' keeps its list in the same bubble
    assert split_reply_into_bubbles("Our hours:\n\n• Mon-Fri 9-6") == ["Our hours:\n\n• Mon-Fri 9-6"]
    assert split_reply_into_bubbles("a\n\nb\n\nc\n\nd") == ["a", "b", "c\n\nd"]


def test_speech_text_is_cleaned():
    cleaned = voice_reply_service.strip_text_for_speech("*Gold* package 😊\n[HUMAN_HANDOFF_REQUESTED] see https://x.y")
    assert cleaned == "Gold package. see."


def test_singlish_that_cannot_be_converted_is_not_spoken(monkeypatch):
    monkeypatch.setattr(llm_router, "role_candidates", lambda role: [])
    assert asyncio.run(voice_reply_service.build_spoken_reply("Hari, oyata puluwan heta enna")) == ""
    assert asyncio.run(voice_reply_service.build_spoken_reply("Sure, see you *tomorrow*!")) == "Sure, see you tomorrow!"


def whatsapp_message(message: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": settings.SEED_PHONE_NUMBER_ID},
        "messages": [{"id": f"wamid.wasala{next(_ids)}", "from": "94775550000", "timestamp": "0", **message}],
    }}]}]}


def test_reply_goes_out_as_bubbles_after_typing(client, outbox, monkeypatch):
    async def two_part_reply(**kwargs):
        return BotReply("We're open till 6 today.\n\nComing in this evening?")
    monkeypatch.setattr(main, "generate_bot_reply", two_part_reply)

    client.post("/webhook", json=whatsapp_message({"type": "text", "text": {"body": "are you open?"}}))
    kinds = [m["kind"] for m in outbox]
    texts = [m["message_text"] for m in outbox if m["kind"] == "text"]

    assert kinds[0] == "typing"
    assert texts[:2] == ["We're open till 6 today.", "Coming in this evening?"]


def test_voice_note_is_answered_with_a_voice_note_first(client, outbox, monkeypatch):
    async def heard(db, tenant, phone, message):
        return VoiceResult("ok", text="are you open today", language="english", confidence=0.9), "english"

    async def reply(**kwargs):
        assert kwargs["from_voice"] is True
        return BotReply("Yes, till 6 today.")

    async def spoken(text):
        return "Yes, till 6 today."

    async def speech(text):
        return b"OggS-fake-audio", "audio/ogg"

    monkeypatch.setattr(main, "hear_voice_note", heard)
    monkeypatch.setattr(main, "generate_bot_reply", reply)
    monkeypatch.setattr(main, "build_spoken_reply", spoken)
    monkeypatch.setattr(main, "synthesize_voice_note", speech)

    client.post("/webhook", json=whatsapp_message({"type": "audio", "audio": {"id": "media1", "mime_type": "audio/ogg"}}))
    sent = [m for m in outbox if m["kind"] in ("voice", "text")]

    assert sent[0]["kind"] == "voice" and sent[0]["spoken_text"] == "Yes, till 6 today."
    assert sent[1]["kind"] == "text" and sent[1]["message_text"] == "Yes, till 6 today."


def test_voice_replies_can_be_switched_off(client, outbox, monkeypatch):
    async def heard(db, tenant, phone, message):
        return VoiceResult("ok", text="hello", language="english", confidence=0.9), "english"

    async def reply(**kwargs):
        return BotReply("Hi!")

    monkeypatch.setattr(settings, "VOICE_REPLIES", False)
    monkeypatch.setattr(main, "hear_voice_note", heard)
    monkeypatch.setattr(main, "generate_bot_reply", reply)

    client.post("/webhook", json=whatsapp_message({"type": "audio", "audio": {"id": "media2", "mime_type": "audio/ogg"}}))
    assert [m["kind"] for m in outbox if m["kind"] in ("voice", "text")][0] == "text"
    assert not any(m["kind"] == "voice" for m in outbox)
