"""Port: process-wide tracing. Adapters: Phoenix (local/dockerized collector)
-> a hosted OTel backend (upgrade).

Unlike the other ports, this isn't called per-request. start() registers the
global OpenTelemetry tracer provider once at startup; after that, every module
gets traced by calling the standard opentelemetry.trace.get_tracer(__name__)
API directly — no dependency on this port or on Phoenix specifically.
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable

@runtime_checkable
class Tracing(Protocol):
    def start(self) -> None:
        """Register the global tracer provider and instrument the process."""
        ...
