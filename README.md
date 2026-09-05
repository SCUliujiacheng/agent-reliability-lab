<h1 align="center">Agent Reliability Lab</h1>

<p align="center">
  <strong>A local-first lab for agents that retry safely, pause for human approval, survive reconstruction, and fail closed.</strong>
</p>

<p align="center">
  <a href="https://github.com/SCUliujiacheng/agent-reliability-lab/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SCUliujiacheng/agent-reliability-lab/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://www.python.org/"><img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="https://react.dev/"><img alt="React" src="https://img.shields.io/badge/React-TypeScript-149ECA?logo=react&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/License-MIT-0F766E"></a>
</p>

<p align="center">
  <a href="#why-i-built-this">Why I built this</a> ·
  <a href="#measured-result">Measured result</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#run-it-locally">Run locally</a> ·
  <a href="#five-minute-technical-tour">Technical tour</a>
</p>

<p align="center">
  <strong>English</strong> · <a href="https://github.com/SCUliujiacheng/agent-reliability-lab-zh">简体中文</a>
</p>

## Why I built this

Most agent demos stop once the happy path works. I wanted to look at the part
that usually gets skipped: what happens after a tool call fails? Can the run
say where it stopped, resume without doing the risky thing twice, and leave a
record that someone else can inspect?

That is the scope here. State, tool attempts, approval decisions, and traces
stay in one inspectable path; the evaluator rebuilds its result from those
records. It is a small test bench for pulling at those boundaries, not a
production incident executor.

<p align="center">
  <img src="docs/screenshots/dashboard-overview.png" alt="Agent Reliability Lab dashboard showing the benchmark comparison and trace evidence" width="100%">
</p>
<p align="center"><sub>The dashboard runs the same versioned evaluation exposed by the CLI and API.</sub></p>

## Measured result

The committed benchmark runs the same six frozen scenarios through both modes.
It uses a deterministic scripted policy and synthetic local tools—no API key,
network request, GPU, or paid service.

| Exact metric | Fragile | Resilient | Change |
| --- | ---: | ---: | ---: |
| Task correctness | 4 / 6 (66.7%) | 6 / 6 (100.0%) | **+33.3 pp** |
| Transient-fault recovery | 0 / 2 (0.0%) | 2 / 2 (100.0%) | **+100.0 pp** |
| Tool-sequence accuracy | 94.4% | 100.0% | **+5.6 pp** |
| Invalid outputs accepted | 0 | 0 | unchanged |
| Unnecessary logical calls | 0 | 0 | unchanged |

The contrast comes from a first-attempt timeout and rate limit. Resilient mode
records the failure, retries within policy, and reaches the declared outcome;
fragile mode stops after one attempt. Inspect the
[machine-readable baseline](benchmarks/baseline-report.json),
[benchmark results](docs/benchmark-results.md), and
[gate/provenance contract](docs/data-and-scenario-provenance.md) for the exact
denominators, grader definitions, reconstruction rules, and limitations.

## What I ended up building

The project follows a few fairly ordinary constraints that matter once a run
has failed:

- **Durable orchestration** — explicit run states, checkpoints, optimistic
  version checks, execution leases, restart-safe resume, and terminal-state
  enforcement. Externally visible run transitions and their audit events share
  one SQLite transaction.
- **Bounded agent execution** — each run permits at most 64 new policy calls by
  default (configurable from 1 to 1024). Each slot is reserved durably before
  invocation, while tool retries remain attempts within one logical action;
  exhaustion is persisted and traced as `action_budget_exhausted` before
  another policy call can occur.
- **Schema-first tool boundary** — registered tools only, strict Pydantic input
  and output validation, at most five attempts and 60 seconds per handler
  attempt, deterministic fault injection, idempotency keys, and cached results.
- **Approval bound to the reviewed action** — the API exposes a sanitized
  pending-action descriptor with the action step, SHA-256 fingerprint, tool
  name, and arguments. SQLite accepts a decision atomically only while that
  exact action is current; exact duplicates converge and stale or conflicting
  decisions fail closed.
- **Auditable evaluation** — six versioned scenarios, exact graders, per-case
  traces, suite/action/output hashes, evidence integrity checks, and a
  baseline-aware CI gate.
- **Operational surface** — FastAPI with bounded requests, exact trusted hosts,
  and stable application-route errors; Typer CLI; React/TypeScript dashboard;
  JSON trace export; containers; and CI.
- **Privacy-aware telemetry** — trace payloads and tool results are recursively
  sanitized before persistence, and the API publishes narrower DTOs. Durable
  pending-action arguments stay exact for reconstruction and must not contain
  credentials.

## Architecture

<p align="center">
  <img src="docs/architecture/agent-reliability-lab-architecture.visual-check.1440x900.light.png" alt="Agent Reliability Lab system architecture" width="100%">
</p>

The browser/API path and CLI/evaluation path share the same deterministic runtime
contracts. SQLite is the single-node coordination and evidence boundary; the
versioned JSON baseline is a separate regression contract.

Open the [interactive architecture](docs/architecture/agent-reliability-lab-architecture.html)
for guided views, search, relationship tracing, light/dark themes, and export.

