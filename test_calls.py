"""
Checks for WhatsApp voice calls (call_service.py, call_audio.py) over real WebRTC on this machine.

The test plays Meta and both phones: a "customer phone" and a "manager phone" are aiortc peers
that send tones instead of voices (440 Hz = the bot speaking, 1000 Hz = the manager, 700 Hz = the
customer), so who hears whom can be measured. Speech-to-text, the AI reply and text-to-speech are
scripted; the Calls API is recorded, never sent.

    python test_calls.py
"""

import asyncio
import fractions
import json
import os
import sys
import tempfile
import time

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["MEDIA_DIR"] = os.path.join(_TMP.name, "media")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "calls_test.db").replace("\\", "/")

import av  # noqa: E402
import numpy as np  # noqa: E402
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import MediaStreamError  # noqa: E402
from sqlalchemy import select  # noqa: E402

import call_audio  # noqa: E402
import call_service as cs  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402,F401 - the webhook wiring
import voice_service  # noqa: E402
from config import settings  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import Call, ChatHistory, Conversation, Tenant  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
MANAGER, CUSTOMER = "94779998877", "94771110000"
BOT_HZ, MANAGER_HZ, CUSTOMER_HZ = 440, 1000, 700


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def tone(hz: int, seconds: float, rate: int = 48_000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * hz * t) * 9000).astype(np.int16).tobytes()


def main_hz(pcm: bytes, rate: int = 48_000) -> int:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if a.size < 1000 or np.sqrt(np.mean(a * a)) < 500:
        return 0
    spectrum = np.abs(np.fft.rfft(a))
    return int(np.argmax(spectrum) * rate / a.size)


class Phone:
    """A WhatsApp user's phone: sends what `talk()` queues, records what it hears."""

    def __init__(self):
        self.pc = RTCPeerConnection()
        self.mouth = call_audio.SpeakerTrack()
        self.pc.addTrack(self.mouth)
        self.heard = bytearray()
        self.task = None

        @self.pc.on("track")
        def _on(track):
            self.task = asyncio.ensure_future(self._ear(track))

    async def _ear(self, track):
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            self.heard.extend(call_audio.to_mono(frame, 48_000))

    def talk(self, hz: int, seconds: float = 1.0):
        self.mouth.say(tone(hz, seconds))

    def recent_hz(self, seconds: float = 0.4) -> int:
        return main_hz(bytes(self.heard[-int(seconds * 48_000) * 2:]))

    async def hears(self, hz: int, within: float = 8.0) -> bool:
        end = time.monotonic() + within
        while time.monotonic() < end:
            if abs(self.recent_hz() - hz) < 40:
                return True
            await asyncio.sleep(0.1)
        return False

    async def close(self):
        await self.pc.close()


# --- Meta, scripted ----------------------------------------------------------------------

api_calls = []          # every Calls API request
sent_texts = []         # WhatsApp texts (to the manager)
permission_requests = []
permission = {"status": "permanent", "can_call": True, "can_ask": True}
manager_phone = {"phone": None, "answers": True}
heard_queue = []        # what speech-to-text "hears", in order
tenant_ref = {}


async def fake_calls_api(tenant, body):
    api_calls.append(body)
    if body.get("action") == "connect":
        call_id = f"wacid.out.{len(api_calls)}"
        asyncio.ensure_future(_phone_picks_up(tenant, call_id, body["session"]["sdp"], body["to"]))
        return {"messaging_product": "whatsapp", "calls": [{"id": call_id}]}
    return {"messaging_product": "whatsapp", "success": True}


async def _phone_picks_up(tenant, call_id, offer_sdp, to):
    """The called phone rings, and (maybe) answers."""
    await asyncio.sleep(0.2)
    value = {"metadata": {"phone_number_id": tenant.whatsapp_phone_number_id},
             "statuses": [{"id": call_id, "type": "call", "status": "RINGING"}]}
    await cs.handle_call_statuses(tenant, value["statuses"])
    if to == MANAGER and not manager_phone["answers"]:
        return
    phone = Phone()
    if to == MANAGER:
        manager_phone["phone"] = phone
    else:
        manager_phone["customer_out"] = phone
    await phone.pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
    await phone.pc.setLocalDescription(await phone.pc.createAnswer())
    await cs.handle_call_events(tenant, {"calls": [{"id": call_id, "event": "connect", "direction": "BUSINESS_INITIATED",
                                                    "session": {"sdp_type": "answer", "sdp": phone.pc.localDescription.sdp}}]})
    await asyncio.sleep(0.3)
    await cs.handle_call_statuses(tenant, [{"id": call_id, "type": "call", "status": "ACCEPTED"}])


