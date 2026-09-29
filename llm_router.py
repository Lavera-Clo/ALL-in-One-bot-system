"""
Multi-provider LLM router.

WHY THIS EXISTS
---------------
Every free AI tier is unreliable on its own. Measured against this project's keys on
2026-09-29:

    Gemini      flash works; pro models return 429 "quota"; flash itself often 503 "high demand"
    Groq        fast and reliable, but small per-minute token caps
    Mistral     key is valid but has 0 requests/minute until a plan is activated
    OpenRouter  free models work, some 429, some restricted to other apps

So nothing calls a provider directly. Callers name a JOB ("chat", "reader", "extract",
"judge", "fast"). Each job has an ordered list of provider:model candidates. The router
tries them in order, retries transient failures once, and remembers failures for a while
(a "circuit breaker") so it doesn't waste time on a provider that just said no.

Model names change often (gemini-2.5-flash was already retired when this was written),
so every list can be overridden from .env without touching code:

    LLM_CHAT_MODELS=groq:openai/gpt-oss-120b,gemini:gemini-flash-latest
"""

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
import openai
from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@dataclass
class ProviderSpec:
    name: str
    base_url: str
    key_settings: Tuple[str, ...]      # names of settings fields holding keys, tried in order
    max_concurrency: int = 3
    min_interval: float = 0.0          # seconds between request starts (Mistral free tier ~1 rps)


