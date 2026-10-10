"""The one Sarvam client: rate limit, retries, disk cache, cost log.

Every response is cached under data/cache/<purpose>/, so re-runs cost nothing and the demo
can run offline. Every real request writes an api_call row.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import random
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sarvamai import AsyncSarvamAI
from sarvamai.core.api_error import ApiError

from app import config, db

logger = logging.getLogger(__name__)

CHAT_TIMEOUT_S = 120
RETRY_DELAYS_S = (2.0, 8.0, 30.0)
RETRYABLE_STATUS = {429, 503}

# The call being worked on. Set by the pipeline so every Sarvam request is costed to its call.
current_call: ContextVar[str | None] = ContextVar("current_call", default=None)

# INR per 1M tokens, and per audio hour with diarization.
LLM_INPUT_INR = 29.28
LLM_OUTPUT_INR = 73.20
STT_DIARIZED_INR_PER_HOUR = 45.0


def llm_cost_inr(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * LLM_INPUT_INR + completion_tokens * LLM_OUTPUT_INR) / 1_000_000


def stt_cost_inr(audio_seconds: float) -> float:
    return audio_seconds / 3600.0 * STT_DIARIZED_INR_PER_HOUR


class RateLimiter:
    """Token bucket: `rpm` per minute, refilled continuously, burst of 10 seconds' worth."""

    def __init__(self, rpm: int) -> None:
        self.rate = rpm / 60.0
        self.capacity = max(1.0, rpm / 6.0)
        self._tokens = self.capacity
        self._ts: float | None = None

    def try_acquire(self, tokens: int = 1, now: float | None = None) -> float:
        """Take `tokens` if available. Returns 0 on success, else seconds to wait."""
        now = time.monotonic() if now is None else now
        if self._ts is not None:
            self._tokens = min(self.capacity, self._tokens + max(0.0, now - self._ts) * self.rate)
        self._ts = now
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        return (tokens - self._tokens) / self.rate

    async def acquire(self, tokens: int = 1) -> None:
        while (wait := self.try_acquire(tokens)) > 0:
            await asyncio.sleep(wait)


class SarvamNotConfigured(RuntimeError):
    pass


class ChatTruncated(RuntimeError):
    """The answer hit max_tokens or came back empty (e.g. reasoning used the whole budget)."""


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    cached: bool

    def json(self) -> Any:
        return json.loads(self.content)


_limiters = {"llm": RateLimiter(config.SARVAM_LLM_RPM), "stt": RateLimiter(config.SARVAM_STT_RPM)}
_llm_slots = asyncio.Semaphore(config.MAX_CONCURRENT_LLM)
_sdk: AsyncSarvamAI | None = None


def sdk() -> AsyncSarvamAI:
    global _sdk
    if _sdk is None:
        if not config.SARVAM_API_KEY:
            raise SarvamNotConfigured("SARVAM_API_KEY is not set in .env")
        _sdk = AsyncSarvamAI(api_subscription_key=config.SARVAM_API_KEY, timeout=CHAT_TIMEOUT_S)
    return _sdk


def log_call(
    purpose: str,
    model: str,
    started: float,
    status: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_inr: float = 0.0,
    call_id: str | None = None,
) -> None:
    db.ex(
        "INSERT INTO api_call (purpose, model, prompt_tokens, completion_tokens, latency_ms, status, cost_inr, call_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        purpose, model, prompt_tokens, completion_tokens, int((time.monotonic() - started) * 1000),
        status, cost_inr, call_id or current_call.get(),
    )  # fmt: skip


async def request(
    api: str,
    purpose: str,
    model: str,
    fn: Callable[[], Awaitable[Any]],
    usage: Callable[[Any], dict[str, Any]] | None = None,
    tokens: int = 1,
) -> Any:
    """Run one Sarvam call. Retries 429/503/network errors with backoff; 400/403 raise at once."""
    for delay in (*RETRY_DELAYS_S, None):
        await _limiters[api].acquire(tokens)
        started = time.monotonic()
        try:
            result = await fn()
        except ApiError as exc:
            log_call(purpose, model, started, str(exc.status_code))
            if exc.status_code not in RETRYABLE_STATUS or delay is None:
                raise
        except (httpx.TransportError, TimeoutError):
            log_call(purpose, model, started, "network")
            if delay is None:
                raise
        else:
            log_call(purpose, model, started, "200", **(usage(result) if usage else {}))
            return result
        await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
    raise AssertionError("unreachable")


# ---- cache ---------------------------------------------------------------------------------