async def fake_permission(tenant, phone):
    return dict(permission)


async def fake_ask(tenant, phone, text):
    permission_requests.append((phone, text))


async def fake_text(tenant, phone, text):
    sent_texts.append((phone, text))


async def fake_hear(audio, mime="audio/ogg", business_name="", words=(), written_style=""):
    text = heard_queue.pop(0) if heard_queue else ""
    return voice_service.VoiceResult("ok" if text else "no_speech", text, "english", "", 0.95, "test")


async def fake_words(db, tenant):
    return []


async def fake_speech(text):
    return tone(BOT_HZ, 0.8)


replies = []


async def fake_ai_reply(tenant_id, phone, said, note=""):
    replies.append((said, note))
    if "refund" in said.lower():
        return "I'll get the team for that.", True
    return "We open at 8 in the morning.", False


# --- helpers ----------------------------------------------------------------------------

async def incoming_call(tenant, call_id):
    heard_queue.clear()            # a cough left over from the last call must not be 'heard' here
    phone = Phone()
    await phone.pc.setLocalDescription(await phone.pc.createOffer())
    api_calls.clear()
    await cs.handle_call_events(tenant, {
        "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nimal"}}],
        "calls": [{"id": call_id, "from": CUSTOMER, "to": "94110000000", "event": "connect",
                   "direction": "USER_INITIATED", "session": {"sdp_type": "offer", "sdp": phone.pc.localDescription.sdp}}]})
    accept = next(b for b in api_calls if b.get("action") == "accept")
    await phone.pc.setRemoteDescription(RTCSessionDescription(accept["session"]["sdp"], "answer"))
    return phone


async def wait_for(predicate, within=8.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


async def row_for(call_id):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Call).where(Call.wa_call_id == call_id))).scalar_one_or_none()


# --- the checks -------------------------------------------------------------------------

async def test_unit():
    print("\nWords")
    for t in ["can I talk to a real person", "connect me to the manager please", "manager ekka kathakaranna one", "මැනේජර් එක්ක කතා කරන්න"]:
        check(cs.wants_person(t), f"asks for a person: {t}")
    for t in ["what time do you open", "is the manager's special on today"]:
        check(not cs.wants_person(t), f"does not: {t}")
    check(cs.is_goodbye("ok thank you bye") and cs.is_goodbye("bye") and not cs.is_goodbye("bye the way, price?"),
          "goodbye spotted, but not inside a question")
    check("ආයුබෝවන්" in cs.line("greet", "sinhala", "Shop") and "Shop" in cs.line("greet", "english", "Shop"),
          "greeting in the caller's language, with the business name")


