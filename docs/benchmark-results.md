# Benchmark results

Agent Reliability Lab evaluates the same six deterministic incident-response
scenarios in two execution modes:

- **fragile**: one attempt per tool call;
- **resilient**: bounded retries for errors classified as transient.

The committed baseline is an evidence artifact, not a claim about arbitrary
agents or production workloads. It uses the local scripted policy, synthetic
tools, and exact graders; it makes no network or model-provider calls.

## Headline result

| Metric | Fragile | Resilient | Difference |
| --- | ---: | ---: | ---: |
| Task correctness | 4 / 6 (66.7%) | 6 / 6 (100.0%) | +33.3 percentage points |
| Verified transient-fault recovery | 0 / 2 (0.0%) | 2 / 2 (100.0%) | +100.0 percentage points |
| Tool-sequence accuracy | 94.4% | 100.0% | +5.6 percentage points |
| Invalid outputs accepted | 0 / 8 | 0 / 11 | 0 in both modes |
| Unnecessary logical calls | 0 | 0 | unchanged |
| Retry attempts | 0 | 2 | +2, both evidence-backed |

The two contrast cases are `timeout-recovery` and `rate-limit-recovery`.
Fragile execution fails after the injected first-attempt fault; resilient
execution records the failed attempt, retries once, and reaches the declared
outcome.

## Exact denominators

### Task correctness

`correct cases / all cases`, evaluated independently for each mode. A case is
correct only when its observed outcome matches the frozen scenario outcome and
the trace-derived terminal semantics agree.

### Recovery rate

`recovered verified transient faults / verified transient faults`.

The denominator is reconstructed from declared faults and ordered trace
evidence. A retry alone is not counted as recovery: the matching logical action
must subsequently succeed. The frozen suite has two verified transient faults
per mode—one timeout and one rate limit.

### Tool-sequence accuracy

For each case, the grader computes the longest common subsequence between the
expected and observed logical tool sequences, divided by the longer sequence.
The published value is the macro-average over six cases. Retry attempts do not
masquerade as extra logical calls.

### Invalid-output rate

`invalid accepted outputs / all accepted outputs`.

The malformed-output scenario injects one schema-invalid response per mode.
Both modes detect and reject it at the typed tool boundary. The headline count
is therefore zero accepted invalid outputs, while the report retains separate
detected and rejected counts.

## Latency is diagnostic, not a performance claim

The baseline records nearest-rank local `perf_counter_ns` measurements:

| Metric | Fragile | Resilient |
| --- | ---: | ---: |
| P50 case latency | 32.0498 ms | 39.8309 ms |
| P95 case latency | 85.4523 ms | 63.8965 ms |

These values help detect gross regressions in one environment. They are not a
cross-machine throughput benchmark and are not enforced as headline quality
claims.

## Reproduce and verify

```bash
uv run arl eval scenarios/incident-response --output artifacts/current-report.json
uv run arl gate artifacts/current-report.json --baseline benchmarks/baseline-report.json
```

The gate recomputes summaries from case and trace evidence before applying
thresholds. It fails closed for altered suite identity, duplicate evidence IDs,
non-comparable baselines, malformed or dirty Git provenance, mismatched
summaries, fabricated recovery, changed tool outputs, or malformed report
structure. Baseline and current revisions can differ, but both must be full
lowercase 40-character revisions produced from clean worktrees.

Baseline provenance:

- report/schema version: `6`
- grader: `exact-v6`
- policy: `scripted`
- suite hash: `46135ff3279835cf7159261565203651f19aad29959591a9fcea4aba9381697f`
- Git revision: `f8b27dd205844867c7ad06158b8d370eb6f4e84c`
- Git dirty: `false`
- credential cost: `$0`
- baseline file SHA-256: `8bab5a85c33dd1dcbf357ac21eef81b9a73fdd6f0b7f98847bc52be748bf4d04`
- baseline file size: `171,915` bytes
- generated at (UTC): `2026-09-05T10:59:05.800603Z`

The hash and size are calculated from the canonical LF-normalized Git blob, so
local platform line-ending conversion cannot change the published evidence.
The `v0.1.0` baseline is intentionally retained in `v0.1.1` to prove that the
security dependency refresh did not change the frozen scenarios' reliability
results. Its clean Git revision points at the exact evaluated implementation.
