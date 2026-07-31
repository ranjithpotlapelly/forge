"""Adapter (Phase 22): implements core.llm.LLMClient against any
OpenAI-compatible chat completions API -- OpenAI itself, or any of the many
hosted/self-hosted services that mirror its REST shape (including Ollama's
own /v1 endpoint, which is what app/*_smoke_test.py below actually verifies
this against, since this dev box has no paid API key). config.yaml has
pointed at this upgrade path in a comment since Phase 1 ("Ollama, local CPU
-> Hosted model API"); this is that adapter.

Same port adapters/llm_ollama.py implements (core.llm.LLMClient: generate()
+ stream()), so either config slot (llm or code_model) can point here with a
one-line config.yaml change and zero changes anywhere a Retriever/LLMClient
is consumed -- core/ and product/ never know or care which one is behind
the port they were handed.

Billing guard: refuses to construct against a non-free-tier OpenRouter model
unless config.yaml sets allow_paid_models: true -- see _is_free_tier() and
the check in __init__. This exists so an accidental .env/config change (a
typo, a model swapped for a "better" one without checking) can't silently
start incurring real cost. The actual backstop is a hard credit limit set on
the OpenRouter API key itself (openrouter.ai/keys) -- this check is a
convenience trip-wire on top of that, not a replacement for it.
"""
from __future__ import annotations
import json
from typing import Iterable

import requests

from core.types import Message

# Meaningful only for OpenRouter itself; no other OpenAI-compatible provider
# (OpenAI, a self-hosted vLLM, ...) uses either convention below, so the
# billing guard only applies when base_url is actually OpenRouter's.
_OPENROUTER_HOST = "openrouter.ai"
_FREE_SUFFIX = ":free"  # OpenRouter's zero-cost VARIANT of a model, e.g. "deepseek/deepseek-chat-v3:free"

def _is_free_tier(model: str) -> bool:
    """True for OpenRouter's two "this is free" spellings: a ":free" variant
    suffix on an otherwise-paid model (the common case), or a model whose own
    name IS "free" (e.g. "openrouter/free", an auto-router to whatever free
    model is available -- no ":free" suffix since there's no paid sibling
    variant to distinguish it from)."""
    return model.endswith(_FREE_SUFFIX) or model.rsplit("/", 1)[-1] == "free"

class OpenAICompatibleLLM:
    def __init__(
        self,
        model: str | None = None,
        models: list[str] | None = None,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 120,
        allow_paid_models: bool = False,
        **opts,
    ):
        if not model and not models:
            raise ValueError("OpenAICompatibleLLM requires 'model' or 'models'")
        # 'models' (plural) is OpenRouter's native fallback chain: the server
        # tries each id in order and moves to the next on rate-limit/moderation/
        # downtime/any error, so no client-side retry logic is needed here.
        # self.model is kept as the primary id purely for logging/identification.
        self.models = models
        self.model = model or models[0]
        self.base_url = base_url.rstrip("/")
        self.opts = opts
        self._api_key = api_key
        self._timeout = timeout
        # Billing guard: refuse to even construct against a non-free
        # OpenRouter model unless explicitly opted in. This is a code-level
        # backstop, not the real safety net -- set a hard credit limit on the
        # OpenRouter API key itself (openrouter.ai/keys), which is enforced
        # server-side and can't be bypassed by a config typo or a model
        # getting silently swapped for a paid one. This check only guards
        # against exactly that: an accidental config change, not a
        # compromised key or OpenRouter changing what a model id means.
        if not allow_paid_models and _OPENROUTER_HOST in self.base_url:
            not_free = [m for m in (self.models or [self.model]) if m and not _is_free_tier(m)]
            if not_free:
                raise ValueError(
                    f"code_model/llm is configured with non-free-tier OpenRouter model(s) "
                    f"{not_free!r} and allow_paid_models is not set -- refusing to start "
                    "rather than risk an unexpected charge. If this is intentional, set "
                    "allow_paid_models: true in config.yaml's code_model (or llm) section."
                )

    def generate(self, messages: list[Message], **opts) -> str:
        response = self._post(messages, stream=False, **opts)
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def stream(self, messages: list[Message], **opts) -> Iterable[str]:
        response = self._post(messages, stream=True, **opts)
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue  # SSE keep-alive/blank lines
            raw = line[len("data: "):].strip()
            if raw == "[DONE]":
                break
            chunk = json.loads(raw)
            content = chunk["choices"][0].get("delta", {}).get("content")
            if content:
                yield content

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(self, messages: list[Message], stream: bool, **opts) -> dict:
        merged = {**self.opts, **opts}
        fmt = merged.pop("format", None)
        # num_ctx is an Ollama-specific context-window knob (set in
        # config.yaml's llm: block) that has no OpenAI-compatible equivalent
        # -- hosted models size their own context, so it's just dropped
        # rather than sent as a param the server won't recognize.
        merged.pop("num_ctx", None)
        payload: dict = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            **merged,
        }
        if self.models:
            payload["models"] = self.models
        else:
            payload["model"] = self.model
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, messages: list[Message], stream: bool, **opts) -> "requests.Response":
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(messages, stream, **opts),
                timeout=self._timeout,
                stream=stream,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"could not reach the hosted model API at {self.base_url}: {e}") from e
        return self._check(response)

    def _check(self, response: "requests.Response") -> "requests.Response":
        if response.status_code == 200:
            return response
        if response.status_code == 401:
            raise RuntimeError(
                "the hosted model API rejected the request (401 Unauthorized) -- "
                "the API key is missing, invalid, or expired."
            )
        if response.status_code == 403:
            raise RuntimeError(
                "the hosted model API denied access (403 Forbidden) -- "
                "the API key may lack access to this model."
            )
        try:
            message = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        raise RuntimeError(f"hosted model API error {response.status_code}: {message}")