async def test_incoming(tenant):
    print("\nIncoming call: the AI answers")
    phone = await incoming_call(tenant, "wacid.in.1")
    actions = [b["action"] for b in api_calls]
    check(actions[:2] == ["pre_accept", "accept"], "answered with pre_accept then accept")
    accept = next(b for b in api_calls if b["action"] == "accept")
    check("m=audio" in accept["session"]["sdp"] and "opus" in accept["session"]["sdp"].lower(),
          "the answer is an audio SDP with Opus")
    check(await phone.hears(BOT_HZ), "the caller hears the greeting")
    row = await row_for("wacid.in.1")
    check(row and row.direction == "incoming" and row.status == "in_progress", "logged as an incoming call in progress")

    await asyncio.sleep(1.2)                       # greeting finished
    heard_queue.append("what time do you open?")
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: len(replies) == 1, 10), "they spoke, they stopped: the AI got what they said")
    check(await phone.hears(BOT_HZ), "and the caller hears the spoken answer")

    async with AsyncSessionLocal() as db:
        turns = (await db.execute(select(ChatHistory).where(ChatHistory.customer_phone_number == CUSTOMER,
                                                            ChatHistory.message_type == "call"))).scalars().all()
    texts = [t.content for t in turns]
    check(any("what time do you open" in t for t in texts) and any("We open at 8" in t for t in texts),
          "both sides of the call are in the chat history, so the chat bot remembers the call")

    print("\nTalking over the bot stops it")
    session = cs._sessions["wacid.in.1"]
    session.speaker.say(tone(BOT_HZ, 5.0))
    await asyncio.sleep(0.5)
    heard_queue.append("")                         # a cough: nothing to answer
    phone.talk(CUSTOMER_HZ, 0.6)
    check(await wait_for(lambda: not session.speaker.talking, 3), "the bot goes quiet when the caller talks over it")

    print("\nThe caller hangs up")
    await cs.handle_call_events(tenant, {"calls": [{"id": "wacid.in.1", "event": "terminate", "status": "COMPLETED", "duration": 9}]})
    row = await row_for("wacid.in.1")
    transcript = json.loads(row.transcript)
    check(row.status == "completed" and row.duration_seconds is not None and row.ended_at, "logged as completed, with its length")
    check([t["who"] for t in transcript][:3] == ["bot", "customer", "bot"], "transcript: greeting, question, answer")
    check(row.summary and "what time do you open" in row.summary, "a summary is kept (plain one when no AI is available)")
    check("wacid.in.1" not in cs._sessions, "the live call is cleaned up")
    await phone.close()


async def test_transfer(tenant):
    print("\nComplaint on a call: the manager is rung and joined")
    replies.clear()
    sent_texts.clear()
    manager_phone.update({"phone": None, "answers": True})
    phone = await incoming_call(tenant, "wacid.in.2")
    await asyncio.sleep(1.5)
    heard_queue.append("I want to talk to the manager about my order")
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: any(b.get("action") == "connect" and b["to"] == MANAGER for b in api_calls), 10),
          "the AI rings the manager's WhatsApp")
    check(any(p == MANAGER and "Transferring a call" in t for p, t in sent_texts), "the manager gets a text saying who and why")
    check(not replies, "asking for a person is not answered by the AI")
    check(await wait_for(lambda: manager_phone["phone"] is not None, 5), "the manager picks up")
    session = cs._sessions["wacid.in.2"]
    check(await wait_for(lambda: session.partner is not None, 8), "the two calls are joined")
    mgr = manager_phone["phone"]
    await asyncio.sleep(0.5)
    mgr.talk(MANAGER_HZ, 2.0)
    check(await phone.hears(MANAGER_HZ), "the customer hears the manager")
    phone.talk(CUSTOMER_HZ, 2.0)
    check(await mgr.hears(CUSTOMER_HZ), "the manager hears the customer")
    check(session.listener.paused, "the AI stops listening while they talk")
    row = await row_for("wacid.in.2")
    check(row.status == "with_team" and row.transferred, "logged as transferred, with the team")
    async with AsyncSessionLocal() as db:
        conv = (await db.execute(select(Conversation).where(Conversation.customer_phone == CUSTOMER))).scalar_one()
    check(conv.needs_human, "the chat is marked 'Needs you'")

    print("\nThe manager hangs up first: the AI takes the caller back")
    manager_call = next(k for k, v in cs._sessions.items() if v.role == "manager")
    await cs.handle_call_events(tenant, {"calls": [{"id": manager_call, "event": "terminate", "status": "COMPLETED"}]})
    check(await wait_for(lambda: session.partner is None and not session.listener.paused, 3), "the AI listens again")
    check(await phone.hears(BOT_HZ), "and tells the caller they're back with the assistant")
    await session.hang_up()
    check(api_calls[-1] == {"call_id": "wacid.in.2", "action": "terminate"}, "hanging up tells WhatsApp (terminate)")
    await phone.close()
    await mgr.close()


