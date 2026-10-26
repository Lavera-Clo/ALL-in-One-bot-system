"""
WhatsApp voice calls: the AI answers, talks, and hands over to a person when it should.

    Customer calls the business's WhatsApp number
        -> Meta sends a `calls` webhook with an SDP offer
        -> we answer (pre_accept, accept) with our own WebRTC end (aiortc)
        -> the AI greets them, listens, and replies in speech: speech-to-text (voice_service),
           the same reply brain as chat (ai_service, with the knowledge base, products, memory),
           text-to-speech (voice_reply_service)
        -> complaint / refund / "can I talk to someone" -> the AI says one moment and calls the
           manager's WhatsApp; when they pick up the two calls are joined and the AI goes quiet.
           No answer -> the AI says the team will call back and the chat is marked "Needs you".

    Business calls a customer (portal "Call" button or POST /api/tenants/{id}/calls)
        -> WhatsApp only lets a business call someone who agreed to it, so if they haven't, a
           "may we call you?" request is sent first and the call is placed when they accept
        -> otherwise we send an SDP offer (connect); when they answer, the AI opens with the
           reason for the call and the conversation continues as above.

Every spoken turn is saved in the chat history (message_type "call") so the chat bot remembers
what was said on the phone, and each call gets a row in `calls` with the transcript and a summary.

Meta's limits: calling has to be switched on for the number, the business needs the 2,000-a-day
messaging tier, and business-initiated calls need the customer's permission (a few requests a week).
"""

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Dict, List, Optional

import httpx
import numpy as np
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from sqlalchemy import select

import llm_router
from call_audio import Listener, SpeakerTrack, recording_bytes, speech_pcm, split_speech, wav_bytes
from config import settings
from database import AsyncSessionLocal
from inbox_service import announce, bot_may_reply, get_conversation, is_manager, mark_needs_human, touch_conversation
from models import Call, ChatHistory, Tenant, utc_now
from reply_guards import needs_person

logger = logging.getLogger("uvicorn.error")

RING_MANAGER_SECONDS = 35
PERMISSION_WAIT_HOURS = 24

# "can I talk to a person", "manager", "කෙනෙක් එක්ක කතා කරන්න"
_WANTS_PERSON = re.compile(
    r"\b(talk|speak|connect|transfer|put me through)\b.{0,25}\b(person|human|someone|somebody|manager|owner|staff|team|agent|real)\b"
    r"|\b(real person|human being|manager please|call the manager)\b"
    r"|kenek ekka|manager ekka|owner ekka|මිනිහෙක්|කෙනෙක් එක්ක|මැනේජර්",
    re.I,
)
_GOODBYE = re.compile(r"^\W*(ok(ay)?\W+)?(bye|goodbye|thank(s| you)[\w ]{0,20}bye|that'?s all|nothing else|no thanks?|ස්තූතියි|බායි|bayi)\W*$", re.I)


def wants_person(text: str) -> bool:
    return bool(_WANTS_PERSON.search(text or ""))


def is_goodbye(text: str) -> bool:
    return bool(_GOODBYE.search((text or "").strip()))


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _sinhala(language: str) -> bool:
    return language in ("sinhala", "singlish")


LINES = {
    "greet": ("Hello, thank you for calling {business}. This call may be recorded. How can I help you?",
              "ආයුබෝවන්, {business} වෙත කතා කළාට ස්තූතියි. මෙම ඇමතුම පටිගත විය හැක. මම ඔබට උදව් කරන්නේ කොහොමද?"),
    "hold": ("One moment please, I'm connecting you to our team.",
             "පොඩ්ඩක් ඉන්න, මම ඔබව අපේ කණ්ඩායමට සම්බන්ධ කරනවා."),
    "callback": ("Sorry, our team can't take the call right now. I've passed your message on and they will call you back soon.",
                 "සමාවෙන්න, අපේ කණ්ඩායමට දැන් කතා කරන්න බැහැ. මම ඔබේ පණිවිඩය දුන්නා, ඔවුන් ඉක්මනින් ඔබට කතා කරයි."),
    "again": ("Sorry, I didn't catch that. Could you say it again?",
              "සමාවෙන්න, මට ඒක ඇහුණේ නැහැ. ආයෙත් කියන්න පුළුවන්ද?"),
    "back": ("You're back with the assistant. Is there anything else I can help with?",
             "ඔබ නැවත සහායකයා සමඟ. තව මොනවා හරි උදව්වක් ඕනද?"),
    "bye": ("Thank you for calling. Have a nice day!", "කතා කළාට ස්තූතියි. සුබ දවසක්!"),
    "too_long": ("I'm sorry, I have to end the call now. Please message us on WhatsApp if you need anything else.",
                 "සමාවෙන්න, මට දැන් ඇමතුම නවත්වන්න වෙනවා. තව මොනවා හරි ඕන නම් WhatsApp එකෙන් පණිවිඩයක් එවන්න."),
}


def line(name: str, language: str, business: str = "") -> str:
    english, sinhala = LINES[name]
    return (sinhala if _sinhala(language) else english).format(business=business or "us")


# ---------------------------------------------------------------------------
# Meta Graph API
# ---------------------------------------------------------------------------

def _mock(tenant: Tenant) -> bool:
    return "placeholder" in (tenant.meta_access_token or "")


def _url(tenant: Tenant, path: str) -> str:
    return f"https://graph.facebook.com/{settings.CALLS_GRAPH_API_VERSION}/{tenant.whatsapp_phone_number_id}/{path}"


async def calls_api(tenant: Tenant, body: dict) -> dict:
    """POST /{phone-number-id}/calls (connect, pre_accept, accept, reject, terminate)."""
    body = {"messaging_product": "whatsapp", **body}
    if _mock(tenant):
        logger.info(f"[MOCK CALLS API] {body.get('action')} {body.get('call_id') or body.get('to')}")
        return {"success": True, "calls": [{"id": f"wacid.mock.{body.get('to', '')}"}]}
    async with httpx.AsyncClient() as client:
        r = await client.post(_url(tenant, "calls"), json=body, timeout=15.0,
                              headers={"Authorization": f"Bearer {tenant.meta_access_token}"})
    if r.status_code >= 400:
        raise RuntimeError(f"Calls API {body.get('action')} failed: {r.status_code} {r.text[:300]}")
    return r.json()


