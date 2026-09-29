"""A minimal client for a local model served by Ollama (https://ollama.com),
over the HTTP API it serves on localhost. No SDK: httpx is already here.

The model has two jobs in the chat and neither is arithmetic: route a
question to one of the tools (route), and write a short take on numbers
Python already computed (stream). Every number the user sees comes from
Python; see PLANNING.md, Phase 5, "Core design".

Settings, from .env with these defaults:
    LLM_MODEL=qwen3:4b-instruct       the model chosen by the Phase 5 bake-off
    OLLAMA_URL=http://localhost:11434 where Ollama listens
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from dynasty_agent import config  # noqa: F401  (loads .env before the settings below are read)
from dynasty_agent.errors import AgentError

LLM_MODEL = os.environ.get("LLM_MODEL") or "qwen3:4b-instruct"
OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
# How long Ollama keeps the model loaded after a request. Loading it takes
# about 18 seconds on the MacBook Air M4; a chat session shouldn't pay that
# twice because the user paused to think.
KEEP_ALIVE = "30m"
# Routing is a decision, not prose: temperature 0, the same answer every time.
ROUTE_OPTIONS = {"num_ctx": 8192, "temperature": 0, "num_predict": 200}
TAKE_OPTIONS = {"num_ctx": 8192, "temperature": 0.2, "num_predict": 160}
# A walk through a calculation runs longer than a take; a cap of 160 cut one off mid-sentence in the first live test.
EXPLAIN_OPTIONS = {"num_ctx": 8192, "temperature": 0.2, "num_predict": 420}
# A reply that spent this long loading the model says "loading model..."
# instead of reporting a misleadingly slow first-words time.
LOAD_NOTICE_SECONDS = 1.0


class LLMError(AgentError):
    """The local model can't be reached or isn't installed; the message says how to fix it."""


@dataclass
class Stats:
    """What one reply cost, from Ollama's own counters (nanoseconds), the
    same numbers `ollama run --verbose` prints."""

    model: str
    eval_count: int = 0
    eval_duration_ns: int = 0
    prompt_eval_count: int = 0
    prompt_eval_duration_ns: int = 0
    load_duration_ns: int = 0
    first_token_s: float | None = None
    total_s: float = 0.0

    @property
    def tokens_per_second(self) -> float | None:
        return self.eval_count / (self.eval_duration_ns / 1e9) if self.eval_duration_ns else None

    @property
    def prompt_tokens_per_second(self) -> float | None:
        return self.prompt_eval_count / (self.prompt_eval_duration_ns / 1e9) if self.prompt_eval_duration_ns else None

    @property
    def loaded_model(self) -> bool:
        return self.load_duration_ns / 1e9 >= LOAD_NOTICE_SECONDS


def _stats(model: str, body: dict, first_token_s: float | None, total_s: float) -> Stats:
    return Stats(
        model=model,
        eval_count=body.get("eval_count") or 0,
        eval_duration_ns=body.get("eval_duration") or 0,
        prompt_eval_count=body.get("prompt_eval_count") or 0,
        prompt_eval_duration_ns=body.get("prompt_eval_duration") or 0,
        load_duration_ns=body.get("load_duration") or 0,
        first_token_s=first_token_s,
        total_s=total_s,
    )


def _arguments(raw) -> dict:
    """Tool arguments as a dict: Ollama sends a dict, however some models put
    a JSON string there instead."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class OllamaClient:
    def __init__(self, model: str = LLM_MODEL, url: str = OLLAMA_URL, transport: httpx.BaseTransport | None = None):
        self.model = model
        self._http = httpx.Client(base_url=url, timeout=httpx.Timeout(120.0, connect=3.0), transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            response = self._http.post(path, json=body)
        except httpx.ConnectError:
            raise LLMError("Ollama isn't running: open the Ollama app (or run `ollama serve`), then try again.") from None
        except httpx.TimeoutException:
            raise LLMError("Ollama didn't answer in time; the model may still be loading. Try again in a moment.") from None
        if response.status_code == 404:
            raise LLMError(f"The model {self.model} isn't installed. Run `ollama pull {self.model}`.")
        if response.status_code >= 400:
            raise LLMError(f"Ollama returned an error ({response.status_code}): {response.text[:200]}")
        return response

    def ensure_ready(self) -> None:
        """Ollama is running and the model is installed, or LLMError saying
        which one isn't and what to run."""
        try:
            response = self._http.get("/api/tags")
            response.raise_for_status()
        except httpx.ConnectError:
            raise LLMError("Ollama isn't running: open the Ollama app (or run `ollama serve`), then try again.") from None
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama answered, but not as expected ({type(e).__name__}). Is something else on {self._http.base_url}?") from None
        names = {m.get("name") for m in response.json().get("models", [])}
        wanted = {self.model, f"{self.model}:latest"} if ":" not in self.model else {self.model}
        if not names & wanted:
            raise LLMError(f"The model {self.model} isn't installed. Run `ollama pull {self.model}` (about 2.5 GB).")

    def warm(self) -> None:
        """Load the model into memory ahead of the first question: Ollama loads
        a model for an empty conversation and answers nothing."""
        self._post("/api/chat", {"model": self.model, "messages": [], "keep_alive": KEEP_ALIVE})

    def route(self, messages: list[dict], tools: list[dict]) -> tuple[list[dict], str, Stats]:
        """One non-streamed turn with tools offered. Returns (tool calls as
        [{"name", "arguments": dict}], any text the model wrote, stats)."""
        started = time.monotonic()
        body = {"model": self.model, "messages": messages, "tools": tools, "stream": False, "think": False,
                "options": ROUTE_OPTIONS, "keep_alive": KEEP_ALIVE}
        data = self._post("/api/chat", body).json()
        message = data.get("message") or {}
        calls = [
            {"name": (c.get("function") or {}).get("name"), "arguments": _arguments((c.get("function") or {}).get("arguments"))}
            for c in message.get("tool_calls") or []
        ]
        return calls, message.get("content") or "", _stats(self.model, data, None, time.monotonic() - started)

    def stream(self, messages: list[dict], options: dict | None = None) -> Iterator[str | Stats]:
        """Stream a reply with no tools: yields text pieces as they arrive,
        then one Stats (exact counts from Ollama's final chunk)."""
        started = time.monotonic()
        first_token_s = None
        body = {"model": self.model, "messages": messages, "stream": True, "think": False,
                "options": options or TAKE_OPTIONS, "keep_alive": KEEP_ALIVE}
        try:
            with self._http.stream("POST", "/api/chat", json=body) as response:
                if response.status_code == 404:
                    raise LLMError(f"The model {self.model} isn't installed. Run `ollama pull {self.model}`.")
                if response.status_code >= 400:
                    response.read()
                    raise LLMError(f"Ollama returned an error ({response.status_code}): {response.text[:200]}")
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        if first_token_s is None:
                            first_token_s = time.monotonic() - started
                        yield piece
                    if chunk.get("done"):
                        yield _stats(self.model, chunk, first_token_s, time.monotonic() - started)
                        return
        except httpx.ConnectError:
            raise LLMError("Ollama isn't running: open the Ollama app (or run `ollama serve`), then try again.") from None
        except httpx.TimeoutException:
            raise LLMError("Ollama stopped answering partway through. Try the question again.") from None
