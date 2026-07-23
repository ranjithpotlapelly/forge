"""Adapter (Phase 6): implements core.observability.Tracing via a Phoenix
collector (runs as a container — see docker-compose.yml, `docker compose up -d
phoenix`) plus OpenInference auto-instrumentation for LangChain/LangGraph.
"""
from __future__ import annotations
from phoenix.otel import register

class PhoenixTracing:
    def __init__(self, endpoint: str, project_name: str = "forge", enabled: bool = True):
        self.endpoint, self.project_name, self.enabled = endpoint, project_name, enabled
        self._provider = None

    def start(self) -> None:
        if not self.enabled:
            return
        self._provider = register(
            endpoint=self.endpoint,
            project_name=self.project_name,
            protocol="grpc",
            auto_instrument=True,   # picks up openinference-instrumentation-langchain
            batch=False,            # export each span immediately, no flush needed
        )
