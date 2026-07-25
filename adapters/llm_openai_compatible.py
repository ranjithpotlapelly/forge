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
"""
from __future__ import annotations
import json
from typing import Iterable

import requests

from core.types import Message

class OpenAICompatibleLLM:
    def __init__(self, model: str, base_url: str, api_key: str = "", timeout: float = 120, **opts):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.opts = opts
        self._api_key = api_key
        self._timeout = timeout

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
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            **merged,
        }
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
