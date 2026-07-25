"""Adapter (Phase 2): implements core.llm.LLMClient via a local Ollama server."""
from __future__ import annotations
import re
from typing import Iterable
import ollama
from opentelemetry import trace
from core.types import Message

_tracer = trace.get_tracer(__name__)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"

def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some models (Qwen3) emit."""
    return _THINK_BLOCK_RE.sub("", text).strip()

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
            fmt = opts.pop("format", self.opts.get("format"))
            response = self._client.chat(
                model=self.model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                format=fmt,
                options={k: v for k, v in {**self.opts, **opts}.items() if k != "format"},
            )
            text = _strip_think(response["message"]["content"])
            span.set_attribute("llm.response.length", len(text))
            return text

    def stream(self, messages: list[Message], **opts) -> Iterable[str]:
        with _tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("llm.provider", "ollama")
            span.set_attribute("llm.model", self.model)
            segments = 0
            fmt = opts.pop("format", self.opts.get("format"))

            def _raw_chunks():
                for chunk in self._client.chat(
                    model=self.model,
                    messages=[{"role": m.role, "content": m.content} for m in messages],
                    format=fmt,
                    options={k: v for k, v in {**self.opts, **opts}.items() if k != "format"},
                    stream=True,
                ):
                    content = chunk["message"]["content"]
                    if content:
                        yield content

            for piece in _strip_think_stream(_raw_chunks()):
                segments += 1
                yield piece
            span.set_attribute("llm.response.segments", segments)