## Run it locally

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js 22.20+.

```bash
git clone https://github.com/SCUliujiacheng/agent-reliability-lab.git
cd agent-reliability-lab

uv sync --dev --locked
npm ci --prefix web

# Terminal 1: API
uv run uvicorn agent_reliability_lab.api.app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000

# Terminal 2: dashboard (proxies /v1 to the API)
npm --prefix web run dev
```

Open `http://127.0.0.1:5173`, run an evaluation, then replay a scenario from the
dashboard.

### Docker Compose

```bash
docker compose up --build
```

The Compose stack runs both containers as non-root users, serves the dashboard
and `/v1` through one origin, and keeps the SQLite database in a named volume.

The complete process-environment reference is in [`.env.example`](.env.example).
The application does not load that file automatically, and Compose declares its
own values explicitly. `ARL_TRUSTED_HOSTS` replaces the API allowlist; changing
the dashboard hostname also requires a matching `server_name` in
`web/nginx.conf`.

## Reproduce the benchmark

```bash
uv run arl eval scenarios/incident-response \
  --output artifacts/current-report.json

uv run arl compare artifacts/current-report.json

uv run arl gate artifacts/current-report.json \
  --baseline benchmarks/baseline-report.json
```

Expected gate output:

```text
PASS
```

The gate first validates report structure, suite identity, trace uniqueness,
ordered event semantics, deterministic outputs, and recomputed summaries. A
tampered or incomparable artifact is an infrastructure failure, not a passing
score.

## Inspect one failure-and-recovery trace

```bash
uv run arl run scenarios/incident-response/timeout-recovery.yaml \
  --mode resilient \
  --database .arl-data/demo.db \
  --json
```

The trace shows this evidence chain:

```text
search_recent_logs attempt 1
  -> timeout injected
  -> transient failure
search_recent_logs attempt 2
  -> validated success
  -> durable checkpoint
run succeeded: diagnosed
```

![Recovered timeout trace](docs/screenshots/trace-detail.png)

Export a run after copying its `run_id` from the command output:

```bash
uv run arl export-trace <run-id> \
  --database .arl-data/demo.db \
  --output artifacts/trace.json
```

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

## Optional OpenAI-compatible policy boundary

The exact benchmark intentionally does not call a model. A separate adapter can
request one strict `AgentAction` from an OpenAI-compatible
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

This adapter is a tested library boundary, not the default CLI policy. Provider
quality needs a separate repeated, statistical evaluation; it is not presented
as part of the deterministic headline result. The adapter does not provide an
outbound destination allowlist or network sandbox; production deployments must
restrict egress independently.

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

GitHub Actions runs independent Python, frontend, benchmark, and container jobs.
The container job builds both images and exercises the Compose stack; the
benchmark job enforces the committed evidence contract.

## Repository map

```text
src/agent_reliability_lab/
  api/          FastAPI adapter and narrow response contracts
  domain/       immutable actions, runs, and scenario models
  evaluation/   exact graders, report provenance, and fail-closed gate
  providers/    strict OpenAI-compatible policy adapter
  runtime/      checkpointed orchestration and approval-aware services
  storage/      SQLite transactions, CAS, leases, and durable evidence
  telemetry/    ordered events and recursive redaction
  tools/        registry, validation, retries, faults, and idempotency
web/            React + TypeScript evidence dashboard
scenarios/      frozen synthetic YAML suite
benchmarks/     committed baseline report
docs/           architecture, benchmark semantics, provenance, and technical tour
```

## What this project does not prove

- The headline suite has six synthetic incident scenarios; it does not model
  real-world incident diversity.
- The default policy is scripted, so the benchmark measures orchestration and
  tool-boundary reliability—not LLM reasoning quality.
- SQLite and in-process execution target a local, single-node demonstration;
  there are no database migrations or distributed workers.
- The demo has no authentication, RBAC, tenant isolation, or secrets manager.
- The generic `Policy` protocol does not impose a universal per-call deadline;
  custom policies must bound their own I/O. The optional HTTP provider does have
  a 45-second total deadline, and the action budget bounds call count, not call
  duration.
- Tool side effects are simulated. This is not a production incident executor.

These constraints keep the local result narrow enough to check. Moving toward
production would mean PostgreSQL migrations, authenticated approvals,
distributed leases/workers, OpenTelemetry export, and a separate statistically
grounded provider-evaluation track. None of that is claimed here.

## Five-minute technical tour

The quickest way through the design is to run `timeout-recovery`, open its
trace, and then compare the report against the committed baseline. Along the
way, these are the questions worth asking:

1. Why use exact trace-derived graders instead of LLM-as-judge?
2. How do approval races converge across two application instances?
3. Where does idempotency stop for an external side effect?
4. How does the gate distinguish a product regression from corrupted evidence?
5. Which contracts survive a move to PostgreSQL and worker queues?

The answers and a concrete walkthrough are in the
[technical design tour](docs/technical-tour.md).
Scenario origins and integrity fields are documented in
[data and scenario provenance](docs/data-and-scenario-provenance.md).

## License

[MIT](LICENSE)
