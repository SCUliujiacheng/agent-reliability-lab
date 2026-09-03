# Data and scenario provenance

Agent Reliability Lab is intentionally self-contained. The default demo and
benchmark use no scraped corpus, production incident data, customer records,
or hidden model-generated labels.

## What the repository contains

- Six hand-authored YAML scenarios under `scenarios/incident-response/`.
- A deterministic in-memory incident backend with typed inputs and outputs.
- Explicit fault rules that activate at a named tool, logical action, and
  attempt number.
- Expected tool sequences and outcomes stored beside each scenario.
- A committed JSON report containing the manifest, hashes, ordered trace
  evidence, exact metrics, and execution provenance.

No scenario contains personal data. Example service names, deployment IDs, log
messages, actors, and incident descriptions are synthetic fixtures created for
this repository.

## Frozen suite manifest

| Scenario | Purpose | Injected fault | Expected outcome |
| --- | --- | --- | --- |
| `normal-success` | Control path without a fault | none | `diagnosed` |
| `timeout-recovery` | Verify bounded retry after a timeout | first `search_recent_logs` attempt | `diagnosed` |
| `rate-limit-recovery` | Verify bounded retry after rate limiting | first `get_deployment` attempt | `diagnosed` |
| `malformed-output-rejected` | Exercise output-schema rejection | malformed `get_deployment` response | `invalid_output` |
| `permanent-invalid-input` | Reject invalid input before execution | none; input is permanently invalid | `invalid_input` |
| `approval-reconstruction` | Rebuild services, approve, and execute a write once | durable human approval boundary | `prepared` |

## Identity and integrity

Each scenario is loaded into a strict model and hashed from its exact file
bytes. The evaluation report also carries a canonical suite manifest with:

- relative path, scenario ID, version, and SHA-256;
- initial context and declared faults;
- canonical logical actions and action fingerprints;
- expected output digests for deterministic tools;
- expected tool sequence, outcome, and approval requirement.

The suite hash commits to that complete manifest. The regression gate does not
trust the report's headline metrics; it reconstructs them from cases and ordered
trace evidence, validates trace and scenario identities, and rejects
incomparable or internally inconsistent inputs.

## Runtime evidence

Every run receives distinct run and trace UUIDs. Events have monotonically
increasing sequence numbers and retain the minimum payload needed to explain:

- policy decisions;
- tool attempts, faults, failures, retries, and validated successes;
- durable checkpoints;
- approval decisions;
- terminal success or failure.

Stored and exported payloads pass through recursive redaction. Authorization
fields and configured secret values are replaced before persistence, and the
API exposes a deliberately narrower trace DTO than the internal event model.

## Reproduction contract

```bash
uv sync --dev --locked
uv run arl eval scenarios/incident-response --output artifacts/current-report.json
uv run arl gate artifacts/current-report.json --baseline benchmarks/baseline-report.json
```

The scripted benchmark is deterministic in behavior, but runtime UUIDs,
timestamps, and latency measurements vary. Baseline normalization preserves
claim-relevant evidence while keeping version-controlled provenance explicit.

## Extending the suite responsibly

New scenarios should introduce one clearly named behavior, declare exact
expected outcomes, and add tests for both the scenario loader and the gate.
Do not mix real credentials or production logs into YAML fixtures. If a new
backend is nondeterministic, keep it out of the headline exact benchmark or
publish a separate evaluation with its own grader and limitations.