async def call_permission(tenant: Tenant, phone: str) -> dict:
    """{'status': 'no_permission'|'temporary'|'permanent', 'can_call': bool, 'can_ask': bool}"""
    if _mock(tenant):
        return {"status": "permanent", "can_call": True, "can_ask": True}
    async with httpx.AsyncClient() as client:
        r = await client.get(_url(tenant, "call_permissions"), params={"user_wa_id": digits(phone)}, timeout=15.0,
                             headers={"Authorization": f"Bearer {tenant.meta_access_token}"})
    if r.status_code >= 400:
        raise RuntimeError(f"Could not check call permission: {r.status_code} {r.text[:300]}")
    data = r.json()
    actions = {a.get("action_name"): a.get("can_perform_action") for a in data.get("actions") or []}
    status = (data.get("permission") or {}).get("status", "no_permission")
    return {"status": status,
            "can_call": status in ("temporary", "permanent") and actions.get("start_call", True) is not False,
            "can_ask": actions.get("send_call_permission_request", True) is not False}


async def ask_permission(tenant: Tenant, phone: str, text: str) -> None:
    """The WhatsApp 'Allow calls' request. Their answer arrives as a call_permission_reply message."""
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(phone), "type": "interactive",
            "interactive": {"type": "call_permission_request", "action": {"name": "call_permission_request"},
                            "body": {"text": text[:1024]}}}
    if _mock(tenant):
        logger.info(f"[MOCK CALLS API] permission request to {phone}: {text}")
        return
    async with httpx.AsyncClient() as client:
        r = await client.post(_url(tenant, "messages"), json=body, timeout=15.0,
                              headers={"Authorization": f"Bearer {tenant.meta_access_token}"})
    if r.status_code >= 400:
        raise RuntimeError(f"Could not send the call permission request: {r.status_code} {r.text[:300]}")


async def _send_text(tenant: Tenant, phone: str, text: str) -> None:
    from whatsapp_client import send_whatsapp_message
    try:
        await send_whatsapp_message(tenant.whatsapp_phone_number_id, digits(phone), text, tenant.meta_access_token)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[call] could not message {phone}: {exc}")


# ---------------------------------------------------------------------------
# The AI's side of the conversation
# ---------------------------------------------------------------------------

