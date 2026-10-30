"""
The sound side of WhatsApp calls (call_service.py uses it).

    SpeakerTrack   what the caller hears: speech the bot queued (48 kHz PCM), or silence, or - once
                   the call is joined to the manager - the manager's voice
    Listener       what the caller says: cuts their audio into utterances by loudness (they stop
                   talking for ~0.8 s = they finished), and notices them talking over the bot
    speech_pcm()   text -> edge-tts -> 48 kHz mono PCM ready for SpeakerTrack
    wav_bytes()    PCM -> a WAV file the speech-to-text step accepts
"""

import asyncio
import fractions
import io
import logging
import time
import wave
from typing import Awaitable, Callable, Optional

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

logger = logging.getLogger("uvicorn.error")

RATE = 48_000              # WebRTC / Opus
FRAME = 960                # 20 ms at 48 kHz
LISTEN_RATE = 16_000       # what speech-to-text wants
FRAME_SECONDS = FRAME / RATE

# A call is live: sound that arrives late is worth less than sound now. When frames pile up (the
# server briefly couldn't keep up, a burst after a network hiccup), the oldest are dropped instead of
# played late - otherwise the delay grows for the rest of the call and never comes back.
MAX_FORWARD_BACKLOG = 3        # frames (60 ms) waiting from the other side before old ones are dropped
MAX_LISTEN_BACKLOG = 25        # frames (0.5 s) of the caller waiting to be heard by the AI


def drop_backlog(track, keep: int) -> int:
    """Drop all but the newest `keep` frames queued on a relayed track. Returns how many were dropped."""
    queue = getattr(track, "_queue", None)
    dropped = 0
    while queue is not None and queue.qsize() > keep:
        try:
            frame = queue.get_nowait()
        except Exception:  # noqa: BLE001
            break
        if frame is None:                       # end of stream: keep it for recv() to see
            queue.put_nowait(None)
            break
        dropped += 1
    return dropped


class SpeakerTrack(MediaStreamTrack):
    """Outgoing audio. Plays queued PCM in real time; silence when there's nothing to say."""
    kind = "audio"

    def __init__(self, live: bool = True):
        super().__init__()
        # Nothing is sent until the call is answered (open()). Audio sent to WhatsApp while the
        # phone is still ringing is held and played once they pick up - so everything said after it
        # arrived seconds late for the whole call (measured live: ~3 s, team -> customer only).
        self.live = asyncio.Event()
        if live:
            self.live.set()
        self._buffer = bytearray()          # s16le mono 48 kHz
        self._pts = 0
        self._start: Optional[float] = None
        self.forward: Optional[MediaStreamTrack] = None   # set when bridged to another call
        self.tap: Optional[Callable[[bytes], None]] = None  # sees every 20 ms sent (call recording)
        # Diagnostics (call_service logs them): frames sent, and frames that came from the other leg.
        self.sent_frames = 0
        self.forwarded_frames = 0
        self.dropped_frames = 0    # late frames from the other side, dropped to keep the delay low
        self._from_forward = ToMono(RATE)

    def say(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)

    def stop_talking(self) -> None:
        self._buffer.clear()

    @property
    def talking(self) -> bool:
        return len(self._buffer) > 0

    def seconds_queued(self) -> float:
        return len(self._buffer) / 2 / RATE

    def open(self) -> None:
        """The call was answered: start sending, from now (no backlog)."""
        if not self.live.is_set():
            if self.forward is not None:
                drop_backlog(self.forward, 0)
            self._start = None
            self.live.set()

    async def recv(self) -> av.AudioFrame:
        if not self.live.is_set():
            await self.live.wait()
        if self._start is None:
            # The clock starts now; the frame count carries on, so timestamps never go backwards.
            self._start = time.monotonic() - self._pts / RATE
        # Pace frames to the clock: WebRTC expects one 20 ms frame every 20 ms.
        wait = self._start + self._pts / RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        pcm = None
        if self.forward is not None and not self._buffer:
            self.dropped_frames += drop_backlog(self.forward, MAX_FORWARD_BACKLOG)
            try:
                # A short wait: if their next frame isn't here yet, a moment of silence beats a delay.
                other = await asyncio.wait_for(self.forward.recv(), timeout=0.03)
                pcm = self._from_forward(other)
                self.forwarded_frames += 1
            except (asyncio.TimeoutError, MediaStreamError):
                pcm = None
        if pcm is None:
            need = FRAME * 2
            chunk = bytes(self._buffer[:need])
            del self._buffer[:need]
            pcm = chunk + b"\x00" * (need - len(chunk))
        samples = len(pcm) // 2
        self.sent_frames += 1
        if self.tap is not None:
            self.tap(pcm)
        frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        self._pts += samples
        return frame


