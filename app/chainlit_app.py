"""Chainlit chat interface (Phase 8): the first user-facing surface for
Forge's Q&A engine. Run with:  chainlit run app/chainlit_app.py -w
"""
from __future__ import annotations
import asyncio
import time
import uuid
import chainlit as cl
from app.config_loader import load_config
from app.wiring import build_engine, build_store, build_tracing

_cfg = load_config()
_tracing = build_tracing(_cfg)
_tracing.start()
_engine = build_engine(_cfg)
_store = build_store(_cfg)

def _format_answer(answer) -> str:
    if not answer.citations:
        return answer.text
    sources = "\n".join(
        f"- `{c.metadata.get('path', '?')}:{c.metadata.get('start_line', '?')}`"
        for c in answer.citations
    )
    return f"{answer.text}\n\n**Sources**\n{sources}"

@cl.on_chat_start
async def on_chat_start():
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    await cl.Message(
        content="Ask me anything about the indexed codebase — answers come with file+line citations."
    ).send()

@cl.on_message
async def on_message(message: cl.Message):
    thread_id = cl.user_session.get("thread_id")
    async with cl.Step(name="engine.ask", type="run"):
        answer = await asyncio.to_thread(_engine.ask, message.content, thread_id)

    _store.put(
        f"chat:{thread_id}:{int(time.time() * 1000)}",
        {"question": message.content, "answer": answer.text, "citations": len(answer.citations)},
    )
    await cl.Message(content=_format_answer(answer)).send()
