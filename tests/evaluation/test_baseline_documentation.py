"""Keep human-readable benchmark claims bound to the canonical baseline."""

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
BASELINE = ROOT / "benchmarks" / "baseline-report.json"
DOCUMENTATION = ROOT / "docs" / "benchmark-results.md"


def test_benchmark_documentation_matches_canonical_baseline() -> None:
    """Published provenance and latency values must match the versioned artifact."""

    canonical_bytes = BASELINE.read_bytes().replace(b"\r\n", b"\n")
    report: dict[str, Any] = json.loads(canonical_bytes)
    documentation = DOCUMENTATION.read_text(encoding="utf-8")
    provenance = report["provenance"]

    expected_values = {
        provenance["suite_hash"],
        provenance["git_revision"],
        report["generated_at"],
        hashlib.sha256(canonical_bytes).hexdigest(),
        f"{len(canonical_bytes):,}",
        f"{report['modes']['fragile']['metrics']['p50_latency_ms']:.4f} ms",
        f"{report['modes']['resilient']['metrics']['p50_latency_ms']:.4f} ms",
        f"{report['modes']['fragile']['metrics']['p95_latency_ms']:.4f} ms",
        f"{report['modes']['resilient']['metrics']['p95_latency_ms']:.4f} ms",
    }

    missing = sorted(value for value in expected_values if value not in documentation)
    assert not missing, (
        f"benchmark documentation is missing canonical values: {missing}"
    )