async def ai_reply(tenant_id: int, phone: str, said: str, note: str = "") -> tuple:
    """(spoken reply, hand over?) - the same brain as chat, told this is a phone call."""
    from ai_service import generate_bot_reply, resolve_reply_documents
    from conversation_memory import build_context
    from inbox_service import get_conversation as _conv
    from order_service import get_dynamic_kb, load_catalog
    from tenant_config import build_bot_config
    from document_service import get_tenant_documents
    from voice_reply_service import build_spoken_reply
    from whatsapp_client import send_whatsapp_document

    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, tenant_id)
        history_rows = (await db.execute(
            select(ChatHistory).where(ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone)
            .order_by(ChatHistory.created_at.asc(), ChatHistory.id.asc()))).scalars().all()
        history = [{"role": r.role, "content": r.content} for r in history_rows]
        # The newest row is what they just said (saved before this is called): it goes in separately.
        if history and history[-1]["role"] == "user" and history[-1]["content"] == said:
            history = history[:-1]
        documents = await get_tenant_documents(db, tenant_id, active_only=True)
        config = build_bot_config(tenant, documents)
        config.conversation = build_context(await _conv(db, tenant_id, phone), history_rows,
                                            history_rows[-1].id if history_rows else 0)
        config.catalog = await load_catalog(db, tenant_id)
        kb = await get_dynamic_kb(tenant, db)
        message = ("[This is a live WhatsApp VOICE CALL. Answer the way you would speak on the phone: one or two "
                   "short sentences, no lists, no emojis, no links. "
                   + (f"Why this call was made: {note}. " if note else "")
                   + "]\n" + said)
        reply = await generate_bot_reply(system_prompt=tenant.system_prompt, knowledge_base=kb, chat_history=history,
                                         user_message=message, config=config, from_voice=True)
        text, docs = resolve_reply_documents(reply.text, said, config)
        if reply.unknown:
            # Learning: a question the documents don't cover goes on the owner's "Questions to
            # answer" list, as from chat; once answered, the next caller gets the answer.
            from ai_service import detect_language_intent
            from learning_service import record_gap
            await record_gap(db, tenant_id, said, bot_reply=text, language=detect_language_intent(said, history),
                             customer_phone=phone, source="call")
        for doc in docs:
            if doc and doc.get("url"):
                try:
                    await send_whatsapp_document(tenant.whatsapp_phone_number_id, phone, doc["url"], doc["filename"],
                                                 doc.get("caption", ""), tenant.meta_access_token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[call] document not sent: {exc}")
        spoken = await build_spoken_reply(text) if text else ""
        return spoken, bool(reply.handoff)


async def opening_line(business: str, purpose: str, language: str) -> str:
    """What the AI says first when the business calls a customer."""
    if llm_router.role_candidates("fast"):
        try:
            data, _ = await llm_router.complete_json("fast", [
                {"role": "system", "content": "Write the first sentence a friendly phone assistant says when calling a "
                 "customer on behalf of a business. Say the business name and the reason in one or two short spoken "
                 "sentences, then ask if it's a good time. Language: "
                 + ("Sinhala (Sinhala script)" if _sinhala(language) else "English")
                 + '. JSON only: {"line": "..."}'},
                {"role": "user", "content": f"Business: {business}\nReason for the call: {purpose}"}],
                max_tokens=150, temperature=0.3, timeout=15)
            if isinstance(data, dict) and str(data.get("line", "")).strip():
                return str(data["line"]).strip()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[call] opening line fallback: {exc}")
    if _sinhala(language):
        return f"ආයුබෝවන්, මේ {business} එකෙන් කතා කරන්නේ. {purpose}"
    return f"Hello, this is {business} calling. {purpose} Is now a good time?"


async def summarise(transcript: List[Dict]) -> str:
    said = "\n".join(f"{t['who']}: {t['text']}" for t in transcript)
    if not said:
        return ""
    if llm_router.role_candidates("fast"):
        try:
            data, _ = await llm_router.complete_json("fast", [
                {"role": "system", "content": 'Summarise this phone call for the business owner in one or two plain '
                 'English sentences: what the customer wanted and what is still to do. JSON only: {"summary": "..."}'},
                {"role": "user", "content": said[-6000:]}], max_tokens=200, temperature=0.0, timeout=20)
            if isinstance(data, dict) and data.get("summary"):
                return str(data["summary"]).strip()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[call] summary fallback: {exc}")
    customer = [t["text"] for t in transcript if t["who"] == "customer"]
    return ("Customer said: " + " / ".join(customer[-3:]))[:500] if customer else ""


# ---------------------------------------------------------------------------
# One live call
# ---------------------------------------------------------------------------

_sessions: Dict[str, "CallSession"] = {}
_agent_legs: Dict[int, "CallSession"] = {}      # call row id -> the website leg joined to it


def _rtc() -> RTCPeerConnection:
    servers = [RTCIceServer(urls=settings.CALL_STUN_SERVER)] if settings.CALL_STUN_SERVER else []
    return RTCPeerConnection(RTCConfiguration(iceServers=servers))


class CallSession:
    """
    One leg of a call. `role` is "customer" for the person the business is talking to, "manager" for
    the leg the AI rings to hand over; a manager leg is joined to its customer leg when answered.
    """

    def __init__(self, tenant: Tenant, phone: str, direction: str, call_row_id: int, role: str = "customer",
                 purpose: str = "", language: str = "english"):
        self.tenant_id = tenant.id
        self.business = tenant.business_name or ""
        self.phone = digits(phone)
        self.direction = direction
        self.call_row_id = call_row_id
        self.role = role
        self.purpose = purpose
        self.language = language
        self.wa_call_id: Optional[str] = None
        self.pc = _rtc()
        self.relay = MediaRelay()
        self.speaker = SpeakerTrack()
        self.pc.addTrack(self.speaker)
        self.remote = None                 # the caller's audio
        self.listener: Optional[Listener] = None
        self.tasks: List[asyncio.Task] = []
        self.transcript: List[Dict] = []
        self.turn_lock = asyncio.Lock()
        self.answered = asyncio.Event()
        self.ended = asyncio.Event()       # hung up (set first thing)
        self.logged = asyncio.Event()      # ...and the call log, summary and recording are saved
        self.partner: Optional["CallSession"] = None     # the other leg once joined
        self.waiting_for: Optional["CallSession"] = None  # manager leg -> the customer it is for
        self.handed_over = False
        self.missed_turns = 0
        self.hang_up_after_speaking = False
        # A person on the team in the website (role "agent" leg): who took the call, and for a call
        # the team placed, the browser leg waiting for the customer to pick up.
        self.taken_by: Optional[str] = None
        self.pending_agent: Optional["CallSession"] = None
        self.ring_decided = asyncio.Event()     # incoming: the team answered, or let the AI take it
        # Recording: the caller (left) and what they heard (right), 16 kHz, both 20 ms at a time.
        self.rec_in = bytearray()
        self.rec_out = bytearray()
        self.recording = role == "customer" and settings.CALL_RECORDING
        # The audio is also needed to learn from the part a person handled, even with recording off.
        self.capture = role == "customer" and (settings.CALL_RECORDING or settings.CALL_LEARNING)
        if self.capture:
            self.speaker.tap = self._record_out
        # Where a person (team member or manager) talked: [(start s, end s, transcript index, who)].
        self.team_spans: List[tuple] = []
        self._span: Optional[tuple] = None
        self.learning_task: Optional[asyncio.Task] = None

        @self.pc.on("track")
        def _on_track(track):
            if track.kind != "audio":
                return
            self.remote = track
            if self.role == "customer":
                self.listener = Listener(self.relay.subscribe(track), self.on_utterance, self.on_barge_in,
                                         on_audio=self._record_in if self.capture else None)
                self.tasks.append(asyncio.ensure_future(self.listener.run()))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            if self.pc.connectionState in ("failed", "closed"):
                await self.finish("failed" if self.pc.connectionState == "failed" and not self.answered.is_set() else None)

    # --- recording -----------------------------------------------------------

    def _room(self) -> bool:
        return len(self.rec_in) < settings.CALL_MAX_MINUTES * 60 * 16_000 * 2 + 640_000

    def _record_in(self, pcm16k: bytes) -> None:
        if self.answered.is_set() and self._room():
            self.rec_in.extend(pcm16k)

    def _record_out(self, pcm48k: bytes) -> None:
        if self.answered.is_set() and self._room():
            self.rec_out.extend(np.frombuffer(pcm48k, dtype=np.int16)[::3].tobytes())

    async def _save_recording(self) -> Optional[str]:
        if not self.recording or len(self.rec_in) + len(self.rec_out) < 16_000:   # under ~0.5 s: nothing to keep
            return None
        try:
            from media_service import save_file
            audio = await asyncio.to_thread(recording_bytes, bytes(self.rec_in), bytes(self.rec_out))
            return save_file(audio, "audio/ogg", "call.ogg") if audio else None
        except Exception:  # noqa: BLE001 - a lost recording must not lose the call log
            logger.exception("[call] could not save the recording")
            return None

    # --- speaking ------------------------------------------------------------

    async def say(self, text: str, record: bool = True) -> None:
        if not text or self.ended.is_set():
            return
        if record:
            self.transcript.append({"who": "bot", "text": text})
            await self._save_turn("assistant", "bot", text)
        pcm = await speech_pcm(text)
        if pcm:
            self.speaker.say(pcm)

    async def wait_until_quiet(self, limit: float = 30.0) -> None:
        for _ in range(int(limit / 0.1)):
            if not self.speaker.talking:
                return
            await asyncio.sleep(0.1)

    def on_barge_in(self) -> None:
        # They started talking over the bot: stop and listen (as a person would).
        if self.speaker.talking and not self.hang_up_after_speaking:
            self.speaker.stop_talking()

    # --- listening -------------------------------------------------------------

    async def on_utterance(self, pcm: bytes) -> None:
        if self.ended.is_set() or self.handed_over:
            return
        async with self.turn_lock:
            from voice_service import business_words, understand_voice_note
            async with AsyncSessionLocal() as db:
                tenant = await db.get(Tenant, self.tenant_id)
                words = await business_words(db, tenant)
            heard = await understand_voice_note(wav_bytes(pcm), "audio/wav", self.business, words)
            if heard.status != "ok" or not heard.text.strip() or heard.unclear:
                if heard.status == "ok" or heard.text.strip():
                    self.missed_turns += 1
                    if self.missed_turns <= 2:
                        await self.say(line("again", self.language), record=False)
                return
            self.missed_turns = 0
            said = heard.text.strip()
            if heard.language in ("english", "sinhala", "singlish"):
                self.language = heard.language
            self.transcript.append({"who": "customer", "text": said})
            await self._save_turn("user", "customer", said)

            if wants_person(said) or needs_person(heard.english or said, ""):
                await self.hand_over(said)
                return
            reply, handoff = await ai_reply(self.tenant_id, self.phone, said, self.purpose)
            await self.say(reply)
            if handoff:
                await self.wait_until_quiet()
                await self.hand_over(said)
            elif is_goodbye(heard.english or said):
                self.hang_up_after_speaking = True
                await self.wait_until_quiet()
                await self.hang_up()

    async def _save_turn(self, role: str, sender: str, text: str) -> None:
        if self.role != "customer":
            return
        try:
            async with AsyncSessionLocal() as db:
                conv = await touch_conversation(db, self.tenant_id, self.phone)
                db.add(ChatHistory(tenant_id=self.tenant_id, customer_phone_number=self.phone, role=role,
                                   content=("📞 " + text), sender=sender, message_type="call",
                                   status="RECEIVED" if role == "user" else "SENT"))
                conv.last_message_at = utc_now()
                conv.message_count = (conv.message_count or 0) + 1
                await db.commit()
            await announce(self.tenant_id, self.phone, incoming=role == "user")
        except Exception:  # noqa: BLE001 - a lost log line must not drop the call
            logger.exception("[call] could not save a call turn")

    # --- handing over to a person ------------------------------------------------

    async def hand_over(self, reason: str) -> None:
        if self.handed_over or self.ended.is_set():
            return
        self.handed_over = True
        await self.say(line("hold", self.language))
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, self.tenant_id)
            await mark_needs_human(db, self.tenant_id, self.phone)
            await _set_row(db, self.call_row_id, status="transferring", transferred=True)
            manager = digits(tenant.human_handoff_number or "")
        joined = False
        if manager and manager != self.phone:
            await _send_text(tenant, manager, f"📞 Transferring a call from +{self.phone} to you now.\n"
                                              f"They said: \"{reason[:300]}\"")
            joined = await self._ring_manager(tenant, manager)
        if joined:
            return
        await self.say(line("callback", self.language))
        if manager:
            await _send_text(tenant, manager, f"📞 Missed transfer: please call +{self.phone} back.\n"
                                              f"They said: \"{reason[:300]}\"")
        self.handed_over = False        # the AI keeps helping until they hang up

    async def _ring_manager(self, tenant: Tenant, manager: str) -> bool:
        try:
            permission = await call_permission(tenant, manager)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[call] {exc}")
            return False
        if not permission["can_call"]:
            if permission["can_ask"]:
                try:
                    await ask_permission(tenant, manager, "Allow calls so the assistant can put customers through to you.")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[call] {exc}")
            return False
        try:
            leg = await dial(tenant, manager, role="manager", for_session=self)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[call] could not ring the manager: {exc}")
            return False
        try:
            await asyncio.wait_for(leg.answered.wait(), timeout=RING_MANAGER_SECONDS)
        except asyncio.TimeoutError:
            await leg.hang_up()
            return False
        if leg.ended.is_set() or self.ended.is_set():
            return False
        self.join(leg)
        async with AsyncSessionLocal() as db:
            await _set_row(db, self.call_row_id, status="with_team")
        return True

    def _recorded_seconds(self) -> float:
        return len(self.rec_out) / 2 / 16_000

    def _close_span(self) -> None:
        if self._span is not None:
            start, index, who = self._span
            self.team_spans.append((start, self._recorded_seconds(), index, who))
            self._span = None

    def join(self, leg: "CallSession") -> None:
        """Customer <-> manager: each hears the other; the AI stops listening."""
        if self.role == "customer" and self._span is None:
            who = self.taken_by or ("Manager" if leg.role == "manager" else "Team")
            self._span = (self._recorded_seconds(), len(self.transcript), who)
        self.partner, leg.partner = leg, self
        if self.listener:
            self.listener.paused = True
        self.speaker.stop_talking()
        if leg.remote is not None:
            self.speaker.forward = leg.relay.subscribe(leg.remote)
        if self.remote is not None:
            leg.speaker.forward = self.relay.subscribe(self.remote)

    async def join_team(self, leg: "CallSession", who: str) -> None:
        """A person in the website takes this call (answered it ringing, took it over from the AI, or placed it)."""
        self.handed_over = True
        self.taken_by = who
        self._span = None if self._span is None else (self._span[0], self._span[1], who)
        self.join(leg)
        async with AsyncSessionLocal() as db:
            await _set_row(db, self.call_row_id, status="with_team", answered_by=who)

    async def unjoin(self) -> None:
        """The manager hung up but the customer is still there: the AI picks up again."""
        self._close_span()
        self.partner = None
        self.speaker.forward = None
        self.handed_over = False
        if self.listener:
            self.listener.paused = False
        async with AsyncSessionLocal() as db:
            await _set_row(db, self.call_row_id, status="in_progress")
        await self.say(line("back", self.language), record=False)

    # --- starting and ending -----------------------------------------------------

    async def started(self) -> None:
        """They picked up (or we picked up): say the first line, start the clock."""
        self.answered.set()
        async with AsyncSessionLocal() as db:
            await _set_row(db, self.call_row_id, status="with_team" if self.partner else "in_progress",
                           started_at=utc_now())
        self.tasks.append(asyncio.ensure_future(self._time_limit()))
        if self.role == "manager":
            return
        if self.pending_agent is not None:
            # The team placed this call from the website: they talk, not the AI.
            leg, self.pending_agent = self.pending_agent, None
            if not leg.ended.is_set():
                await self.join_team(leg, self.taken_by or "Team")
                return
        if self.taken_by:
            return                         # a person answered it ringing: already joined
        if self.direction == "outgoing":
            await self.say(await opening_line(self.business, self.purpose, self.language))
        else:
            async with AsyncSessionLocal() as db:
                tenant = await db.get(Tenant, self.tenant_id)
                ai_allowed = bot_may_reply(tenant, await get_conversation(db, self.tenant_id, self.phone))
            if ai_allowed:
                await self.say(line("greet", self.language, self.business))
            else:
                # The bot is switched off for this business or this chat: straight to a person.
                await self.hand_over("(called while the assistant is switched off)")

    async def _time_limit(self) -> None:
        await asyncio.sleep(settings.CALL_MAX_MINUTES * 60)
        if not self.ended.is_set() and self.partner is None:
            await self.say(line("too_long", self.language), record=False)
            await self.wait_until_quiet()
            await self.hang_up()

    async def hang_up(self) -> None:
        if self.ended.is_set():
            return
        if self.wa_call_id:
            async with AsyncSessionLocal() as db:
                tenant = await db.get(Tenant, self.tenant_id)
            try:
                await calls_api(tenant, {"call_id": self.wa_call_id, "action": "terminate"})
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[call] terminate: {exc}")
        await self.finish()

    async def finish(self, status: Optional[str] = None) -> None:
        """Clean up and log. Safe to call more than once (Meta's terminate webhook, our own hang-up)."""
        if self.ended.is_set():
            return
        self.ended.set()
        for task in self.tasks:
            task.cancel()
        try:
            await self.pc.close()
        except Exception:  # noqa: BLE001
            pass
        if self.wa_call_id:
            _sessions.pop(self.wa_call_id, None)

        if self.pending_agent is not None:          # the team rang, nobody picked up
            await self.pending_agent.finish()
            self.pending_agent = None
        if self.role == "customer":
            _agent_legs.pop(self.call_row_id, None)
        partner, self.partner = self.partner, None
        if partner is not None and not partner.ended.is_set():
            if partner.role == "customer":
                await partner.unjoin()
            else:
                await partner.hang_up()
        if self.role in ("manager", "agent"):
            self.logged.set()
            return

        self._close_span()
        recording = await self._save_recording()
        final = status or ("completed" if self.answered.is_set() else "missed")
        summary = await summarise(self.transcript)
        async with AsyncSessionLocal() as db:
            row = await db.get(Call, self.call_row_id)
            if row is not None:
                row.status = final
                row.ended_at = utc_now()
                if row.started_at:
                    row.duration_seconds = int((row.ended_at - _aware(row.started_at)).total_seconds())
                row.transcript = json.dumps(self.transcript, ensure_ascii=False)
                row.summary = summary
                row.recording_url = recording
                await db.commit()
        if settings.CALL_LEARNING and self.team_spans:
            # After the reply to Meta: transcribe the person's part and learn from it.
            self.learning_task = asyncio.ensure_future(learn_from_call(
                self.tenant_id, self.call_row_id, self.phone, bytes(self.rec_in), bytes(self.rec_out),
                list(self.team_spans), list(self.transcript), self.language))
        self.logged.set()
        await announce(self.tenant_id, self.phone, kind="calls.changed")


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _set_row(db, row_id: int, **fields) -> None:
    row = await db.get(Call, row_id)
    if row is None:
        return
    for k, v in fields.items():
        setattr(row, k, v)
    await db.commit()
    await announce(row.tenant_id, row.customer_phone, kind="calls.changed")


