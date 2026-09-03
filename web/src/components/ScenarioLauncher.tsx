import { useState } from "react";

import type { LoadState, RunMode, ScenarioSummary } from "../types";

interface ScenarioLauncherProps {
  scenarios: ScenarioSummary[];
  state: LoadState;
  launching: boolean;
  onStart: (scenarioId: string, mode: RunMode) => void;
  onRetry: () => void;
}

export function ScenarioLauncher({
  scenarios,
  state,
  launching,
  onStart,
  onRetry,
}: ScenarioLauncherProps) {
  const [selectedScenario, setSelectedScenario] = useState("");
  const [mode, setMode] = useState<RunMode>("resilient");
  const effectiveScenario = scenarios.some((scenario) => scenario.id === selectedScenario)
    ? selectedScenario
    : (scenarios[0]?.id ?? "");

  if (state === "loading") {
    return <div className="state-panel" aria-live="polite">Loading scenario catalog…</div>;
  }
  if (state === "error") {
    return (
      <div className="state-panel" role="alert">
        <strong>Scenarios unavailable</strong>
        <button type="button" className="text-button" onClick={onRetry}>Try again</button>
      </div>
    );
  }
  if (scenarios.length === 0) {
    return <div className="state-panel"><strong>No scenarios configured</strong></div>;
  }

  const current = scenarios.find((scenario) => scenario.id === effectiveScenario) ?? scenarios[0];

  return (
    <form
      className="launcher-form"
      onSubmit={(event) => {
        event.preventDefault();
        onStart(effectiveScenario, mode);
      }}
    >
      <label htmlFor="scenario-select">Scenario</label>
      <select
        id="scenario-select"
        value={effectiveScenario}
        onChange={(event) => setSelectedScenario(event.target.value)}
        disabled={launching}
      >
        {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.id}</option>)}
      </select>

      <label htmlFor="mode-select">Mode</label>
      <select
        id="mode-select"
        value={mode}
        onChange={(event) => setMode(event.target.value as RunMode)}
        disabled={launching}
      >
        <option value="resilient">Resilient</option>
        <option value="fragile">Fragile</option>
      </select>

      <dl className="launcher-facts">
        <div><dt>Expected</dt><dd>{current.expected_outcome}</dd></div>
        <div><dt>Faults</dt><dd>{current.faults.length}</dd></div>
        <div><dt>Approval</dt><dd>{current.approval_required ? "Required" : "Not required"}</dd></div>
      </dl>

      <button className="primary-button" type="submit" disabled={launching || effectiveScenario.length === 0}>
        {launching ? "Starting…" : "Start run"}
      </button>
    </form>
  );
}
