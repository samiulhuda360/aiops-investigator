"""A small OpenAI-compatible chat client with tool calling, a record/replay cache, a rate limit and a hard call cap.

Every live reply is stored under ``cache/`` keyed by a hash of the exact request (model, messages, tools). Because
the environment and the tools are deterministic, replaying a run sends the same requests and finds every reply in
the cache, so the evaluation and the demo run offline, for anyone, with no key.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-flash-lite-latest"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "cache" / "llm"


class CacheMiss(RuntimeError):
    """Offline mode and the request is not in the cache."""


class RateLimited(RuntimeError):
    """The provider returned 429. The run stops so no result is invented."""


class CallCapReached(RuntimeError):
    """The live-call budget for this cache directory is used up."""


@dataclass
class ChatResult:
    message: dict[str, Any]
    usage: dict[str, int]
    latency_s: float
    cache_hit: bool
    model: str


class ChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        cache_dir: Path | None = None,
        offline: bool | None = None,
        min_interval: float | None = None,
        max_live_calls: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("AI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.model = model or os.environ.get("AI_MODEL") or DEFAULT_MODEL
        self.cache_dir = cache_dir or Path(os.environ.get("AIOPS_CACHE_DIR", DEFAULT_CACHE))
        env_offline = os.environ.get("AIOPS_OFFLINE", "") == "1"
        self.offline = offline if offline is not None else (env_offline or not self.api_key)
        self.min_interval = min_interval if min_interval is not None else float(os.environ.get("AI_MIN_INTERVAL", "0"))
        self.max_live_calls = max_live_calls if max_live_calls is not None else int(os.environ.get("AI_MAX_CALLS", "150"))
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ----- cache -----
    def key(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _counter_path(self) -> Path:
        return self.cache_dir / "_live_calls.json"

    def live_calls_used(self) -> int:
        p = self._counter_path()
        return int(json.loads(p.read_text())["count"]) if p.exists() else 0

    def _bump_counter(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._counter_path().write_text(json.dumps({"count": self.live_calls_used() + 1}) + "\n")

    # ----- the call -----
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto") -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": t} for t in tools],
            "tool_choice": tool_choice,
            "temperature": 0,
        }
        key = self.key(payload)
        path = self._path(key)
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            return ChatResult(rec["message"], rec["usage"], rec["latency_s"], True, rec.get("model", self.model))
        if self.offline:
            raise CacheMiss(f"no cached reply for request {key[:12]} and no live model configured")
        with self._lock:
            if self.live_calls_used() >= self.max_live_calls:
                raise CallCapReached(f"live call cap of {self.max_live_calls} reached")
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            data, latency = self._post(payload)
            self._last_call = time.monotonic()
            self._bump_counter()
        choice = data["choices"][0]
        message = {k: v for k, v in choice["message"].items() if v is not None}
        message.setdefault("role", "assistant")
        u = data.get("usage") or {}
        usage = {"prompt_tokens": int(u.get("prompt_tokens", 0)), "completion_tokens": int(u.get("completion_tokens", 0))}
        rec = {
            "request_sha256": key,
            "message": message,
            "usage": usage,
            "latency_s": round(latency, 3),
            "model": data.get("model", self.model),
            "finish_reason": choice.get("finish_reason"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return ChatResult(message, usage, latency, False, rec["model"])

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(3):
            t0 = time.monotonic()
            r = httpx.post(self.base_url + "chat/completions", json=payload, headers=headers, timeout=90.0)
            latency = time.monotonic() - t0
            if r.status_code == 429:
                raise RateLimited(f"429 from provider: {r.text[:200]}")
            if r.status_code >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"provider error {r.status_code}: {r.text[:300]}")
            result: dict[str, Any] = r.json()
            return result, latency
        raise RuntimeError("provider kept failing")
