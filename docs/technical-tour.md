# Runtime implementation notes

`timeout-recovery` is the shortest path through the implementation. The first log lookup receives an injected timeout. Resilient mode records the failure, retries, and saves a checkpoint and result. The case checks that the retry belongs to the same logical action, the failed attempt remains visible, and the grader can recover that sequence from the trace.

## Follow a run

```text
uv run arl run scenarios/incident-response/timeout-recovery.yaml --mode resilient --database .arl-data/demo.db --json
```

Copy the returned `run_id` and export its trace:

```text
uv run arl export-trace <run-id> --database .arl-data/demo.db --output artifacts/trace.json
```

The expected order is an injected timeout, failed attempt, retry, successful attempt, checkpoint, and terminal result. The dashboard presents the same kind of record; see the [trace screenshot](screenshots/trace-detail.png).

`approval-reconstruction` checks a different path. A run pauses for approval, the service is rebuilt around the same SQLite database, and approval resumes the action. Repeated identical decisions should leave one `approval.recorded` event and one write execution.

## Fixed policies and tools

The current experiment checks orchestration: given the same actions and faults, what do the two execution modes do? A scripted policy and local tools make those conditions repeatable and give the grader deterministic outputs to compare.

The optional OpenAI-compatible adapter provides a model integration point, but does not participate in this benchmark. A real model changes action selection and tool order. Evaluating it would require recorded model and prompt versions and repeated runs to observe variation.

## What SQLite stores

Run states, checkpoints, approvals, and events share one database. A state transition and its event are written in one transaction. Optimistic version checks prevent stale updates; WAL supports local concurrent access. Reconstructed application objects can read the pending work back from storage.

The storage layer also coordinates execution leases and idempotent results. Tests cover application instances sharing one SQLite database. They do not cover multiple database nodes, queues, or database migrations.

## Concurrent approvals

An approval must include the current `action_step` and its SHA-256 fingerprint. One transaction records the decision only while the run is waiting for that action. Matching concurrent decisions resolve to one stored record. If two decisions conflict, one request receives HTTP 409.

High-risk writes require an idempotency key, and tool results are cached after acquiring an execution lease. The tested result is one decision and one simulated write across two application instances sharing a SQLite database. That does not imply exactly-once behavior for arbitrary external services.

The `actor` field is currently a request string. Authentication, authorization, and approval expiry are not implemented.

## Call budget and cancellation

Each new policy invocation reserves a durable slot before the call. The default is 64 calls, configurable from 1 to 1024. Cancellation and reconstruction do not reset that allowance. A call returning `finish` consumes its slot; tool retries do not consume additional slots.

Approval resumes the action selected before the pause without reserving again. On exhaustion, the runtime writes the terminal state and a `run.failed` event containing `action_budget_exhausted`, then stops making policy calls.

The budget bounds call count, not call duration. Custom `Policy` implementations must limit their own I/O. The optional HTTP adapter has a 45-second total deadline.

## Recomputing reports

A JSON report can be edited, leaving its summary inconsistent with its trace. The gate checks scenario and action identity, event order, uniqueness, and deterministic outputs before recomputing metrics and comparing them with the baseline. Inconsistent evidence produces an infrastructure error instead of `PASS`.

See [scenario and report provenance](data-and-scenario-provenance.md) for identity and hash rules, and [benchmark results](benchmark-results.md) for metric denominators.

## Interface and data limits

Only registered tools with valid Pydantic inputs can run. Outputs are validated again, stored payloads are recursively redacted, and API responses contain fewer fields than internal events.

Request bodies are bounded, CORS origins are configured explicitly, the API checks Host values, and Nginx rejects unknown virtual hosts. Compose browser responses deny framing. Application routes return stable JSON errors, while the outer Host check may return a plain 400 or an empty Nginx 444.

The optional model adapter constrains remote HTTPS, redirects, response encoding, total time, and response bytes; see [local development](local-development.md). These controls do not add authentication or tenant isolation, and tool side effects remain simulated.

## Separate follow-up experiments

Multiple workers would require database leases, server-side time, and a recovery queue, followed by contention and failure-timing tests. A PostgreSQL migration also needs schema migrations and load tests.

For real models, the scripted suite can remain a control while a separate suite records model and prompt versions, repeats runs, and reports deterministic checks alongside quality statistics. These experiments answer different questions and should have separate results.

The [architecture diagram](architecture/agent-reliability-lab-architecture.html) shows the component relationships.
