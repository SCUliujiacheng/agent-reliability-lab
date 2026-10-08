# Local development

Setup, API examples, and the optional model adapter. The main [README](../README.md) covers a first run and the fixed benchmark.

## Docker Compose

```bash
docker compose up --build
```

The Compose stack runs both containers as non-root users, serves the dashboard
and `/v1` through one origin, and keeps the SQLite database in a named volume.

The environment variables are listed in [`.env.example`](../.env.example).
The application does not load that file automatically, and Compose declares its
own values explicitly. `ARL_TRUSTED_HOSTS` replaces the API allowlist; changing
the dashboard hostname also requires a matching `server_name` in
[`web/nginx.conf`](../web/nginx.conf).

## HTTP workflow

```bash
# Discover the catalog
curl http://127.0.0.1:8000/v1/scenarios

# Start a durable approval scenario and keep its review descriptor
RUN_JSON="$(curl -sS -X POST http://127.0.0.1:8000/v1/runs \
  -H "content-type: application/json" \
  -d '{"scenario_id":"approval-reconstruction","mode":"resilient"}')"
RUN_ID="$(
  printf '%s' "$RUN_JSON" |
    python -c 'import json,sys; print(json.load(sys.stdin)["id"])'
)"

read -r ACTION_STEP ACTION_FINGERPRINT < <(
  printf '%s' "$RUN_JSON" |
    python -c '
import json, sys
approval = json.load(sys.stdin)["pending_approval"]
print(approval["action_step"], approval["action_fingerprint"])
'
)

# Approve using the returned run ID and its current pending_approval descriptor
curl -X POST "http://127.0.0.1:8000/v1/runs/$RUN_ID/approvals" \
  -H "content-type: application/json" \
  -d "{
    \"actor\": \"demo-operator\",
    \"allow\": true,
    \"action_step\": $ACTION_STEP,
    \"action_fingerprint\": \"$ACTION_FINGERPRINT\",
    \"reason\": \"trace verified\"
  }"

curl "http://127.0.0.1:8000/v1/runs/$RUN_ID/trace?limit=100"

# Run and list the same frozen evaluation used by the dashboard
curl -X POST http://127.0.0.1:8000/v1/evaluations \
  -H "content-type: application/json" \
  -d '{"suite":"incident-response"}'

curl "http://127.0.0.1:8000/v1/evaluations?limit=10"
```

Copy `action_step` and `action_fingerprint` from the run's current
`pending_approval` descriptor without recomputing them. The server accepts the
decision only while that exact action is pending. An exact duplicate converges
idempotently; stale, forged, or conflicting decisions return HTTP 409. Pending
arguments are recursively sanitized before review. `actor` is a caller-supplied
label, not an authenticated identity.

Evaluation creation completes synchronously with HTTP 201, persists the report
in SQLite, and is limited to one concurrent request per API process (the
default local deployment runs one process). Competing requests receive HTTP 409
with `evaluation_in_progress`. The dashboard button calls this same public API
and replaces the displayed report with the returned result.

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Optional OpenAI-compatible adapter

The benchmark uses a scripted policy. A separate adapter can request one
validated `AgentAction` from an OpenAI-compatible
`/chat/completions` endpoint. Remote URLs must use HTTPS; plaintext HTTP is
accepted only for `localhost` or loopback-IP development. Redirects are
disabled. The default connect and read limits are 5 and 30 seconds, with a
45-second overall HTTP request/read deadline. Responses are bounded while
streaming to 1 MiB by default (validated maximum: 16 MiB). The adapter requests
identity encoding and rejects encoded responses before reading their bodies, so
decompression cannot occur ahead of the byte ceiling. The API key is loaded
from the caller-selected environment variable, included in trace redaction, and
rejected if a provider reflects it inside a returned action.

```python
from agent_reliability_lab.providers.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatiblePolicy,
)

policy = OpenAICompatiblePolicy(
    OpenAICompatibleConfig(
        base_url="https://provider.example/v1",
        model="your-model",
        api_key_env="PROVIDER_API_KEY",
        total_timeout_seconds=45.0,
        max_response_bytes=1_048_576,
    )
)
```

The adapter is a library interface; the default CLI policy does not use it.
Model quality needs a separate evaluation with repeated runs. The adapter has
no outbound destination allowlist or network sandbox, so egress restrictions
would need to be configured outside the application.

## Local checks

```bash
uv sync --dev --locked
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src

npm ci --prefix web
npm --prefix web test -- --run
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run build

uv run arl eval scenarios/incident-response \
  --output artifacts/final-report.json
uv run arl gate artifacts/final-report.json \
  --baseline benchmarks/baseline-report.json
```

GitHub Actions runs Python, frontend, benchmark, and container checks. The
container job builds both images and starts the Compose stack. The benchmark
job generates a report from a clean Git checkout and compares it with the
committed baseline.
