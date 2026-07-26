# Forge (Phase 9): packages the Chainlit UI + engine. Ollama and Phoenix are
# separate services (host-installed Ollama, containerized Phoenix — see
# docker-compose.yml) so this image stays small; no model weights live here.

FROM python:3.14-slim AS builder
WORKDIR /app

# uv instead of pip: much faster resolves/downloads and sturdier retries on
# flaky networks — this build was timing out against files.pythonhosted.org
# under plain pip.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN uv pip install --python /opt/venv/bin/python -r requirements.txt

FROM python:3.14-slim
WORKDIR /app

# git: adapters/mcp_servers/workspace_server.py shells out to it (init/add/
# commit) for the sandboxed workspace -- python:3.14-slim doesn't ship it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY core/ core/
COPY adapters/ adapters/
COPY product/ product/
COPY app/ app/
COPY config.yaml chainlit.md ./

EXPOSE 8000
CMD ["python", "-m", "app.run_chainlit", "run", "app/chainlit_app.py", "--host", "0.0.0.0", "--port", "8000"]
