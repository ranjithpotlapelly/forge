"""Adapter (Phase 7): implements core.tools.Tool via an MCP server connected
over stdio.

MCP is asyncio-native; every other port in Forge is synchronous. Rather than
leak asyncio into product/ and app/, this adapter runs one dedicated event
loop in a background thread for the lifetime of the server connection, and
bridges each sync Tool.run() call onto it.
"""
from __future__ import annotations
import asyncio
import threading
from contextlib import AsyncExitStack
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from core.tools import Tool

class _McpConnection:
    """One background event loop + one live MCP session, shared by every
    Tool created from the same server process."""

    def __init__(self, params: StdioServerParameters):
        self._loop = asyncio.new_event_loop()
        self._session: ClientSession | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(params,), daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def _run(self, params: StdioServerParameters) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect(params))
        except BaseException as e:  # noqa: BLE001
            self._error = e
            self._ready.set()

    async def _connect(self, params: StdioServerParameters) -> None:
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params))
            self._session = await stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            self._ready.set()
            await asyncio.Event().wait()  # keep the connection open until the process exits

    def call(self, coro_fn):
        future = asyncio.run_coroutine_threadsafe(coro_fn(self._session), self._loop)
        return future.result()

class McpTool:
    def __init__(self, name: str, description: str, input_schema: dict, requires_approval: bool, connection: _McpConnection):
        self.name = name
        self.description = description
        self.requires_approval = requires_approval
        self._schema = input_schema
        self._connection = connection

    def input_schema(self) -> dict[str, Any]:
        return self._schema

    def run(self, **kwargs) -> Any:
        async def _call(session: ClientSession):
            result = await session.call_tool(self.name, kwargs)
            text = "".join(part.text for part in result.content if hasattr(part, "text"))
            if result.isError:
                raise RuntimeError(f"MCP tool {self.name!r} failed: {text}")
            return text
        return self._connection.call(_call)

def load_mcp_tools(command: str, args: list[str], require_approval_for: set[str]) -> list[Tool]:
    """Connect to an MCP server and return its tools as core.tools.Tool instances."""
    connection = _McpConnection(StdioServerParameters(command=command, args=args))

    async def _list(session: ClientSession):
        return await session.list_tools()

    listed = connection.call(_list)
    return [
        McpTool(
            name=t.name,
            description=t.description or "",
            input_schema=t.inputSchema,
            requires_approval=t.name in require_approval_for,
            connection=connection,
        )
        for t in listed.tools
    ]