PROVIDERS: Dict[str, ProviderSpec] = {
    "groq": ProviderSpec("groq", "https://api.groq.com/openai/v1", ("GROQ_API_KEY",), max_concurrency=4),
    "gemini": ProviderSpec("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", ("GEMINI_API_KEY",), max_concurrency=3),
    "openrouter": ProviderSpec("openrouter", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"), max_concurrency=2),
    "mistral": ProviderSpec("mistral", "https://api.mistral.ai/v1", ("MISTRAL_API_KEY",), max_concurrency=1, min_interval=1.2),
    "openai": ProviderSpec("openai", "https://api.openai.com/v1", ("OPENAI_API_KEY",), max_concurrency=4),
}

# Ordered candidates per job. First working one wins (or, for ensembles, the first N
# working ones from DIFFERENT providers, so the models genuinely cross-check each other).
DEFAULT_ROLES: Dict[str, List[str]] = {
    # Customer replies on WhatsApp: fastest first. Groq limits each MODEL separately (its big
    # model allows ~2 full replies/minute on the free tier), so the chain alternates providers
    # and keeps the slow-but-dependable OpenRouter model as the last resort.
    "chat": [
        "groq:openai/gpt-oss-120b",
        "gemini:gemini-flash-lite-latest",
        "groq:qwen/qwen3.8-27b",
        "gemini:gemini-flash-latest",
        "mistral:mistral-small-latest",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "groq:openai/gpt-oss-20b",
        "openai:gpt-4o-mini",
    ],
    # Reading PDFs as images: handles scanned pages and keeps tables as tables.
    "reader": [
        "gemini:gemini-flash-latest",
        "gemini:gemini-flash-lite-latest",
        "gemini:gemini-3.1-flash-lite",
        "mistral:mistral-ocr-latest",
    ],
    # Independent fact extraction; run as an ensemble across providers.
    "extract": [
        "groq:openai/gpt-oss-120b",
        "gemini:gemini-flash-latest",
        "gemini:gemini-flash-lite-latest",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "mistral:mistral-medium-latest",
        "openrouter:dots-studio/dots-3-note-preview:free",
        "groq:qwen/qwen3.8-27b",
        "openai:gpt-4o",
    ],
    # Merging drafts, fixing unverified numbers, writing the FAQ. Strongest reasoning first.
    "judge": [
        "groq:openai/gpt-oss-120b",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "gemini:gemini-flash-latest",
        "mistral:mistral-medium-latest",
        "openai:gpt-4o",
    ],
    # Small jobs: titles, keywords, captions.
    "fast": [
        "groq:openai/gpt-oss-20b",
        "gemini:gemini-flash-lite-latest",
        "groq:qwen/qwen3.8-27b",
        "openrouter:inclusionai/ling-3.0-flash-sante:free",
        "openai:gpt-4o-mini",
    ],
}

AUDIO_MODELS = ["groq:whisper-large-v3-turbo", "groq:whisper-large-v3", "openai:whisper-1"]


class LLMUnavailable(Exception):
    """Every candidate for a job failed. The message lists why, for the logs and the portal."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency: float
    attempts: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def _key_value(setting_name: str) -> str:
    value = (getattr(settings, setting_name, "") or "").strip()
    if not value or "placeholder" in value.lower() or value.startswith("your_"):
        return ""
    return value


def configured_keys(provider: str) -> List[str]:
    spec = PROVIDERS.get(provider)
    if not spec:
        return []
    return [k for k in (_key_value(name) for name in spec.key_settings) if k]


def role_candidates(role: str) -> List[Tuple[str, str]]:
    """(provider, model) pairs for a job, from .env override or defaults, keys-present only."""
    override = (getattr(settings, f"LLM_{role.upper()}_MODELS", "") or "").strip()
    raw = [c.strip() for c in override.split(",") if c.strip()] if override else DEFAULT_ROLES.get(role, [])
    candidates = []
    for item in raw:
        provider, _, model = item.partition(":")   # model ids may themselves contain ':' (":free")
        if provider in PROVIDERS and model and configured_keys(provider):
            candidates.append((provider, model))
    return candidates


def any_provider_configured() -> bool:
    return any(configured_keys(p) for p in PROVIDERS)


# ---------------------------------------------------------------------------
# Circuit breaker + stats (in-memory; resets on restart, which is fine)
# ---------------------------------------------------------------------------

_breakers: Dict[str, Tuple[float, str]] = {}          # key -> (until_timestamp, reason)
_stats: Dict[str, Dict] = {}
_semaphores: Dict[str, asyncio.Semaphore] = {}
_interval_locks: Dict[str, asyncio.Lock] = {}
_last_start: Dict[str, float] = {}
_clients: Dict[Tuple[str, str], AsyncOpenAI] = {}


def _trip(key: str, seconds: float, reason: str) -> None:
    _breakers[key] = (time.time() + seconds, reason)
    logger.warning(f"[llm] pausing {key} for {int(seconds)}s: {reason}")


def _blocked(key: str) -> Optional[str]:
    entry = _breakers.get(key)
    if not entry:
        return None
    until, reason = entry
    if time.time() >= until:
        _breakers.pop(key, None)
        return None
    return reason


def _record(provider: str, model: str, ok: bool, latency: float = 0.0, error: str = "") -> None:
    s = _stats.setdefault(f"{provider}/{model}", {"ok": 0, "fail": 0, "last_error": "", "last_latency": None})
    if ok:
        s["ok"] += 1
        s["last_latency"] = round(latency, 2)
    else:
        s["fail"] += 1
        s["last_error"] = error[:200]


def _semaphore(provider: str) -> asyncio.Semaphore:
    if provider not in _semaphores:
        _semaphores[provider] = asyncio.Semaphore(PROVIDERS[provider].max_concurrency)
    return _semaphores[provider]


async def _respect_interval(provider: str) -> None:
    gap = PROVIDERS[provider].min_interval
    if not gap:
        return
    lock = _interval_locks.setdefault(provider, asyncio.Lock())
    async with lock:
        wait = _last_start.get(provider, 0) + gap - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_start[provider] = time.time()


def _client(provider: str, key: str) -> AsyncOpenAI:
    cache_key = (provider, key[-8:])
    if cache_key not in _clients:
        _clients[cache_key] = AsyncOpenAI(base_url=PROVIDERS[provider].base_url, api_key=key, max_retries=0)
    return _clients[cache_key]


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def _classify(exc: Exception) -> Tuple[str, float, str]:
    """
    Decide what a failure means. Returns (scope, pause_seconds, reason) where scope is:
      'retry'    transient — wait briefly, try this model once more
      'model'    this model is unusable for a while — skip to next candidate
      'key'      this API key is unusable (auth / zero quota) — try the provider's next key
      'request'  this particular request is too big for this model — skip, don't pause
    """
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, httpx.TimeoutException, httpx.TransportError)):
        return "retry", 0, "timeout / connection error"

    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    body = str(exc)[:400]
    headers = {}
    response = getattr(exc, "response", None)
    if response is not None:
        headers = {k.lower(): v for k, v in response.headers.items()}

    if status == 429:
        if headers.get("x-ratelimit-limit-req-minute") == "0" or headers.get("x-ratelimit-limit-requests") == "0":
            return "key", 1800, "this key has zero quota (plan not activated?)"
        if "quota" in body.lower():
            return "model", 1800, "quota exhausted for this model"
        retry_after = headers.get("retry-after")
        try:
            wait = min(float(retry_after), 10.0) if retry_after else 4.0
        except ValueError:
            wait = 4.0
        return "retry", wait, "rate limited"
    if status in (500, 502, 503, 504):
        return "retry", 3.0, f"provider error {status} (overloaded)"
    if status == 413 or "too large" in body.lower() or "context length" in body.lower() or "maximum context" in body.lower():
        return "request", 0, "request too large for this model"
    if status in (401, 403):
        return "key", 3600, f"auth refused ({status})"
    if status == 402:
        return "key", 3600, "out of credits (402)"
    if status in (400, 404, 422):
        return "model", 3600, f"model rejected request ({status}): {body[:120]}"
    return "model", 120, f"unexpected error: {body[:120]}"


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------

async def _chat_once(provider: str, model: str, key: str, messages: List[Dict], max_tokens: int,
                     temperature: float, timeout: float) -> str:
    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
    if provider == "groq" and model.startswith("openai/gpt-oss"):
        # Groq's gpt-oss models reason before answering; keep that from eating the token budget.
        kwargs["reasoning_effort"] = "low" if max_tokens <= 2000 else "medium"
    async with _semaphore(provider):
        await _respect_interval(provider)
        response = await _client(provider, key).chat.completions.create(**kwargs)
    text = _clean(response.choices[0].message.content if response.choices else "")
    if not text:
        finish = response.choices[0].finish_reason if response.choices else "none"
        raise ValueError(f"empty response (finish_reason={finish})")
    return text


async def _run_candidate(provider: str, model: str, call, attempts: List[str],
                         patient: bool = True) -> Optional[LLMResult]:
    """
    Run `call(key)` for one provider:model, across that provider's keys, with one retry on
    transient errors. Shared by chat, PDF reading and anything else that talks to a provider.

    patient=False (customer chat): don't sleep and retry on a rate limit or overload — pause
    this model and move straight on to the next candidate. Training can afford to wait; a
    customer on WhatsApp can't.
    """
    label = f"{provider}/{model}"
    reason = _blocked(label)
    if reason:
        attempts.append(f"{label}: skipped ({reason})")
        return None

    for key_index, key in enumerate(configured_keys(provider)):
        key_id = f"{provider}#key{key_index + 1}"
        reason = _blocked(key_id)
        if reason:
            attempts.append(f"{key_id}: skipped ({reason})")
            continue
        for attempt in range(2):
            started = time.time()
            try:
                text = await call(key)
                latency = time.time() - started
                _record(provider, model, True, latency)
                return LLMResult(text=text, provider=provider, model=model, latency=latency, attempts=attempts)
            except ValueError as exc:            # empty/blocked output — model misbehaved for this input
                _record(provider, model, False, error=str(exc))
                attempts.append(f"{label}: {exc}")
                return None
            except Exception as exc:  # noqa: BLE001 — provider SDKs raise many types
                scope, pause, reason = _classify(exc)
                _record(provider, model, False, error=reason)
                attempts.append(f"{label}: {reason}")
                if scope == "retry" and attempt == 0 and patient:
                    await asyncio.sleep(pause)
                    continue
                if scope == "retry":
                    _trip(label, 60 if patient else 30, reason)
                    return None
                if scope == "key":
                    _trip(key_id, pause, reason)
                    break                         # try this provider's next key
                if scope == "model":
                    _trip(label, pause, reason)
                return None                       # 'request' (too big for this model): no pause
    return None


async def _try_candidate(provider: str, model: str, messages: List[Dict], max_tokens: int,
                         temperature: float, timeout: float, attempts: List[str],
                         patient: bool = True) -> Optional[LLMResult]:
    return await _run_candidate(
        provider, model,
        lambda key: _chat_once(provider, model, key, messages, max_tokens, temperature, timeout),
        attempts,
        patient,
    )


async def complete(role: str, messages: List[Dict], *, max_tokens: int = 1500, temperature: float = 0.3,
                   timeout: float = 90.0, exclude_providers: Optional[set] = None,
                   only_providers: Optional[set] = None, patient: Optional[bool] = None) -> LLMResult:
    """Run a job on the first candidate that works. Chat defaults to impatient (see _run_candidate)."""
    if patient is None:
        patient = role != "chat"
    attempts: List[str] = []
    for provider, model in role_candidates(role):
        if exclude_providers and provider in exclude_providers:
            continue
        if only_providers and provider not in only_providers:
            continue
        result = await _try_candidate(provider, model, messages, max_tokens, temperature, timeout, attempts, patient)
        if result:
            if len(attempts) > 0:
                logger.info(f"[llm] {role}: served by {result.label} after {len(attempts)} failed attempt(s)")
            return result
    raise LLMUnavailable(f"No AI provider could handle '{role}'. Tried: " + "; ".join(attempts[-8:]) if attempts
                         else f"No AI provider is configured for '{role}'. Add a key to .env.")


async def ensemble(role: str, messages: List[Dict], *, n: int = 3, max_tokens: int = 6000,
                   temperature: float = 0.2, timeout: float = 150.0) -> List[LLMResult]:
    """
    Ask up to `n` models from DIFFERENT providers the same question, in parallel.

    Different providers means different training data and different blind spots, which is
    the whole point: a fact one model misses, another usually catches. If a provider's first
    model fails, its later candidates in the list are tried before giving up on it.
    """
    by_provider: Dict[str, List[str]] = {}
    for provider, model in role_candidates(role):
        by_provider.setdefault(provider, []).append(model)
    providers = list(by_provider.keys())[: max(n, 1) + 1]   # one spare provider in case one is down

    async def run_provider(provider: str) -> Optional[LLMResult]:
        attempts: List[str] = []
        for model in by_provider[provider]:
            result = await _try_candidate(provider, model, messages, max_tokens, temperature, timeout, attempts)
            if result:
                return result
        return None

    results = [r for r in await asyncio.gather(*(run_provider(p) for p in providers)) if r]
    return results[:n]


def parse_json(text: str):
    """Pull the first JSON object/array out of a model reply (models love wrapping it in prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model reply")


async def complete_json(role: str, messages: List[Dict], **kwargs):
    """Like complete(), but moves on to the next candidate if a model returns unparseable JSON."""
    tried: set = set()
    last_error = ""
    for _ in range(3):
        result = await complete(role, messages, exclude_providers=tried or None, **kwargs)
        try:
            return parse_json(result.text), result
        except ValueError as exc:
            last_error = str(exc)
            tried.add(result.provider)
    raise LLMUnavailable(f"No model returned valid JSON for '{role}': {last_error}")


# ---------------------------------------------------------------------------
# Reading PDFs as images (scanned pages, tables)
# ---------------------------------------------------------------------------

READ_PROMPT = (
    "Transcribe ALL text in this document exactly, in reading order.\n"
    "- Render every table as a markdown table WITH its column headers. If a header spans "
    "sub-columns (e.g. a guest count split into two halls), repeat the parent header in each "
    "sub-column name, like '100 Pax - Grand Ballroom', so every cell is unambiguous.\n"
    "- Keep every number, price, date and phone number EXACTLY as printed. Never round, "
    "never convert currency, never fix apparent typos.\n"
    "- Mark each page with a line '### Page N'.\n"
    "- Output only the transcription, no commentary."
)


async def _gemini_read(model: str, key: str, pdf_bytes: bytes, timeout: float) -> str:
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
            {"text": READ_PROMPT},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 16384},
    }
    async with _semaphore("gemini"):
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json=payload,
            )
    r.raise_for_status()
    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError(f"no output (blocked: {data.get('promptFeedback', {}).get('blockReason', 'unknown')})")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise ValueError(f"empty transcription (finishReason={candidates[0].get('finishReason')})")
    return text.strip()


