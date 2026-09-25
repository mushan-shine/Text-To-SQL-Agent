"""LLM client — Zhipu open platform (OpenAI-compatible chat completions).

Experiment requirements (docs/PROJECT_POSITIONING.md §7):
* greedy decoding (``do_sample=False``): the same prompt must give the same answer,
  otherwise differences between experiment arms cannot be attributed;
* every call records tokens and latency (cost per recovered case);
* hard call / token budgets so a loop or batch cannot run away;
* optional on-disk cache keyed by (model, params, prompt) so re-running an
  evaluation replays identical responses without new API calls.

Lessons carried over from gmv-rca-agent: retry 408/429/5xx with backoff on the
*same* prompt; reject API keys containing control characters (a Ctrl+V pasted
into a hidden prompt stores ``\\x16`` and the gateway answers HTML 400).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4-flash"
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LlmError(RuntimeError):
    pass


class LlmBudgetExceeded(LlmError):
    pass


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    finish_reason: str | None = None
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class UsageMeter:
    max_calls: int | None = None
    max_tokens: int | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def check(self) -> None:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise LlmBudgetExceeded(f"call budget exhausted ({self.calls}/{self.max_calls})")
        if self.max_tokens is not None and self.input_tokens + self.output_tokens >= self.max_tokens:
            raise LlmBudgetExceeded(f"token budget exhausted ({self.input_tokens + self.output_tokens}/{self.max_tokens})")

    def record(self, r: LlmResponse) -> None:
        with self._lock:  # safe under a thread pool
            if r.cached:
                self.cached_calls += 1
                return
            self.calls += 1
            self.input_tokens += r.input_tokens
            self.output_tokens += r.output_tokens

    def snapshot(self) -> dict[str, int]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cached_calls": self.cached_calls}


class ChatClient(Protocol):
    model: str

    def complete(self, prompt: str, system: str | None = None) -> LlmResponse: ...


def check_api_key(key: str) -> str:
    bad = [f"U+{ord(c):04X}" for c in key if ord(c) < 32 or ord(c) == 127]
    if bad:
        raise LlmError(f"API key contains {len(bad)} invisible control character(s) ({', '.join(bad)}); "
                       "re-enter it without Ctrl+V into a hidden prompt")
    if not key.strip():
        raise LlmError("API key is empty (set ZHIPUAI_API_KEY in .env)")
    return key.strip()


@dataclass
class ZhipuChatClient:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = ZHIPU_BASE_URL
    max_output_tokens: int = 2048
    timeout_s: float = 120.0
    max_retries: int = 5
    meter: UsageMeter = field(default_factory=UsageMeter)

    def __post_init__(self) -> None:
        self.api_key = check_api_key(self.api_key)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **kw: Any) -> "ZhipuChatClient":
        env = dict(os.environ) if env is None else env
        return cls(api_key=env.get("ZHIPUAI_API_KEY", ""), model=env.get("ZHIPU_MODEL", DEFAULT_MODEL).strip(), **kw)

    @property
    def params(self) -> dict[str, Any]:
        return {"do_sample": False, "max_tokens": self.max_output_tokens}

    def complete(self, prompt: str, system: str | None = None) -> LlmResponse:
        self.meter.check()
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload = {"model": self.model, "messages": messages, **self.params}
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout_s,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise LlmError(f"transport error after {attempt + 1} attempts: {e}") from e
                _backoff(attempt)
                continue
            latency = int((time.perf_counter() - t0) * 1000)
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                log.warning("zhipu HTTP %s, retry %d", resp.status_code, attempt + 1)
                _backoff(attempt)
                continue
            if resp.status_code != 200:
                raise LlmError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            r = LlmResponse(text=choice["message"].get("content") or "", model=data.get("model", self.model),
                            input_tokens=int(usage.get("prompt_tokens", 0)),
                            output_tokens=int(usage.get("completion_tokens", 0)),
                            latency_ms=latency, finish_reason=choice.get("finish_reason"))
            self.meter.record(r)
            return r
        raise LlmError("unreachable")


def _backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> None:
    time.sleep(min(cap, base * 2 ** attempt))


def prompt_fingerprint(model: str, params: dict[str, Any], system: str | None, prompt: str) -> str:
    blob = json.dumps({"model": model, "params": params, "system": system, "prompt": prompt},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class CachingChatClient:
    """Replays responses for identical (model, params, system, prompt)."""

    inner: ZhipuChatClient
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._cache[rec["key"]] = rec["response"]

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def meter(self) -> UsageMeter:
        return self.inner.meter

    def complete(self, prompt: str, system: str | None = None) -> LlmResponse:
        key = prompt_fingerprint(self.inner.model, self.inner.params, system, prompt)
        if key in self._cache:
            r = LlmResponse(**{**self._cache[key], "cached": True})
            self.inner.meter.record(r)
            return r
        r = self.inner.complete(prompt, system)
        with self._lock:  # safe under a thread pool
            self._cache[key] = asdict(r)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "response": asdict(r)}, ensure_ascii=False) + "\n")
        return r