def cache_path(purpose: str, key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
    return config.CACHE_DIR / purpose / f"{safe}.json"


def cache_get(purpose: str, key: str) -> Any | None:
    path = cache_path(purpose, key)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cache_put(purpose: str, key: str, value: Any) -> None:
    path = cache_path(purpose, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def text_hash(*parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- chat ----------------------------------------------------------------------------------


async def chat(
    purpose: str,
    key: str,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    reasoning_effort: str | None = "low",
    temperature: float = 0.0,
    max_tokens: int = 4000,
    use_cache: bool = True,
) -> ChatResult:
    """One chat completion, cached by `key` plus a hash of the full request.

    `json_schema` is the {"name", "strict", "schema"} object. `reasoning_effort=None` turns
    thinking off (sent as an explicit null). Reasoning tokens count against `max_tokens`.
    """
    model = model or config.SARVAM_LLM_MODEL
    request_hash = text_hash(model, messages, json_schema, reasoning_effort, temperature, max_tokens)[:16]
    cache_key = f"{key}-{request_hash}"
    hit = cache_get(purpose, cache_key) if use_cache else None
    # A cut-off or empty answer is never worth reusing; ask again.
    if hit is not None and hit.get("finish_reason") != "length" and hit["content"].strip():
        return ChatResult(hit["content"], hit.get("finish_reason"), cached=True)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        # Retries are ours, so each attempt is rate-limited and logged.
        "request_options": {"timeout_in_seconds": CHAT_TIMEOUT_S, "max_retries": 0},
    }
    if json_schema is not None:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": json_schema}

    def usage(resp: Any) -> dict[str, Any]:
        u = resp.usage
        if u is None:
            return {}
        return {
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "cost_inr": llm_cost_inr(u.prompt_tokens, u.completion_tokens),
        }

    async with _llm_slots:
        resp = await request("llm", purpose, model, lambda: sdk().chat.completions(**kwargs), usage)
    choice = resp.choices[0]
    content = choice.message.content or ""
    if choice.finish_reason == "length" or not content.strip():
        # Cut off mid-answer (or nothing at all): never cache or use it.
        raise ChatTruncated(
            f"{purpose}: answer cut off or empty (finish_reason={choice.finish_reason},"
            f" max_tokens={max_tokens}, {len(content)} characters returned)"
        )
    cache_put(purpose, cache_key, {"content": content, "finish_reason": choice.finish_reason})
    return ChatResult(content, choice.finish_reason, cached=False)


# ---- batch speech-to-text ------------------------------------------------------------------

MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 64
STT_HARD_TIMEOUT_S = 2 * 3600


def load_keyterms() -> list[str]:
    path = config.RUBRIC_DIR / "keyterms.txt"
    if not path.exists():
        return []
    seen: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#") and len(term) <= MAX_KEYTERM_CHARS:
            seen.setdefault(term, None)
    return list(seen)[:MAX_KEYTERMS]


async def transcribe_batch(
    paths: list[Path], audio_seconds: float, language_code: str | None = None
) -> dict[str, dict[str, Any] | str]:
    """Transcribe up to 20 files in one Saaras batch job with diarization.

    Returns file name -> raw Saaras JSON (dict) on success, or an error message (str).
    """
    model = config.SARVAM_STT_MODEL
    params: dict[str, Any] = {
        "model": model,
        "mode": "codemix",
        "language_code": language_code or config.SARVAM_STT_LANGUAGE,
        "with_timestamps": True,
        "with_diarization": True,
        "num_speakers": 2,
    }
    keyterms = load_keyterms()
    if keyterms and model == "saaras:v4":
        params["keyterms"] = keyterms

    started = time.monotonic()
    jobs = sdk().speech_to_text_job
    job = await request("stt", "stt_create_job", model, lambda: jobs.create_job(**params))
    files = [str(p) for p in paths]
    await request("stt", "stt_upload", model, lambda: job.upload_files(file_paths=files, timeout=300.0))
    await request("stt", "stt_start", model, job.start)

    while True:
        status = await request("stt", "stt_status", model, job.get_status)
        if str(status.job_state).lower() in ("completed", "failed"):
            break
        if time.monotonic() - started > STT_HARD_TIMEOUT_S:
            raise TimeoutError(f"STT job {job.job_id} still {status.job_state}")
        await asyncio.sleep(10)  # docs: never poll faster than every 5 s

    out: dict[str, dict[str, Any] | str] = {p.name: "missing from job result" for p in paths}
    ok_names = set()
    for detail in status.job_details or []:
        if not detail.inputs:
            continue
        name = detail.inputs[0].file_name
        if detail.state == "Success" and detail.outputs:
            ok_names.add(name)
        else:
            out[name] = detail.error_message or str(detail.state)
    if ok_names:
        with tempfile.TemporaryDirectory(prefix="calllens-stt-") as tmp:
            # download_outputs makes two API calls: status (file mapping) and download links.
            await request("stt", "stt_download", model, lambda: job.download_outputs(output_dir=tmp), tokens=2)
            for path in Path(tmp).glob("*.json"):
                out[path.name.removesuffix(".json")] = json.loads(path.read_text(encoding="utf-8"))
    return out  # the caller logs transcription cost per call, from each recording's length


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
