# Contributing

Thanks for helping improve Agent Reliability Lab. Changes should preserve the
repository's central contract: reliability claims must be reproducible from
ordered evidence and must fail closed when that evidence is incomplete or
inconsistent.

## Development setup

```bash
uv sync --dev --locked
npm ci --prefix web
```

Run the full local gate before opening a pull request:

```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src

npm --prefix web test -- --run
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run build

uv run arl eval scenarios/incident-response --output artifacts/current-report.json
uv run arl gate artifacts/current-report.json --baseline benchmarks/baseline-report.json
```

## Change guidelines

- Add a failing test before changing runtime, tool, storage, API, or gate
  behavior.
- Keep public inputs bounded and strict; reject unknown fields.
- Never add arbitrary shell execution to the tool gateway.
- Record state transitions before taking the next action.
- Preserve recursive secret redaction in every stored or exported trace path.
- Treat retries as attempts of one logical action, not extra logical tool calls.
- Document any benchmark denominator or semantic change.

## Scenario and baseline changes

A scenario change must include a deliberate version update, exact expected
outcome, expected logical tool sequence, and tests for new behavior. Regenerate
the report with the official evaluator—do not hand-edit benchmark metrics:

```bash
uv run arl eval scenarios/incident-response \
  --baseline-output benchmarks/baseline-report.json
```

Explain why the suite hash changed and which headline denominators are affected.
Provider-backed or nondeterministic evaluations should use a separate suite and
must not silently replace the deterministic baseline.

## Pull requests

Keep pull requests focused. Include the behavior change, failure mode, test
evidence, migration or compatibility impact, and any limitation that remains.
Screenshots are expected for visible dashboard changes.