async def _language_of(db, tenant_id: int, phone: str) -> str:
    from ai_service import detect_language_intent
    rows = (await db.execute(select(ChatHistory.content).where(
        ChatHistory.tenant_id == tenant_id, ChatHistory.customer_phone_number == phone, ChatHistory.role == "user")
        .order_by(ChatHistory.id.desc()).limit(4))).scalars().all()
    return detect_language_intent(" ".join(reversed(rows))) if rows else "english"


# ---------------------------------------------------------------------------
# Learning from calls a person handled
# ---------------------------------------------------------------------------
#
# The AI's own turns are written down as it speaks; when a team member or the manager takes over,
# nobody writes anything. Afterwards, the recording of that part is cut into what each side said
# and transcribed, so (1) the call's transcript and the chat history are complete - the chat bot
# knows what the team promised - and (2) general facts the person gave ("we open 8 to 9", "delivery
# to Kandy is Rs 450") become SUGGESTED answers on the owner's "Questions to answer" list. They are
# used only after the owner checks and saves them: a person on the phone can be wrong, or say
# something meant for this one customer only, and the bot must not repeat that to everyone.

LEARN_PROMPT = (
    "Below is a phone call between a customer and a staff member of a business. Find questions the customer "
    "asked that the staff member answered with a GENERAL fact about the business that would help answer other "
    "customers too: opening hours, prices, delivery areas and fees, payment methods, policies, what products or "
    "services exist. Leave out anything about this customer only (their order, booking, address, name, phone, "
    "complaint), promises, and anything the staff member was unsure about. Write each question as a customer "
    "would ask it and the answer as a short factual sentence, in English. "
    'JSON only: {"learned": [{"question": "...", "answer": "..."}]} - an empty list if there is nothing.'
)
_PERSONAL = re.compile(r"\+?\d[\d\s-]{7,}\d|@[a-z0-9-]+\.[a-z]|\b(order|booking)\s*(no\.?|number|#)\s*\w+", re.I)
MAX_LEARNED = 5
MAX_PIECES = 60