async def test_transfer_unanswered(tenant):
    print("\nThe manager doesn't answer")
    sent_texts.clear()
    manager_phone.update({"phone": None, "answers": False})
    old = cs.RING_MANAGER_SECONDS
    cs.RING_MANAGER_SECONDS = 2
    phone = await incoming_call(tenant, "wacid.in.3")
    await asyncio.sleep(1.5)
    heard_queue.append("I want a refund, this is the second time")
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: any(p == MANAGER and "Missed transfer" in t for p, t in sent_texts), 12),
          "complaint -> manager rung -> no answer -> the manager is texted to call back")
    session = cs._sessions["wacid.in.3"]
    check(not session.handed_over and session.partner is None, "the AI stays on the line with the caller")
    check(any(t["text"].startswith("Sorry, our team can't") for t in session.transcript), "and tells them the team will call back")
    check(any(b.get("action") == "terminate" for b in api_calls), "the unanswered manager call is ended")
    cs.RING_MANAGER_SECONDS = old
    await session.hang_up()
    await phone.close()

    print("\nThe manager hasn't allowed calls")
    permission.update({"status": "no_permission", "can_call": False, "can_ask": True})
    permission_requests.clear()
    sent_texts.clear()
    phone = await incoming_call(tenant, "wacid.in.4")
    await asyncio.sleep(1.5)
    heard_queue.append("can I speak to someone please")
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: any(p == MANAGER and "Missed transfer" in t for p, t in sent_texts), 10),
          "no permission: no call, the manager is texted to call back")
    check(permission_requests and permission_requests[0][0].endswith(MANAGER), "and asked to allow calls for next time")
    await cs._sessions["wacid.in.4"].hang_up()
    await phone.close()
    permission.update({"status": "permanent", "can_call": True, "can_ask": True})


async def test_outgoing(tenant):
    print("\nThe business calls a customer")
    replies.clear()
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        result = await cs.start_outgoing_call(db, t, "+" + CUSTOMER, "Your order 12 is ready to collect.")
    check(result["status"] == "ringing", "permission given: it rings straight away")
    connect = next(b for b in api_calls if b.get("action") == "connect" and b["to"] == CUSTOMER)
    check(connect["session"]["sdp_type"] == "offer" and "m=audio" in connect["session"]["sdp"], "connect carries our audio offer")
    check(await wait_for(lambda: manager_phone.get("customer_out") is not None, 5), "the customer answers")
    phone = manager_phone["customer_out"]
    check(await phone.hears(BOT_HZ), "the AI opens the call")
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.direction == "outgoing" and row.status == "in_progress", "logged as outgoing, in progress")
    session = next(v for v in cs._sessions.values() if v.call_row_id == result["call_id"])
    check(session.transcript and "order 12" in session.transcript[0]["text"], "the opening line gives the reason for the call")
    await asyncio.sleep(1.2)
    heard_queue.append("ok what time can I come")
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: replies and replies[-1][1] == "Your order 12 is ready to collect.", 10),
          "the AI's replies know why we called")
    heard_queue.append("ok thank you bye")
    await asyncio.sleep(1.5)
    phone.talk(CUSTOMER_HZ, 1.0)
    check(await wait_for(lambda: session.ended.is_set(), 12), "'thank you bye' -> the AI answers and hangs up")
    await asyncio.wait_for(session.logged.wait(), 15)
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.status == "completed", "logged as completed")
    await phone.close()

    print("\nCalling someone who hasn't allowed calls")
    permission.update({"status": "no_permission", "can_call": False, "can_ask": True})
    permission_requests.clear()
    manager_phone["customer_out"] = None
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        result = await cs.start_outgoing_call(db, t, CUSTOMER, "Your table for tonight is confirmed.")
    check(result["status"] == "awaiting_permission", "no permission: the call waits")
    check(permission_requests and "table for tonight" in permission_requests[0][1], "they are asked to allow calls, with the reason")
    permission.update({"status": "temporary", "can_call": True, "can_ask": True})
    async with AsyncSessionLocal() as webhook_db:
      await main._handle_webhook_body({"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": tenant.whatsapp_phone_number_id},
        "messages": [{"from": CUSTOMER, "id": "wamid.perm1", "type": "interactive",
                      "interactive": {"type": "call_permission_reply",
                                      "call_permission_reply": {"response": "accept", "is_permanent": False}}}]}}]}]},
        webhook_db)
    check(await wait_for(lambda: manager_phone.get("customer_out") is not None, 6),
          "they tap Allow (webhook) -> the waiting call is placed")
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.wa_call_id and row.status in ("ringing", "in_progress"), "the same call row goes on to ring")
    session = next(v for v in cs._sessions.values() if v.call_row_id == result["call_id"])
    await session.hang_up()
    await manager_phone["customer_out"].close()

    print("\nThey decline")
    permission.update({"status": "no_permission", "can_call": False, "can_ask": True})
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        result = await cs.start_outgoing_call(db, t, CUSTOMER, "A quick question about your booking.")
    await cs.handle_permission_reply(tenant, CUSTOMER, {"response": "reject"})
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.status == "rejected", "declined: the call is marked rejected and never placed")
    permission.update({"status": "no_permission", "can_call": False, "can_ask": False})
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        try:
            await cs.start_outgoing_call(db, t, CUSTOMER, "Another try.")
            check(False, "asking limit")
        except ValueError as e:
            check("limit" in str(e), "WhatsApp's limit on asking is reached: a clear message, no request sent")
    permission.update({"status": "permanent", "can_call": True, "can_ask": True})


