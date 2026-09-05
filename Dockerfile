# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ARG UV_VERSION=0.11.32

ENV PATH="/app/.venv/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    ARL_DATA_DIR=/var/lib/arl \
    ARL_DATABASE_PATH=agent-reliability-lab.db \
    ARL_SCENARIO_DIR=/app/scenarios/incident-response \
    ARL_EVALUATION_SUITE=incident-response \
    ARL_EVALUATION_SUITE_DIR=/app/scenarios/incident-response

RUN groupadd --gid 10001 arl \
    && useradd --uid 10001 --gid arl --shell /usr/sbin/nologin --create-home arl \
    && mkdir -p /app /var/lib/arl \
    && chown -R arl:arl /var/lib/arl \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

COPY pyproject.toml uv.lock LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY scenarios ./scenarios
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["uvicorn", "agent_reliability_lab.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