async def _transcribe_pieces(pieces: List[tuple], business: str, words: List[str]) -> List[tuple]:
    """[(start, pcm)] -> [(start, text)], a few at a time."""
    from voice_service import understand_voice_note
    gate = asyncio.Semaphore(3)

    async def one(start, pcm):
        async with gate:
            heard = await understand_voice_note(wav_bytes(pcm), "audio/wav", business, words)
        return (start, heard.text.strip()) if heard.status == "ok" and heard.text.strip() else None

    done = await asyncio.gather(*(one(st, pcm) for st, pcm in pieces[:MAX_PIECES]))
    return [d for d in done if d]


async def learn_from_call(tenant_id: int, call_row_id: int, phone: str, rec_in: bytes, rec_out: bytes,
                          spans: List[tuple], transcript: List[Dict], language: str) -> List[dict]:
    """Transcribe the part(s) a person handled; complete the transcript and history; suggest answers."""
    from learning_service import suggest
    from voice_service import business_words
    learned: List[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            words = await business_words(db, tenant)
        business = tenant.business_name or ""
        bps = 2 * 16_000                                      # bytes per second, 16 kHz s16 mono
        added: List[tuple] = []                               # (transcript index, [turns])
        for start, end, index, who in spans:
            a, b = int(start * bps) & ~1, int(end * bps) & ~1
            customer = [(start + t, "customer", pcm) for t, pcm in split_speech(rec_in[a:b])]
            person = [(start + t, "team", pcm) for t, pcm in split_speech(rec_out[a:b])]
            pieces = sorted(customer + person, key=lambda x: x[0])
            heard = await _transcribe_pieces([(t, pcm) for t, _, pcm in pieces], business, words)
            side = {t: w for t, w, _ in pieces}
            turns = [{"who": side[t], "text": text, "by": who if side[t] == "team" else None}
                     for t, text in sorted(heard)]
            if turns:
                added.append((index, turns))
        if not added:
            return []

        full = list(transcript)
        for index, turns in sorted(added, key=lambda x: x[0], reverse=True):
            full[index:index] = turns
        async with AsyncSessionLocal() as db:
            conv = await touch_conversation(db, tenant_id, phone)
            for _, turns in sorted(added, key=lambda x: x[0]):
                for t in turns:
                    agent = t["who"] == "team"
                    db.add(ChatHistory(tenant_id=tenant_id, customer_phone_number=phone,
                                       role="assistant" if agent else "user", content="📞 " + t["text"],
                                       sender="agent" if agent else "customer", message_type="call",
                                       status="SENT" if agent else "RECEIVED"))
                    conv.message_count = (conv.message_count or 0) + 1
            row = await db.get(Call, call_row_id)
            if row is not None:
                row.transcript = json.dumps(full, ensure_ascii=False)
                row.summary = await summarise(full) or row.summary
            await db.commit()
        await announce(tenant_id, phone, kind="calls.changed")
        await announce(tenant_id, phone)

        if not llm_router.role_candidates("fast"):
            return []
        said = "\n".join(f"{'Staff' if t['who'] != 'customer' else 'Customer'}: {t['text']}" for t in full)
        data, _ = await llm_router.complete_json("fast", [{"role": "system", "content": LEARN_PROMPT},
                                                          {"role": "user", "content": said[-8000:]}],
                                                 max_tokens=500, temperature=0.0, timeout=30)
        for item in ((data or {}).get("learned") or [])[:MAX_LEARNED] if isinstance(data, dict) else []:
            q, ans = str((item or {}).get("question", "")).strip(), str((item or {}).get("answer", "")).strip()
            if not q or not ans or _PERSONAL.search(q + " " + ans):
                continue                                      # about this customer only: not for everyone
            async with AsyncSessionLocal() as db:
                gap = await suggest(db, tenant_id, q, ans, language=language, customer_phone=phone, source="call")
            if gap is not None:
                learned.append({"question": q, "answer": ans, "gap_id": gap.id})
        if learned:
            from realtime import inbox_manager
            try:
                await inbox_manager.broadcast(tenant_id, {"type": "learning.suggested", "count": len(learned)})
            except Exception:  # noqa: BLE001
                pass
        return learned
    except Exception:  # noqa: BLE001 - learning must never break anything else
        logger.exception("[call] learning from the call failed")
        return learned


# ---------------------------------------------------------------------------
# Placing a call
# ---------------------------------------------------------------------------

async def dial(tenant: Tenant, phone: str, role: str = "customer", purpose: str = "", call_row_id: Optional[int] = None,
               for_session: Optional[CallSession] = None) -> CallSession:
    """Send the SDP offer (connect). The answer arrives as a `connect` webhook, pick-up as an ACCEPTED status."""
    async with AsyncSessionLocal() as db:
        if call_row_id is None:
            row = Call(tenant_id=tenant.id, customer_phone=digits(phone), direction="outgoing", status="ringing",
                       purpose=purpose or ("Transfer from the assistant" if role == "manager" else None))
            db.add(row)
            await db.commit()
            call_row_id = row.id
        else:
            await _set_row(db, call_row_id, status="ringing")
        language = await _language_of(db, tenant.id, digits(phone))
    session = CallSession(tenant, phone, "outgoing", call_row_id, role=role, purpose=purpose, language=language)
    session.waiting_for = for_session
    offer = await session.pc.createOffer()
    await session.pc.setLocalDescription(offer)        # aiortc gathers ICE candidates here
    try:
        res = await calls_api(tenant, {"to": digits(phone), "action": "connect",
                                       "session": {"sdp_type": "offer", "sdp": session.pc.localDescription.sdp},
                                       "biz_opaque_callback_data": f"call:{call_row_id}"})
    except Exception:
        await session.finish("failed")
        raise
    call_id = ((res.get("calls") or [{}])[0]).get("id")
    if not call_id:
        await session.finish("failed")
        raise RuntimeError(f"WhatsApp did not return a call id: {res}")
    session.wa_call_id = call_id
    _sessions[call_id] = session
    async with AsyncSessionLocal() as db:
        await _set_row(db, call_row_id, wa_call_id=call_id)
    return session


async def start_outgoing_call(db, tenant: Tenant, phone: str, purpose: str) -> dict:
    """The portal's "Call" button. Asks for permission first when WhatsApp needs it."""
    if not settings.CALLS_ENABLED:
        raise ValueError("Calls are switched off on this server (CALLS_ENABLED).")
    phone = digits(phone)
    if not phone:
        raise ValueError("A phone number is needed.")
    permission = await call_permission(tenant, phone)
    if permission["can_call"]:
        session = await dial(tenant, phone, purpose=purpose)
        return {"status": "ringing", "call_id": session.call_row_id}
    if not permission["can_ask"]:
        raise ValueError("This customer hasn't allowed calls, and WhatsApp's limit on asking them again is reached. "
                         "Try again in a few days, or ask them in chat to call you.")
    row = Call(tenant_id=tenant.id, customer_phone=phone, direction="outgoing", status="awaiting_permission", purpose=purpose)
    db.add(row)
    await db.commit()
    language = await _language_of(db, tenant.id, phone)
    text = (f"{tenant.business_name} would like to call you: {purpose}" if not _sinhala(language)
            else f"{tenant.business_name} ඔබට කතා කරන්න කැමතියි: {purpose}")
    await ask_permission(tenant, phone, text)
    await announce(tenant.id, phone, kind="calls.changed")
    return {"status": "awaiting_permission", "call_id": row.id,
            "message": "The customer was asked to allow calls. The call starts as soon as they accept."}


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

async def handle_call_events(tenant: Tenant, value: dict) -> None:
    """`calls` webhook: connect (incoming offer or the answer to our offer) and terminate."""
    names = {c.get("wa_id"): ((c.get("profile") or {}).get("name")) for c in value.get("contacts") or []}
    for event in value.get("calls") or []:
        try:
            kind = event.get("event")
            call_id = event.get("id")
            if kind == "connect" and event.get("direction") == "USER_INITIATED":
                await _incoming(tenant, event, names.get(event.get("from")))
            elif kind == "connect":
                session = _sessions.get(call_id)
                sdp = (event.get("session") or {}).get("sdp")
                if session and sdp:
                    await session.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
            elif kind == "terminate":
                session = _sessions.get(call_id)
                if session:
                    await session.finish("failed" if event.get("status") == "FAILED" and not session.answered.is_set() else None)
                else:
                    await _close_row(tenant, call_id, event)
        except Exception:  # noqa: BLE001
            logger.exception(f"[call] could not handle call event {event.get('event')}")


async def handle_call_statuses(tenant: Tenant, statuses: list) -> None:
    """RINGING / ACCEPTED / REJECTED for calls we placed."""
    for status in statuses:
        session = _sessions.get(status.get("id"))
        if session is None:
            continue
        state = (status.get("status") or "").upper()
        if state == "ACCEPTED" and not session.answered.is_set():
            if session.role == "manager":
                session.answered.set()
            else:
                asyncio.ensure_future(session.started())
        elif state == "REJECTED":
            await session.finish("rejected")


async def handle_permission_reply(tenant: Tenant, phone: str, reply: dict) -> None:
    """They answered "may we call you?": accepted -> place the waiting call; declined -> mark it."""
    accepted = (reply.get("response") or "").lower() == "accept"
    async with AsyncSessionLocal() as db:
        since = utc_now() - timedelta(hours=PERMISSION_WAIT_HOURS)
        rows = (await db.execute(select(Call).where(
            Call.tenant_id == tenant.id, Call.customer_phone == digits(phone),
            Call.status == "awaiting_permission", Call.created_at >= since).order_by(Call.id.desc()))).scalars().all()
        if not rows:
            return
        newest, older = rows[0], rows[1:]
        for row in older:
            row.status = "failed"
        if not accepted:
            newest.status = "rejected"
        elif not newest.purpose:
            # A call the team placed from the website: tell them, they press Call again (they must be there).
            newest.status = "allowed"
        await db.commit()
        await announce(tenant.id, phone, kind="calls.changed")
        purpose, row_id = newest.purpose or "", newest.id
    if accepted and purpose:
        try:
            await dial(tenant, phone, purpose=purpose, call_row_id=row_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[call] could not place the accepted call: {exc}")


async def _incoming(tenant: Tenant, event: dict, name: Optional[str]) -> None:
    phone = digits(event.get("from") or "")
    offer = (event.get("session") or {}).get("sdp")
    call_id = event.get("id")
    if not call_id or not offer or call_id in _sessions:
        return
    if not settings.CALLS_ENABLED:
        await calls_api(tenant, {"call_id": call_id, "action": "reject"})
        return
    async with AsyncSessionLocal() as db:
        await touch_conversation(db, tenant.id, phone, name)
        row = Call(tenant_id=tenant.id, wa_call_id=call_id, customer_phone=phone, direction="incoming", status="ringing")
        db.add(row)
        await db.commit()
        language = await _language_of(db, tenant.id, phone)
        row_id = row.id
    await announce(tenant.id, phone, kind="calls.changed", incoming=True)

    session = CallSession(tenant, phone, "incoming", row_id, language=language)
    session.wa_call_id = call_id
    _sessions[call_id] = session
    try:
        await session.pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        answer = await session.pc.createAnswer()
        await session.pc.setLocalDescription(answer)
        sdp = session.pc.localDescription.sdp
        await calls_api(tenant, {"call_id": call_id, "action": "pre_accept", "session": {"sdp_type": "answer", "sdp": sdp}})
        # The team's websites ring first; whoever presses Answer takes it. Nobody within
        # CALL_TEAM_RING_SECONDS (or "Let the AI answer") -> the AI picks up.
        if settings.CALL_TEAM_RING_SECONDS > 0:
            await ring_team(tenant.id, row_id, phone, name)
            try:
                await asyncio.wait_for(session.ring_decided.wait(), timeout=settings.CALL_TEAM_RING_SECONDS)
            except asyncio.TimeoutError:
                pass
            session.ring_decided.set()
            if session.ended.is_set():
                return                      # they hung up while it rang
        await calls_api(tenant, {"call_id": call_id, "action": "accept", "session": {"sdp_type": "answer", "sdp": sdp},
                                 "biz_opaque_callback_data": f"call:{row_id}"})
    except Exception:
        logger.exception("[call] could not answer")
        await session.finish("failed")
        return
    asyncio.ensure_future(session.started())


async def _close_row(tenant: Tenant, call_id: str, event: dict) -> None:
    """A terminate for a call with no live session (server restarted, or never answered)."""
    if not call_id:
        return
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(Call).where(Call.tenant_id == tenant.id, Call.wa_call_id == call_id))).scalar_one_or_none()
        if row is None or row.ended_at:
            return
        row.status = "completed" if event.get("duration") else "missed"
        row.ended_at = utc_now()
        if event.get("duration"):
            row.duration_seconds = int(event["duration"])
        await db.commit()
    await announce(tenant.id, row.customer_phone, kind="calls.changed")