broadcasts = []


async def fake_broadcast(tenant_id, message):
    broadcasts.append(message)


def channels(ogg: bytes):
    """(left, right) of a stereo recording, as int16 arrays at 48 kHz."""
    import io as _io
    left, right = [], []
    with av.open(_io.BytesIO(ogg)) as c:
        rs = av.AudioResampler(format="s16", layout="stereo", rate=48_000)
        for frame in c.decode(audio=0):
            for f in rs.resample(frame):
                a = f.to_ndarray().reshape(-1, 2)
                left.append(a[:, 0])
                right.append(a[:, 1])
    return np.concatenate(left), np.concatenate(right)


async def ringing_call(tenant, call_id):
    """A call that rings the team first: the webhook is still waiting when this returns."""
    heard_queue.clear()
    phone = Phone()
    await phone.pc.setLocalDescription(await phone.pc.createOffer())
    api_calls.clear()
    broadcasts.clear()
    task = asyncio.ensure_future(cs.handle_call_events(tenant, {
        "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nimal"}}],
        "calls": [{"id": call_id, "from": CUSTOMER, "event": "connect", "direction": "USER_INITIATED",
                   "session": {"sdp_type": "offer", "sdp": phone.pc.localDescription.sdp}}]}))
    await wait_for(lambda: any(b.get("type") == "call.ringing" for b in broadcasts), 5)
    return phone, task


async def connect_caller(phone, task):
    await asyncio.wait_for(task, 20)
    accept = next(b for b in api_calls if b.get("action") == "accept")
    await phone.pc.setRemoteDescription(RTCSessionDescription(accept["session"]["sdp"], "answer"))


async def browser_offer():
    browser = Phone()
    await browser.pc.setLocalDescription(await browser.pc.createOffer())
    return browser


