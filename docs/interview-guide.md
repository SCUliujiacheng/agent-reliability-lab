# Interview guide

This document is a concise map for explaining Agent Reliability Lab in a
technical interview. It emphasizes decisions and evidence rather than a tour of
every file.

## Thirty-second summary

> Agent Reliability Lab is a local-first test bench for tool-using agents. It
> runs the same frozen incident scenarios through fragile and resilient
> execution, persists every state transition and tool attempt, supports durable
> human approval, and turns the resulting traces into an exact regression gate
> and an explorable dashboard.

## Five-minute demo

1. Open the dashboard after running the frozen evaluation. Point out the
   66.7% versus 100% correctness result and the exact six-case denominator.
2. Launch `timeout-recovery` in resilient mode. Open the trace and follow the
   injected timeout, failed attempt, retry, successful attempt, checkpoint, and
   terminal result in sequence.
3. Launch `approval-reconstruction`. Explain that the service can be rebuilt
   around the same SQLite database before approval. Allow the action and show
   the single `approval.recorded` event and single write execution.
4. Export the trace JSON, then run the CLI gate against the committed baseline.
5. Open the interactive architecture and use the two guided views to contrast
   the runtime request path with the evaluation path.

## Design decisions worth discussing

### Why a scripted policy?

The default benchmark isolates orchestration reliability from model variance,
credentials, rate limits, and cost. This makes failures reproducible and lets
the exact grader prove a narrow claim. An OpenAI-compatible policy adapter is
available as an integration boundary, but its behavior is deliberately not
folded into the deterministic headline result.

### Why SQLite?

The project needs durable reconstruction, transactions, uniqueness, and
compare-and-swap behavior without an external service. SQLite with WAL is a
good single-node demonstration substrate. The code treats persistence as the
coordination boundary, including concurrent approval decisions, while the
documentation explicitly avoids claiming multi-node production readiness.

### How is exactly-once behavior approached?

There is no universal exactly-once network guarantee. The project implements a
bounded local contract: approval recording is atomic, run transitions use
optimistic state/version checks, high-risk writes require idempotency keys, and
tool results are cached behind a claim lease. Concurrent duplicate approvals
therefore converge on one durable decision and one write in the tested SQLite
deployment.

### Why not trust summary metrics in JSON?

An evaluation artifact can be edited. The gate validates schema and provenance,
rebuilds metrics from trace-level evidence, checks scenario/action identities
and deterministic outputs, and then applies exact-fraction thresholds. This
makes the artifact auditable and causes corruption to fail closed.

### What is the security boundary?

Only registered tools can run; arbitrary shell execution is absent. Inputs and
outputs are validated with Pydantic. Traces are sanitized before persistence,
request bodies are bounded, CORS origins are explicit, and public API responses
use narrow DTOs and stable errors. These controls reduce risk, but the demo has
no authentication or tenant isolation and must not be exposed as a production
control plane.

## Likely follow-up questions

**How would you scale it beyond one process?**  Move durable state to PostgreSQL,
replace local claim timing with database-backed leases using server time, use a
queue for resumable execution, and preserve the same idempotency and trace
contracts. Add migrations and contention/load tests before horizontal scale.

**How would you evaluate a real model?**  Keep the frozen scripted suite as the
orchestration control, add a separate provider-backed suite, record model and
prompt versions, repeat cases over seeds, separate deterministic safety checks
from statistical quality metrics, and publish uncertainty and cost.

**How would you prevent approval replay?**  Bind approval to run ID, pending
action fingerprint, actor, decision, and current version; consume it
transactionally with the state transition; expire stale approvals; and require
authenticated, authorized actors in production.

**What failure was hardest?**  Cross-process approval races are more subtle than
button debouncing. The final tests instantiate two application objects over the
same database and force competing decisions, proving same-decision idempotency
and conflicting-decision convergence at the persistence boundary.

**What would you build next?**  PostgreSQL migrations, authenticated users and
RBAC, distributed workers, OpenTelemetry export, property-based state-machine
tests, and a separate statistically grounded model evaluation track.

## Honest limitations

- Six synthetic scenarios cannot represent real-world incident diversity.
- The benchmark policy is scripted, so no claim is made about LLM reasoning
  quality.
- SQLite and in-process execution target a local single-node demo.
- There is no authentication, authorization, tenancy, or secrets manager.
- Tool side effects are simulated; this is not a production incident executor.

These constraints are deliberate. They keep the repository runnable by a
reviewer while making the tested reliability contracts precise.