# ---------------------------------------------------------------------------
# The team, talking from the website (Business Sender, cloai.site)
# ---------------------------------------------------------------------------
#
# The browser makes its own WebRTC connection to this server (microphone and speaker); this server
# joins it to the WhatsApp call exactly as it joins the manager's phone: each side hears the other.
# The browser sends its SDP offer once its ICE candidates are gathered, and gets the answer back.

async def ring_team(tenant_id: int, call_row_id: int, phone: str, name: Optional[str]) -> None:
    from realtime import inbox_manager
    try:
        await inbox_manager.broadcast(tenant_id, {"type": "call.ringing", "call_id": call_row_id, "phone": phone,
                                                  "name": name or "", "seconds": settings.CALL_TEAM_RING_SECONDS})
    except Exception:  # noqa: BLE001
        logger.exception("[call] could not ring the team")


def _customer_session(call_row_id: int) -> Optional[CallSession]:
    return next((s for s in _sessions.values()
                 if s.call_row_id == call_row_id and s.role == "customer" and not s.ended.is_set()), None)


async def _browser_leg(tenant: Tenant, phone: str, call_row_id: int, offer_sdp: str) -> CallSession:
    leg = CallSession(tenant, phone, "browser", call_row_id, role="agent")
    try:
        await leg.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        await leg.pc.setLocalDescription(await leg.pc.createAnswer())
    except Exception as exc:
        await leg.finish()
        raise ValueError(f"The browser's audio offer wasn't usable: {exc}")
    leg.answered.set()
    return leg


