"""Adapter (Phase 2/23): implements core.llm.LLMClient via a local Ollama server.

Phase 23: num_ctx and keep_alive are now always present in the outgoing
request (options and the top-level keep_alive param respectively), not just
whenever config.yaml happens to set them -- Ollama silently falls back to
its own small built-in default context size when num_ctx is omitted, and to
its ~5min idle-unload when keep_alive is omitted, and both were previously
only set for whichever LLMClient instance's config.yaml section happened to
list them (true for `llm`, not for `code_model`). Proven via
logging.getLogger(__name__).debug(...) of the actual options/keep_alive
about to be sent, not assumed from reading ollama._client.py's source (which
was also checked directly: .chat() serializes options/keep_alive straight
into the /api/chat JSON body via ChatRequest(...).model_dump(exclude_none=True)).
"""
from __future__ import annotations
import logging
import re
from typing import Iterable
import ollama
from opentelemetry import trace
from core.types import Message

_tracer = trace.get_tracer(__name__)
_logger = logging.getLogger(__name__)

# Safety net only: config.yaml is expected to always supply num_ctx (llm/
# code_model/answer_model sections all set one after Phase 23), so this
# should rarely if ever be the value actually sent -- it exists so a call
# somehow missing one from both config and **opts still gets an explicit,
# visible value instead of silently falling through to Ollama's own default.
_FALLBACK_NUM_CTX = 4096

def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some models (Qwen3) emit."""
    return _THINK_BLOCK_RE.sub("", text).strip()

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"

def _longest_tag_prefix_suffix(buffer: str, tag: str) -> int:
    """Length of the longest suffix of buffer that is itself a prefix of tag.

    Used to hold back a chunk boundary that might be mid-tag (e.g. buffer
    ends in "<thi" and the next streamed piece is "nk>") instead of emitting
    a partial tag as visible text.
    """
    for length in range(min(len(tag) - 1, len(buffer)), 0, -1):
        if tag.startswith(buffer[-length:]):
            return length
    return 0

def _strip_think_stream(chunks: Iterable[str]) -> Iterable[str]:
    buffer = ""
    in_think = False
    for chunk in chunks:
        buffer += chunk
        while True:
            if in_think:
                idx = buffer.find(_THINK_CLOSE)
                if idx == -1:
                    break  # still inside the block; wait for more input
                buffer = buffer[idx + len(_THINK_CLOSE):]
                in_think = False
            else:
                idx = buffer.find(_THINK_OPEN)
                if idx == -1:
                    hold = _longest_tag_prefix_suffix(buffer, _THINK_OPEN)
                    emit_len = len(buffer) - hold
                    if emit_len > 0:
                        yield buffer[:emit_len]
                        buffer = buffer[emit_len:]
                    break
                if idx > 0:
                    yield buffer[:idx]
                buffer = buffer[idx + len(_THINK_OPEN):]
                in_think = True
    if not in_think and buffer:
        yield buffer

class OllamaLLM:
    def __init__(self, model: str, host: str, **opts):
        self.model, self.host, self.opts = model, host, opts
        self._client = ollama.Client(host=host)

    def generate(self, messages: list[Message], **opts) -> str:
        with _tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("llm.provider", "ollama")
            span.set_attribute("llm.model", self.model)
            fmt, keep_alive, options = self._resolve_call_opts(opts)
            self._log_request(fmt, keep_alive, options)
            response = self._client.chat(
                model=self.model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                format=fmt,
                options=options,
                keep_alive=keep_alive,
            )
            text = _strip_think(response["message"]["content"])
            span.set_attribute("llm.response.length", len(text))
            return text

    def stream(self, messages: list[Message], **opts) -> Iterable[str]:
        with _tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("llm.provider", "ollama")
            span.set_attribute("llm.model", self.model)
            segments = 0
            fmt, keep_alive, options = self._resolve_call_opts(opts)
            self._log_request(fmt, keep_alive, options)

            def _raw_chunks():
                for chunk in self._client.chat(
                    model=self.model,
                    messages=[{"role": m.role, "content": m.content} for m in messages],
                    format=fmt,
                    options=options,
                    keep_alive=keep_alive,
                    stream=True,
                ):
                    content = chunk["message"]["content"]
                    if content:
                        yield content

            for piece in _strip_think_stream(_raw_chunks()):
                segments += 1
                yield piece
            span.set_attribute("llm.response.segments", segments)

    def _resolve_call_opts(self, opts: dict) -> tuple[str | None, str | float | None, dict]:
        """format and keep_alive are top-level .chat() params, not entries
        inside options -- pulled out here so both generate() and stream()
        build the exact same request shape. num_ctx always ends up in
        options one way or another: per-call, then constructor-level
        (config.yaml), then _FALLBACK_NUM_CTX -- never silently omitted."""
        fmt = opts.pop("format", self.opts.get("format"))
        keep_alive = opts.pop("keep_alive", self.opts.get("keep_alive"))
        options = {k: v for k, v in {**self.opts, **opts}.items() if k not in ("format", "keep_alive")}
        options.setdefault("num_ctx", _FALLBACK_NUM_CTX)
        return fmt, keep_alive, options

    def _log_request(self, fmt: str | None, keep_alive, options: dict) -> None:
        _logger.debug(
            "ollama /api/chat request: model=%s format=%r keep_alive=%r options=%s",
            self.model, fmt, keep_alive, options,
        )
