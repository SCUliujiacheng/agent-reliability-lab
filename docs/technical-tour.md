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

## Questions to take further

**How would you scale it beyond one process?**  Move durable state to PostgreSQL,
replace local claim timing with database-backed leases using server time, use a
queue for resumable execution, and preserve the same idempotency and trace
contracts. Add migrations and contention/load tests before horizontal scale.

**How would you evaluate a real model?**  Keep the frozen scripted suite as the
orchestration control, add a separate provider-backed suite, record model and
prompt versions, repeat cases over seeds, separate deterministic safety checks
from statistical quality metrics, and publish uncertainty and cost.

**How is approval replay bounded today?**  The client must echo the run's
current action step and fingerprint. One SQLite transaction records a decision
only while the run is still waiting for that exact action; stale targets are
rejected and exact duplicates converge. Authentication, authorization, trusted
actor identity, and approval expiry remain production work.

**What does the action budget count?**  Each new policy call reserves one
durable slot before invocation, so cancellation cannot reset the allowance. A
returned `finish` consumes that reservation; tool retries do not. A pending
approval resumes the action selected before the pause without another
reservation. At the limit, the runtime atomically records terminal state and
`run.failed` with `action_budget_exhausted`, without one more policy call.

**Which failure exposed the deepest design problem?**  Cross-instance approval
races are more subtle than button debouncing. The tests instantiate two
application objects over the same SQLite database and force competing
decisions, proving same-decision idempotency and conflicting-decision
convergence at that persistence boundary.

**What comes next?**  PostgreSQL migrations, authenticated users and
RBAC, distributed workers, OpenTelemetry export, property-based state-machine
tests, and a separate statistically grounded model evaluation track.

## Scope and limits

- Six synthetic scenarios cannot represent real-world incident diversity.
- The benchmark policy is scripted, so no claim is made about LLM reasoning
  quality.
- SQLite and in-process execution target a local single-node demo.
- There is no authentication, authorization, tenancy, or secrets manager.
- Tool side effects are simulated; this is not a production incident executor.

These constraints are deliberate. They keep the repository runnable from a
clean local checkout while making the tested reliability contracts precise.