async def team_answer(tenant: Tenant, call_row_id: int, offer_sdp: str, who: str) -> str:
    """Answer a ringing call, or take over a call the AI is on. Returns the browser's SDP answer."""
    session = _customer_session(call_row_id)
    if session is None or session.tenant_id != tenant.id:
        raise LookupError("This call has ended.")
    if session.partner is not None:
        raise ValueError(f"{session.taken_by or 'Someone'} is already on this call.")
    leg = await _browser_leg(tenant, session.phone, call_row_id, offer_sdp)
    session.ring_decided.set()
    session.speaker.stop_talking()
    await session.join_team(leg, who)
    _agent_legs[call_row_id] = leg
    return leg.pc.localDescription.sdp


async def let_ai_answer(tenant: Tenant, call_row_id: int) -> bool:
    session = _customer_session(call_row_id)
    if session is None or session.tenant_id != tenant.id:
        return False
    session.ring_decided.set()
    return True


async def team_dial(db, tenant: Tenant, phone: str, offer_sdp: str, who: str) -> dict:
    """Call a customer from the website; the person talks (no AI). Asks for permission first when needed."""
    if not settings.CALLS_ENABLED:
        raise ValueError("Calls are switched off on this server (CALLS_ENABLED).")
    phone = digits(phone)
    if not phone:
        raise ValueError("A phone number is needed.")
    permission = await call_permission(tenant, phone)
    if not permission["can_call"]:
        if not permission["can_ask"]:
            raise ValueError("This customer hasn't allowed calls, and WhatsApp's limit on asking again is reached. "
                             "Ask them in chat to call you instead.")
        row = Call(tenant_id=tenant.id, customer_phone=phone, direction="outgoing", status="awaiting_permission",
                   answered_by=who)
        db.add(row)
        await db.commit()
        await ask_permission(tenant, phone, f"{tenant.business_name} would like to call you on WhatsApp.")
        await announce(tenant.id, phone, kind="calls.changed")
        return {"status": "awaiting_permission", "call_id": row.id,
                "message": "They were asked to allow calls. When they accept, press Call again."}
    async with AsyncSessionLocal() as s2:
        row = Call(tenant_id=tenant.id, customer_phone=phone, direction="outgoing", status="ringing", answered_by=who)
        s2.add(row)
        await s2.commit()
        row_id = row.id
    leg = await _browser_leg(tenant, phone, row_id, offer_sdp)
    try:
        session = await dial(tenant, phone, call_row_id=row_id)
    except Exception:
        await leg.finish()
        raise
    session.taken_by = who
    session.pending_agent = leg
    _agent_legs[row_id] = leg
    return {"status": "ringing", "call_id": row_id, "sdp": leg.pc.localDescription.sdp}


