# Agent Reliability Lab

> A local-first, evidence-driven test bench for building tool-using agents that
> retry safely, pause for human approval, survive reconstruction, and fail
> closed when their reliability claims cannot be verified.

[![CI](https://github.com/SCUliujiacheng/agent-reliability-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/SCUliujiacheng/agent-reliability-lab/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Node 22.20+](https://img.shields.io/badge/node-22.20%2B-339933?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-0F766E.svg)](LICENSE)

![Agent Reliability Lab dashboard](docs/screenshots/dashboard-overview.png)

Most agent demos show a happy path. This repository makes failure behavior the
main artifact: exact scenario contracts, a durable state machine, schema-first
tools, ordered sanitized traces, human approval, reproducible fault injection,
and a regression gate that reconstructs metrics from evidence instead of
trusting a summary JSON file.

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
fragile mode stops after one attempt. See [benchmark results](docs/benchmark-results.md)
for denominators, grader definitions, provenance, and limitations.

## What is implemented

- **Durable orchestration** — explicit run states, checkpoints, optimistic
  version checks, execution leases, restart-safe resume, and terminal-state
  enforcement.
- **Schema-first tool boundary** — registered tools only, strict Pydantic input
  and output validation, bounded timeouts/retries, deterministic fault injection,
  idempotency keys, and cached results.
- **Human approval that survives reconstruction** — approval is persisted before
  execution; duplicate same-decision requests converge, conflicting decisions
  return a stable conflict, and the simulated write occurs once.
- **Auditable evaluation** — six versioned scenarios, exact graders, per-case
  traces, suite/action/output hashes, evidence integrity checks, and a
  baseline-aware CI gate.
- **Operational surface** — FastAPI with bounded requests and stable errors,
  Typer CLI, React/TypeScript dashboard, JSON trace export, containers, and CI.
- **Privacy-aware telemetry** — secrets and authorization fields are recursively
  redacted before persistence; the API publishes narrower trace DTOs.

## Architecture

![Agent Reliability Lab architecture](docs/architecture/agent-reliability-lab-architecture.visual-check.1440x900.light.png)

The browser/API path and CLI/evaluation path share the same deterministic runtime
contracts. SQLite is the single-node coordination and evidence boundary; the
versioned JSON baseline is a separate regression contract.

Open the [interactive architecture](docs/architecture/agent-reliability-lab-architecture.html)
for guided views, search, relationship tracing, light/dark themes, and export.

## Credential-free quickstart

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js 22.20+.

```bash
git clone https://github.com/SCUliujiacheng/agent-reliability-lab.git
cd agent-reliability-lab

uv sync --dev --locked
npm ci --prefix web

# Terminal 1: API
uv run uvicorn agent_reliability_lab.api.app:create_app --factory --host 127.0.0.1 --port 8000

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

# Start a durable approval scenario
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H "content-type: application/json" \
  -d '{"scenario_id":"approval-reconstruction","mode":"resilient"}'

# Approve using the returned run ID
curl -X POST http://127.0.0.1:8000/v1/runs/<run-id>/approvals \
  -H "content-type: application/json" \
  -d '{"actor":"demo-reviewer","allow":true,"reason":"trace verified"}'

curl "http://127.0.0.1:8000/v1/runs/<run-id>/trace?limit=100"
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Optional OpenAI-compatible policy boundary

The exact benchmark intentionally does not call a model. A separate adapter can
request one strict `AgentAction` from an OpenAI-compatible
`/chat/completions` endpoint, with explicit connect/read deadlines and an
environment-variable name chosen by the caller:

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
    )
)
```

This adapter is a tested library boundary, not the default CLI policy. Provider
quality needs a separate repeated, statistical evaluation; it is not presented
as part of the deterministic headline result.

## Verification

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

uv run arl eval scenarios/incident-response --output artifacts/final-report.json
uv run arl gate artifacts/final-report.json --baseline benchmarks/baseline-report.json
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
docs/           architecture, benchmark semantics, provenance, and interview guide
```

## Deliberate limitations

- The headline suite has six synthetic incident scenarios; it does not model
  real-world incident diversity.
- The default policy is scripted, so the benchmark measures orchestration and
  tool-boundary reliability—not LLM reasoning quality.
- SQLite and in-process execution target a local, single-node demonstration;
  there are no database migrations or distributed workers.
- The demo has no authentication, RBAC, tenant isolation, or secrets manager.
- Tool side effects are simulated. This is not a production incident executor.

These constraints keep the project runnable by a reviewer and make each claim
precise. The next production-oriented steps would be PostgreSQL migrations,
authenticated approvals, distributed leases/workers, OpenTelemetry export, and
a separate statistically grounded provider evaluation track.

## Interview shortcuts

- Why exact trace-derived graders instead of LLM-as-judge?
- How do approval races converge across two application instances?
- Where does idempotency stop, and what would change for an external side effect?
- How does the gate distinguish a product regression from corrupted evidence?
- Which contracts survive a move from SQLite to PostgreSQL and worker queues?

Answers and a five-minute demo path are in the [interview guide](docs/interview-guide.md).
Scenario origins and integrity fields are documented in
[data and scenario provenance](docs/data-and-scenario-provenance.md).

## License

[MIT](LICENSE)