class ToMono:
    """
    Decoded frames of ONE audio stream -> s16le mono PCM at `rate`.

    One per stream, never shared: a resampler keeps a few milliseconds of sound between frames, so
    one shared by two calls mixed each caller's voice into the other's call (a second caller heard
    as the first, the first caller's words answered on the second call).
    """

    def __init__(self, rate: int):
        self.rate = rate
        self._resampler = None
        self._key = None

    def __call__(self, frame: av.AudioFrame) -> bytes:
        key = (frame.format.name, frame.layout.name, frame.sample_rate)
        if self._resampler is None or key != self._key:
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.rate)
            self._key = key
        out = b""
        for f in self._resampler.resample(frame):
            out += f.to_ndarray().tobytes()
        return out


def loudness(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class Listener:
    """
    Reads the caller's audio and calls `on_utterance(pcm16k)` each time they finish a sentence.
    `on_barge_in()` fires when they start talking while the bot is still speaking.
    """
    END_SILENCE = 0.6          # seconds of quiet that end an utterance (longer = slower replies)
    MIN_SPEECH = 0.35          # shorter than this is a cough or a click
    MAX_UTTERANCE = 20.0

    def __init__(self, track: MediaStreamTrack, on_utterance: Callable[[bytes], Awaitable[None]],
                 on_barge_in: Callable[[], None] = lambda: None,
                 on_audio: Optional[Callable[[bytes], None]] = None):
        self.track = track
        self.on_audio = on_audio   # every 20 ms heard, 16 kHz (call recording), paused or not
        self._to_listen = ToMono(LISTEN_RATE)
        self.on_utterance = on_utterance
        self.on_barge_in = on_barge_in
        self.paused = False        # while bridged to a person the AI stops listening
        self.frames = 0            # frames received from the caller (diagnostics)
        self.dropped = 0           # frames dropped because the server fell behind
        self.loud_frames = 0       # ...of which had sound in them
        self._noise = 300.0

    def _threshold(self) -> float:
        return max(600.0, self._noise * 3)

    async def run(self) -> None:
        speech = bytearray()
        loud_frames = quiet_time = speech_time = 0.0
        in_speech = False
        while True:
            self.dropped += drop_backlog(self.track, MAX_LISTEN_BACKLOG)
            try:
                frame = await self.track.recv()
            except MediaStreamError:
                return
            pcm = self._to_listen(frame)
            self.frames += 1
            if loudness(pcm) > 300:
                self.loud_frames += 1
            if self.on_audio is not None:
                self.on_audio(pcm)
            if self.paused:
                speech.clear()
                in_speech = False
                continue
            seconds = len(pcm) / 2 / LISTEN_RATE
            level = loudness(pcm)
            loud = level > self._threshold()
            if not in_speech:
                if loud:
                    loud_frames += 1
                    speech.extend(pcm)
                    if loud_frames >= 3:                 # ~60 ms of voice: they started talking
                        in_speech, quiet_time, speech_time = True, 0.0, seconds * loud_frames
                        self.on_barge_in()
                else:
                    loud_frames = 0
                    speech.clear()
                    self._noise = 0.95 * self._noise + 0.05 * level   # follow the room's background noise
                continue
            speech.extend(pcm)
            speech_time += seconds
            quiet_time = 0.0 if loud else quiet_time + seconds
            if quiet_time >= self.END_SILENCE or speech_time >= self.MAX_UTTERANCE:
                said = bytes(speech)
                speech.clear()
                in_speech, loud_frames = False, 0
                if speech_time - quiet_time >= self.MIN_SPEECH:
                    asyncio.ensure_future(self.on_utterance(said))


def wav_bytes(pcm: bytes, rate: int = LISTEN_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def decode_to_pcm(audio: bytes, rate: int = RATE) -> bytes:
    """MP3/OGG/WAV bytes -> s16le mono PCM at `rate`."""
    out = b""
    with av.open(io.BytesIO(audio)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                out += f.to_ndarray().tobytes()
        for f in resampler.resample(None):
            out += f.to_ndarray().tobytes()
    return out


async def speech_pcm(text: str) -> bytes:
    """Spoken text as 48 kHz PCM, in the voice the voice-note replies use. b"" if speech can't be made."""
    from voice_reply_service import synthesize_voice_note

    audio, _mime = await synthesize_voice_note(text)
    if not audio:
        return b""
    try:
        return await asyncio.to_thread(decode_to_pcm, audio)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[call] could not decode speech: {exc}")
        return b""


def recording_bytes(caller: bytes, business: bytes, rate: int = LISTEN_RATE) -> bytes:
    """
    One call as an OGG/Opus file: the caller on the left, the business (AI or a person) on the right.
    Both sides were taken 20 ms at a time in real time, so they line up; the shorter is padded.
    """
    a = np.frombuffer(caller, dtype=np.int16)
    b = np.frombuffer(business, dtype=np.int16)
    n = max(a.size, b.size)
    if n == 0:
        return b""
    stereo = np.zeros((n, 2), dtype=np.int16)
    stereo[:a.size, 0] = a
    stereo[:b.size, 1] = b
    out = io.BytesIO()
    with av.open(out, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48_000, layout="stereo")
        stream.bit_rate = 32_000
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48_000)
        step = rate // 50 * 10                     # 200 ms per block
        for i in range(0, n, step):
            frame = av.AudioFrame.from_ndarray(stereo[i:i + step].reshape(1, -1), format="s16", layout="stereo")
            frame.sample_rate = rate
            for f in resampler.resample(frame):
                for packet in stream.encode(f):
                    container.mux(packet)
        for f in resampler.resample(None):
            for packet in stream.encode(f):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


def split_speech(pcm: bytes, rate: int = LISTEN_RATE, end_silence: float = 0.7, min_speech: float = 0.4,
                 max_len: float = 25.0) -> list:
    """
    One side of a recorded call cut into what was said, by loudness: [(start_seconds, pcm), ...].
    Used after the call to transcribe the part a person handled (call_service.learn_from_call).
    """
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    step = rate // 50                                   # 20 ms
    if a.size < step:
        return []
    levels = np.sqrt(np.mean(a[: a.size // step * step].reshape(-1, step) ** 2, axis=1))
    noise = float(np.percentile(levels, 20)) if levels.size else 0.0
    threshold = max(600.0, noise * 3)
    out, start, quiet = [], None, 0
    for i, level in enumerate(levels):
        loud = level > threshold
        if start is None:
            if loud:
                start, quiet = i, 0
            continue
        quiet = 0 if loud else quiet + 1
        length = (i - start + 1) * 0.02
        if quiet * 0.02 >= end_silence or length >= max_len or i == len(levels) - 1:
            end = i + 1 - (quiet if quiet * 0.02 >= end_silence else 0)
            if (end - start) * 0.02 >= min_speech:
                out.append((start * 0.02, a[start * step:end * step].astype(np.int16).tobytes()))
            start = None
    return out
