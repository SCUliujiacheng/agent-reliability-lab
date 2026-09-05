# Agent Reliability Lab technical design tour

Start with `timeout-recovery`, then open its trace. It is the quickest way to
see what this project is checking: the first log lookup times out, resilient
mode records that failure, retries inside its boundary, and leaves a checkpoint
and result behind. Then hand the report to the CLI gate and compare it with the
committed baseline.

## Walk through it once

1. Open the dashboard, click **Run evaluation**, and inspect the returned
   report. Start with the 66.7% versus 100% correctness result and the exact
   six-case denominator.
2. Launch `timeout-recovery` in resilient mode. Open the trace and follow the
   injected timeout, failed attempt, retry, successful attempt, checkpoint, and
   terminal result in sequence.
3. Launch `approval-reconstruction`. Rebuild the service around the same SQLite
   database before approval. Allow the action and show
   the single `approval.recorded` event and single write execution.
4. Export the trace JSON, then run the CLI gate against the committed baseline.
5. Open the interactive architecture and compare the **Interactive run** and
   **Evaluation gate** guided views; use **Security boundaries** to explain
   where the guarantees stop.

## Design decisions and guarantee boundaries

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
coordination boundary, including cross-instance approval decisions, while the
documentation explicitly avoids claiming multi-node production readiness.

### What happens to duplicate approvals?

This is not a claim of universal exactly-once behavior on a network. The tested
case is narrower: two application instances share one SQLite database and send
decisions for the same waiting action. Approval recording is transactional, run
transitions use optimistic state/version checks, high-risk writes need an
idempotency key, and tool results are cached behind a claim lease. In that case,
matching concurrent decisions converge on one durable decision and one write;
stale or conflicting decisions are rejected.

### Why not trust summary metrics in JSON?

An evaluation artifact can be edited. The gate validates schema and provenance,
rebuilds metrics from trace-level evidence, checks scenario/action identities
and deterministic outputs, and then applies exact-fraction thresholds. This
makes the artifact auditable and causes corruption to fail closed.

### What is the security boundary?

Only registered tools can run; arbitrary shell execution is absent. Inputs and
outputs are validated with Pydantic. Traces are sanitized before persistence,
request bodies are bounded, CORS origins are explicit, Host values use an exact
allowlist, and Nginx rejects unknown virtual hosts. Compose browser responses
deny framing. The optional provider requires remote HTTPS, disables redirects,
requests identity encoding, rejects encoded responses before body iteration,
and bounds total time and streamed response bytes. Its credential is redacted,
and a returned action that reflects the credential is rejected before
persistence.
Each run also bounds new policy calls with durable pre-invocation reservations;
tool retries do not consume extra slots, and exhaustion is persisted before
another policy call.
Application routes use narrow DTOs and stable JSON errors; the outer Host
boundary can instead return a plain 400 or empty Nginx 444. These controls
reduce risk, but the demo has no authentication or tenant isolation and must
not be exposed as a production control plane.

## Possible extensions

### Multi-process execution

Durable state can move to PostgreSQL, while local claim timing would need
database-backed leases based on server time. A queue can resume work without
changing the idempotency and trace contracts. Migrations and contention/load
tests belong before horizontal scale.

### Real-model evaluation

The frozen scripted suite remains the orchestration control. A separate
provider-backed suite would record model and prompt versions, repeat cases over
seeds, and report statistical quality separately from deterministic checks.

### Approval replay

The client echoes the run's current action step and fingerprint. One SQLite
transaction records a decision only while the run is waiting for that exact
action; stale targets are rejected and exact duplicates converge.
Authentication, authorization, trusted actor identity, and approval expiry are
not implemented.

### Action budget

Each new policy call reserves one durable slot before invocation, so
cancellation cannot reset the allowance. A returned `finish` consumes that
reservation; tool retries do not. A pending approval resumes the action chosen
before the pause without another reservation. At the limit, the runtime writes
the terminal state and `run.failed` with `action_budget_exhausted`, without one
more policy call.

### Concurrent approvals

The tests create two application objects over one SQLite database and submit
competing decisions. Identical decisions are idempotent. If the decisions
conflict, only one is stored and the other request receives HTTP 409.

### Further experiments

I would keep PostgreSQL migrations, authenticated users and RBAC, distributed
workers, OpenTelemetry export, property-based state-machine tests, and
real-model evaluation in separate experiments rather than folding them into
the six fixed cases here.

## Scope and limits

- Six synthetic scenarios cannot represent real-world incident diversity.
- The benchmark policy is scripted, so no claim is made about LLM reasoning
  quality.
- SQLite and in-process execution target a local single-node demo.
- There is no authentication, authorization, tenancy, or secrets manager.
- Tool side effects are simulated; this is not a production incident executor.

I stopped at a single node because it is enough to observe retries, approvals,
and reconstruction. Distributed behavior needs a separate set of experiments.
