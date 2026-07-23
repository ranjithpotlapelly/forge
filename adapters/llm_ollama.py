"""Adapter (Phase 2): implements core.llm.LLMClient via a local Ollama server."""
from __future__ import annotations
from typing import Iterable
import ollama
from opentelemetry import trace
from core.types import Message

_tracer = trace.get_tracer(__name__)

class OllamaLLM:
    def __init__(self, model: str, host: str, **opts):
        self.model, self.host, self.opts = model, host, opts
        self._client = ollama.Client(host=host)

    def generate(self, messages: list[Message], **opts) -> str:
        with _tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("llm.provider", "ollama")
            span.set_attribute("llm.model", self.model)
            response = self._client.chat(
                model=self.model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                options={**self.opts, **opts},
            )
            text = response["message"]["content"]
            span.set_attribute("llm.response.length", len(text))
            return text

    def stream(self, messages: list[Message], **opts) -> Iterable[str]:
        with _tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("llm.provider", "ollama")
            span.set_attribute("llm.model", self.model)
            segments = 0
            for chunk in self._client.chat(
                model=self.model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                options={**self.opts, **opts},
                stream=True,
            ):
                content = chunk["message"]["content"]
                if content:
                    segments += 1
                    yield content
            span.set_attribute("llm.response.segments", segments)