# ---------------------------------------------------------------------------
# Portal
# ---------------------------------------------------------------------------

def payload(row: Call) -> dict:
    try:
        transcript = json.loads(row.transcript) if row.transcript else []
    except ValueError:
        transcript = []
    return {"id": row.id, "phone": row.customer_phone, "direction": row.direction, "status": row.status,
            "purpose": row.purpose, "summary": row.summary, "transferred": row.transferred,
            "duration_seconds": row.duration_seconds, "transcript": transcript,
            "recording_url": row.recording_url, "answered_by": row.answered_by,
            "live": bool(row.wa_call_id and row.wa_call_id in _sessions),
            # Always with the time zone: SQLite hands back naive UTC, which a browser would read as local time.
            "started_at": _aware(row.started_at).isoformat() if row.started_at else None,
            "ended_at": _aware(row.ended_at).isoformat() if row.ended_at else None,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


async def list_calls(db, tenant_id: int, phone: Optional[str] = None, limit: int = 100) -> List[dict]:
    q = select(Call).where(Call.tenant_id == tenant_id)
    if phone:
        q = q.where(Call.customer_phone == digits(phone))
    rows = (await db.execute(q.order_by(Call.id.desc()).limit(limit))).scalars().all()
    return [payload(r) for r in rows]


async def hang_up_call(db, tenant_id: int, call_row_id: int) -> bool:
    row = await db.get(Call, call_row_id)
    if row is None or row.tenant_id != tenant_id:
        return False
    session = _sessions.get(row.wa_call_id or "")
    if session:
        await session.hang_up()
    elif row.status == "awaiting_permission":
        row.status = "failed"
        await db.commit()
    return True
