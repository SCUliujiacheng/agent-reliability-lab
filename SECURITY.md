# Security policy

## Supported versions

Agent Reliability Lab is a portfolio and research demonstration. Security fixes
are applied to the latest commit on `main`; older snapshots are not maintained
as supported releases.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting flow:

<https://github.com/SCUliujiacheng/agent-reliability-lab/security/advisories/new>

Do not include credentials, access tokens, personal data, or production traces
in a public issue. A useful report includes the affected revision, impact,
minimal reproduction, and any suggested mitigation.

## Scope and threat boundary

The repository demonstrates local single-node agent orchestration. Inbound HTTP
is local by default: Compose publishes both services only on loopback, FastAPI
checks an exact Host allowlist, and Nginx rejects unknown virtual hosts. Browser
responses include `Content-Security-Policy: frame-ancestors 'none'` and
`X-Frame-Options: DENY`. Application request bodies and list queries are
bounded, and configured CORS origins are explicit.

Only registered, schema-validated tools can execute. Approval requests must
echo the exact currently pending action step and fingerprint; SQLite verifies
and records that binding atomically. Pending arguments and trace payloads are
recursively sanitized. Actor names remain caller-provided labels rather than
authenticated identities.

Every run also enforces a bounded policy-call budget (64 by default,
configurable from 1 to 1024). A slot is reserved in durable state before each
new policy invocation, so cancellation cannot reset the allowance; tool retries
remain attempts inside one returned action. Approval reconstruction reuses the
action selected before the pause rather than reserving twice. Before any call
beyond the limit, the run fails durably with `action_budget_exhausted`.

The optional provider adapter requires HTTPS for non-loopback destinations,
disables redirects, enforces connect/read/total deadlines and a streamed
response-size ceiling, and includes the active API-key value in trace
redaction. It is not an outbound destination allowlist or network sandbox.
The generic `Policy` protocol does not add a universal deadline around custom
implementations; custom policies must bound their own I/O. The action budget
limits invocation count rather than the duration of one invocation.

It does **not** provide authentication, authorization, tenant isolation, a
secrets manager, hardened network policy, or a production incident-response
control plane. Do not expose the demo API to an untrusted network or connect the
simulated tools to production systems without adding those controls and
performing a dedicated security review.