async def test_team(tenant):
    print("\nIncoming call rings the team's website first")
    settings.CALL_TEAM_RING_SECONDS = 6
    phone, task = await ringing_call(tenant, "wacid.team.1")
    ring = next(b for b in broadcasts if b.get("type") == "call.ringing")
    check(ring["phone"] == CUSTOMER and ring["name"] == "Nimal" and ring["seconds"] == 6, "every open website is told who is calling")
    check([b["action"] for b in api_calls] == ["pre_accept"], "the call isn't picked up yet: the customer hears it ringing")
    row = await row_for("wacid.team.1")
    browser = await browser_offer()
    answer = await cs.team_answer(tenant, row.id, browser.pc.localDescription.sdp, "Kasun")
    await browser.pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
    await connect_caller(phone, task)
    check(api_calls[-1]["action"] == "accept", "Answer pressed -> WhatsApp call accepted at once")
    session = cs._sessions["wacid.team.1"]
    await asyncio.sleep(0.8)
    browser.talk(MANAGER_HZ, 2.0)
    check(await phone.hears(MANAGER_HZ), "the customer hears the person in the website")
    phone.talk(CUSTOMER_HZ, 2.0)
    check(await browser.hears(CUSTOMER_HZ), "the website hears the customer")
    check(not any(t["who"] == "bot" for t in session.transcript), "the AI says nothing")
    row = await row_for("wacid.team.1")
    check(row.status == "with_team" and row.answered_by == "Kasun", "logged: answered by Kasun")
    other = await browser_offer()
    try:
        await cs.team_answer(tenant, row.id, other.pc.localDescription.sdp, "Ruwan")
        check(False, "second answer")
    except ValueError as e:
        check("Kasun" in str(e), "a second person pressing Answer is told Kasun already has it")
    await other.close()
    await asyncio.sleep(1.0)
    async with AsyncSessionLocal() as db:
        await cs.hang_up_call(db, tenant.id, row.id)
    check(api_calls[-1] == {"call_id": "wacid.team.1", "action": "terminate"}, "Hang up in the website ends the WhatsApp call")
    row = await row_for("wacid.team.1")
    check(row.status == "completed" and row.recording_url and row.recording_url.endswith(".ogg"), "the call is kept with a recording")
    name = row.recording_url.rsplit("/", 1)[1]
    with open(os.path.join(os.environ["MEDIA_DIR"], name), "rb") as f:
        left, right = channels(f.read())
    check(len(left) / 48_000 > 2.0, f"the recording has the whole call ({len(left) / 48_000:.1f} s)")
    check(abs(main_hz(left.tobytes()) - CUSTOMER_HZ) < 40 and abs(main_hz(right.tobytes()) - MANAGER_HZ) < 40,
          "left = the customer, right = the team member")
    await phone.close()
    await browser.close()

    print("\nNobody answers: the AI picks up")
    settings.CALL_TEAM_RING_SECONDS = 2
    phone, task = await ringing_call(tenant, "wacid.team.2")
    await connect_caller(phone, task)
    check(await phone.hears(BOT_HZ), "after the ring time the AI answers")
    session = cs._sessions["wacid.team.2"]
    check("may be recorded" in session.transcript[0]["text"], "and its greeting says the call may be recorded")

    print("\nTaking over a call the AI is on")
    row = await row_for("wacid.team.2")
    browser = await browser_offer()
    answer = await cs.team_answer(tenant, row.id, browser.pc.localDescription.sdp, "Kasun")
    await browser.pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
    await asyncio.sleep(0.8)
    browser.talk(MANAGER_HZ, 2.0)
    check(await phone.hears(MANAGER_HZ), "a person takes over mid-call")
    check(session.listener.paused, "the AI stops listening")
    print("\nThe website drops (closed tab, lost Wi-Fi)")
    await cs._agent_legs[row.id].finish()
    check(await wait_for(lambda: session.partner is None and not session.listener.paused, 3), "the AI takes the caller back")
    await session.hang_up()
    await phone.close()
    await browser.close()

    print("\nLet the AI answer")
    settings.CALL_TEAM_RING_SECONDS = 30
    phone, task = await ringing_call(tenant, "wacid.team.3")
    row = await row_for("wacid.team.3")
    t0 = time.monotonic()
    check(await cs.let_ai_answer(tenant, row.id), "'Let the AI answer' pressed")
    await connect_caller(phone, task)
    check(time.monotonic() - t0 < 3, "the AI picks up straight away, without the 30 s wait")
    await cs._sessions["wacid.team.3"].hang_up()
    await phone.close()

    print("\nThe caller hangs up while it rings")
    settings.CALL_TEAM_RING_SECONDS = 6
    phone, task = await ringing_call(tenant, "wacid.team.4")
    await cs.handle_call_events(tenant, {"calls": [{"id": "wacid.team.4", "event": "terminate", "status": "COMPLETED"}]})
    await asyncio.wait_for(task, 10)
    check(not any(b.get("action") == "accept" for b in api_calls), "it is never accepted")
    check((await row_for("wacid.team.4")).status == "missed", "logged as missed")
    await phone.close()

    print("\nThe team calls a customer from the website")
    settings.CALL_TEAM_RING_SECONDS = 0
    manager_phone["customer_out"] = None
    browser = await browser_offer()
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        result = await cs.team_dial(db, t, "+" + CUSTOMER, browser.pc.localDescription.sdp, "Kasun")
    check(result["status"] == "ringing" and result["sdp"], "it rings, and the website gets its audio answer")
    await browser.pc.setRemoteDescription(RTCSessionDescription(result["sdp"], "answer"))
    check(await wait_for(lambda: manager_phone.get("customer_out") is not None, 6), "the customer picks up")
    customer = manager_phone["customer_out"]
    session = next(v for v in cs._sessions.values() if v.call_row_id == result["call_id"])
    check(await wait_for(lambda: session.partner is not None, 6), "joined to the website")
    await asyncio.sleep(0.8)
    browser.talk(MANAGER_HZ, 5.0)
    check(await customer.hears(MANAGER_HZ), "the customer hears the team member")
    customer.talk(CUSTOMER_HZ, 5.0)
    check(await browser.hears(CUSTOMER_HZ), "and the team member hears them")
    check(not session.transcript, "no AI opening line")
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.direction == "outgoing" and row.status == "with_team" and row.answered_by == "Kasun", "logged as Kasun's outgoing call")
    await session.hang_up()
    await customer.close()
    await browser.close()

    print("\nThe team calls someone who hasn't allowed calls")
    permission.update({"status": "no_permission", "can_call": False, "can_ask": True})
    permission_requests.clear()
    n_connect = sum(1 for b in api_calls if b.get("action") == "connect")
    browser = await browser_offer()
    async with AsyncSessionLocal() as db:
        t = await db.get(Tenant, tenant.id)
        result = await cs.team_dial(db, t, CUSTOMER, browser.pc.localDescription.sdp, "Kasun")
    check(result["status"] == "awaiting_permission" and permission_requests, "asked to allow calls first")
    await cs.handle_permission_reply(tenant, CUSTOMER, {"response": "accept"})
    async with AsyncSessionLocal() as db:
        row = await db.get(Call, result["call_id"])
    check(row.status == "allowed" and sum(1 for b in api_calls if b.get("action") == "connect") == n_connect,
          "they accept -> marked 'allowed', not dialled without the person (they press Call again)")
    permission.update({"status": "permanent", "can_call": True, "can_ask": True})
    await browser.close()