async def _mistral_ocr(model: str, key: str, pdf_bytes: bytes, timeout: float) -> str:
    payload = {
        "model": model,
        "document": {"type": "document_url",
                     "document_url": "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()},
    }
    async with _semaphore("mistral"):
        await _respect_interval("mistral")
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.post("https://api.mistral.ai/v1/ocr", headers={"Authorization": f"Bearer {key}"}, json=payload)
    r.raise_for_status()
    pages = r.json().get("pages", [])
    text = "\n\n".join(f"### Page {i + 1}\n{p.get('markdown', '')}" for i, p in enumerate(pages))
    if not text.strip():
        raise ValueError("empty OCR result")
    return text.strip()


async def read_pdf(pdf_bytes: bytes, *, exclude_models: Optional[set] = None, timeout: float = 180.0) -> LLMResult:
    """Transcribe a (small) PDF with a vision model. Callers split big PDFs into page batches."""
    readers = {"gemini": _gemini_read, "mistral": _mistral_ocr}
    attempts: List[str] = []
    for provider, model in role_candidates("reader"):
        if provider not in readers or (exclude_models and f"{provider}/{model}" in exclude_models):
            continue
        reader = readers[provider]
        result = await _run_candidate(
            provider, model,
            lambda key, reader=reader, model=model: reader(model, key, pdf_bytes, timeout),
            attempts,
        )
        if result:
            return result
    raise LLMUnavailable("No vision model could read this PDF. Tried: " + "; ".join(attempts[-6:]) if attempts
                         else "No vision-capable AI provider is configured (add GEMINI_API_KEY).")


# ---------------------------------------------------------------------------
# Voice notes
# ---------------------------------------------------------------------------

async def transcribe_audio(audio_bytes: bytes, filename: str = "voice_note.ogg", prompt: str = "") -> str:
    """Speech-to-text for WhatsApp voice notes. Groq Whisper first, OpenAI Whisper as backup."""
    for item in AUDIO_MODELS:
        provider, _, model = item.partition(":")
        for key in configured_keys(provider):
            if _blocked(f"{provider}/{model}"):
                break
            try:
                started = time.time()
                async with _semaphore(provider):
                    result = await _client(provider, key).audio.transcriptions.create(
                        model=model, file=(filename, audio_bytes), prompt=prompt or None, timeout=60)
                _record(provider, model, True, time.time() - started)
                return (result.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                scope, pause, reason = _classify(exc)
                _record(provider, model, False, error=reason)
                logger.warning(f"[llm] transcription via {provider}/{model} failed: {reason}")
                if scope in ("model", "key"):
                    _trip(f"{provider}/{model}", pause, reason)
                    break
    return ""


# ---------------------------------------------------------------------------
# Status for the portal
# ---------------------------------------------------------------------------

def status() -> Dict:
    now = time.time()
    providers = []
    for name, spec in PROVIDERS.items():
        keys = configured_keys(name)
        if not keys:
            continue
        paused = [
            {"what": k, "reason": reason, "seconds_left": int(until - now)}
            for k, (until, reason) in _breakers.items()
            if (k.startswith(f"{name}/") or k.startswith(f"{name}#")) and until > now
        ]
        providers.append({"name": name, "keys": len(keys), "paused": paused})
    return {
        "providers": providers,
        "roles": {role: [f"{p}/{m}" for p, m in role_candidates(role)] for role in DEFAULT_ROLES},
        "stats": _stats,
    }
