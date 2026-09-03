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

The repository demonstrates local single-node agent orchestration. It includes
input/output validation, a registered-tool allowlist, bounded request bodies,
explicit CORS origins, trace redaction, durable approvals, idempotency, and
non-root container images.

It does **not** provide authentication, authorization, tenant isolation, a
secrets manager, hardened network policy, or a production incident-response
control plane. Do not expose the demo API to an untrusted network or connect the
simulated tools to production systems without adding those controls and
performing a dedicated security review.