async def test_portal(tenant):
    print("\nPortal")
    async with AsyncSessionLocal() as db:
        calls = await cs.list_calls(db, tenant.id)
    check(len(calls) >= 6 and {"incoming", "outgoing"} <= {c["direction"] for c in calls}, "calls listed, both directions")
    check(any(c["transferred"] for c in calls) and all("transcript" in c for c in calls), "with transcript and transfer flag")
    async with AsyncSessionLocal() as db:
        other = await cs.list_calls(db, tenant.id + 999)
    check(other == [], "another business sees none of them")

    print("\nCalls switched off")
    settings.CALLS_ENABLED = False
    api_calls.clear()
    phone = Phone()
    await phone.pc.setLocalDescription(await phone.pc.createOffer())
    await cs.handle_call_events(tenant, {"calls": [{"id": "wacid.off", "from": CUSTOMER, "event": "connect",
                                                    "direction": "USER_INITIATED", "session": {"sdp_type": "offer", "sdp": phone.pc.localDescription.sdp}}]})
    check(api_calls == [{"call_id": "wacid.off", "action": "reject"}], "an incoming call is rejected")
    settings.CALLS_ENABLED = True
    await phone.close()


async def main_async():
    settings.CALL_STUN_SERVER = ""          # local test: no internet needed
    settings.CALL_TEAM_RING_SECONDS = 0     # the AI-only checks; test_team rings the team
    cs.calls_api = fake_calls_api
    cs.call_permission = fake_permission
    cs.ask_permission = fake_ask
    cs._send_text = fake_text
    cs.ai_reply = fake_ai_reply
    cs.speech_pcm = fake_speech
    voice_service.understand_voice_note = fake_hear
    voice_service.business_words = fake_words
    llm_router.role_candidates = lambda role: []
    import realtime
    realtime.inbox_manager.broadcast = fake_broadcast

    await init_db()
    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="City Shop", whatsapp_phone_number_id="8080", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="Open 8 to 8.", human_handoff_number="+" + MANAGER)
        db.add(t)
        await db.commit()
        await db.refresh(t)
    await test_unit()
    await test_incoming(t)
    await test_transfer(t)
    await test_transfer_unanswered(t)
    await test_outgoing(t)
    await test_team(t)
    await test_portal(t)
    for s in list(cs._sessions.values()):
        await s.finish()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
