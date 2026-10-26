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
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from sqlalchemy import select

import llm_router
from call_audio import Listener, SpeakerTrack, speech_pcm, wav_bytes
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
    "greet": ("Hello, thank you for calling {business}. How can I help you?",
              "ආයුබෝවන්, {business} වෙත කතා කළාට ස්තූතියි. මම ඔබට උදව් කරන්නේ කොහොමද?"),
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
        self.ended = asyncio.Event()
        self.partner: Optional["CallSession"] = None     # the other leg once joined
        self.waiting_for: Optional["CallSession"] = None  # manager leg -> the customer it is for
        self.handed_over = False
        self.missed_turns = 0
        self.hang_up_after_speaking = False

        @self.pc.on("track")
        def _on_track(track):
            if track.kind != "audio":
                return
            self.remote = track
            if self.role == "customer":
                self.listener = Listener(self.relay.subscribe(track), self.on_utterance, self.on_barge_in)
                self.tasks.append(asyncio.ensure_future(self.listener.run()))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            if self.pc.connectionState in ("failed", "closed"):
                await self.finish("failed" if self.pc.connectionState == "failed" and not self.answered.is_set() else None)

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

    def join(self, leg: "CallSession") -> None:
        """Customer <-> manager: each hears the other; the AI stops listening."""
        self.partner, leg.partner = leg, self
        if self.listener:
            self.listener.paused = True
        self.speaker.stop_talking()
        if leg.remote is not None:
            self.speaker.forward = leg.relay.subscribe(leg.remote)
        if self.remote is not None:
            leg.speaker.forward = self.relay.subscribe(self.remote)

    async def unjoin(self) -> None:
        """The manager hung up but the customer is still there: the AI picks up again."""
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
            await _set_row(db, self.call_row_id, status="in_progress", started_at=utc_now())
        self.tasks.append(asyncio.ensure_future(self._time_limit()))
        if self.role == "manager":
            return
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

        partner, self.partner = self.partner, None
        if partner is not None and not partner.ended.is_set():
            if partner.role == "customer":
                await partner.unjoin()
            else:
                await partner.hang_up()
        if self.role == "manager":
            return

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
                await db.commit()
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
        await db.commit()
        await announce(tenant.id, phone, kind="calls.changed")
        purpose, row_id = newest.purpose or "", newest.id
    if accepted:
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
            "live": bool(row.wa_call_id and row.wa_call_id in _sessions),
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}


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
