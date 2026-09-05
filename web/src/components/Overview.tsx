import type { EvaluationReport, LoadState, RunMode, RunSummary, ScenarioSummary } from "../types";
import { EvaluationComparison } from "./EvaluationComparison";
import { MetricCard } from "./MetricCard";
import { RunList } from "./RunList";
import { ScenarioLauncher } from "./ScenarioLauncher";

interface OverviewProps {
  state: LoadState;
  runs: RunSummary[];
  evaluation: EvaluationReport | null;
  scenarios: ScenarioSummary[];
  launching: boolean;
  evaluating: boolean;
  onSelectRun: (runId: string) => void;
  onStart: (scenarioId: string, mode: RunMode) => void;
  onEvaluate: () => void;
  onRetry: () => void;
}

function formatRate(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function Overview({
  state,
  runs,
  evaluation,
  scenarios,
  launching,
  evaluating,
  onSelectRun,
  onStart,
  onEvaluate,
  onRetry,
}: OverviewProps) {
  const resilient = evaluation?.modes.resilient.metrics;
  const fragile = evaluation?.modes.fragile.metrics;
  const header = (
    <header className="overview-header">
      <div>
        <p className="eyebrow">Agent Reliability Lab</p>
        <h1>What happens after an agent fails?</h1>
        <p>Run the frozen cases, compare fragile with resilient, then open the trace.</p>
      </div>
      <div className="overview-header__actions">
        <span className="environment-label"><span aria-hidden="true" /> Local API</span>
        <button
          type="button"
          className="primary-button"
          disabled={evaluating || state !== "ready"}
          onClick={onEvaluate}
        >
          {evaluating ? "Running evaluation…" : "Run evaluation"}
        </button>
        <a className="secondary-button header-action" href="#scenarios">Run scenario</a>
      </div>
    </header>
  );

  if (state === "error") {
    return (
      <main className="overview-page" id="overview">
        {header}
        <section className="overview-error" role="alert">
          <div>
            <p className="eyebrow">API unavailable</p>
            <h2>Dashboard data could not be loaded</h2>
            <p>Check that the local API is running, then try again.</p>
          </div>
          <button type="button" className="primary-button" onClick={onRetry}>Retry dashboard</button>
        </section>
      </main>
    );
  }

  return (
    <main className="overview-page" id="overview">
      {header}

      <section className="metrics-grid" aria-label="Reliability metrics">
        <MetricCard
          label="Resilient correctness"
          value={formatRate(resilient?.task_correctness_rate)}
          detail={evaluation ? "Latest evaluation" : "No evaluation report"}
          tone="positive"
        />
        <MetricCard
          label="Recovery"
          value={formatRate(resilient?.recovery_rate)}
          detail="Transient faults recovered"
          tone="positive"
        />
        <MetricCard
          label="Fragile correctness"
          value={formatRate(fragile?.task_correctness_rate)}
          detail="Latest evaluation"
          tone="fragile"
        />
        <MetricCard
          label="Accepted invalid outputs"
          value={formatRate(resilient?.invalid_output_rate)}
          detail="Resilient execution"
          tone={resilient?.invalid_output_rate === 0 ? "positive" : "fragile"}
        />
      </section>

      {evaluation ? (
        <EvaluationComparison report={evaluation} />
      ) : (
        <section className="comparison comparison--empty" id="evaluations">
          <p className="eyebrow">Latest evaluation</p>
          <h2>No evaluations yet</h2>
          <p>Run the frozen catalog suite to populate the benchmark comparison.</p>
        </section>
      )}

      <div className="dashboard-grid">
        <section className="runs-panel" id="runs" aria-labelledby="recent-runs-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Execution history</p>
              <h2 id="recent-runs-title">Recent runs</h2>
            </div>
            <span>{runs.length} visible</span>
          </div>
          <RunList runs={runs} state={state} onSelect={onSelectRun} onRetry={onRetry} />
        </section>

        <aside className="launcher-panel" id="scenarios" aria-labelledby="launcher-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Start with one fixed case</p>
              <h2 id="launcher-title">Pick a scenario</h2>
            </div>
          </div>
          <ScenarioLauncher
            scenarios={scenarios}
            state={state}
            launching={launching}
            onStart={onStart}
            onRetry={onRetry}
          />
        </aside>

        <section className="trace-preview" aria-labelledby="trace-preview-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Then read the trace</p>
              <h2 id="trace-preview-title">Open a run and follow what happened</h2>
            </div>
          </div>
          <div className="preview-steps" aria-hidden="true">
            <span /><span /><span /><span />
          </div>
          <p>The overview waits until you pick a run before loading its events.</p>
        </section>
      </div>
    </main>
  );
}
