import type { EvaluationReport, LoadState, RunMode, RunSummary, ScenarioSummary } from "../types";
import { EvaluationComparison } from "./EvaluationComparison";
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
  const evaluationAction = (
    <button
      type="button"
      className="secondary-button"
      disabled={evaluating || state !== "ready"}
      onClick={onEvaluate}
    >
      {evaluating ? "Evaluating…" : "Run evaluation"}
    </button>
  );

  return (
    <main className="overview-page" id="overview">
      <header className="overview-header">
        <h1>Runs</h1>
        <p>Choose a fault scenario and inspect its calls, retries, and result.</p>
      </header>

      {state === "error" ? (
        <section className="overview-error" role="alert">
          <div>
            <h2>Run data could not be loaded</h2>
            <p>Check that the local API is running, then try again.</p>
          </div>
          <button type="button" className="primary-button" onClick={onRetry}>Retry</button>
        </section>
      ) : (
        <div className="workspace-grid">
          <section className="launcher-panel" id="scenarios" aria-labelledby="launcher-title">
            <div className="section-heading">
              <h2 id="launcher-title">Run a scenario</h2>
            </div>
            <ScenarioLauncher
              scenarios={scenarios}
              state={state}
              launching={launching}
              onStart={onStart}
              onRetry={onRetry}
            />
          </section>

          <section className="runs-panel" id="runs" aria-labelledby="recent-runs-title">
            <div className="section-heading">
              <h2 id="recent-runs-title">Recent runs</h2>
              <span>{runs.length} runs</span>
            </div>
            <RunList runs={runs} state={state} onSelect={onSelectRun} onRetry={onRetry} />
          </section>
        </div>
      )}

      {evaluation && state !== "error" ? (
        <EvaluationComparison report={evaluation} action={evaluationAction} />
      ) : (
        <section className="comparison comparison--empty" id="evaluations" aria-labelledby="comparison-title">
          <div className="section-heading">
            <h2 id="comparison-title">Evaluation comparison</h2>
            {evaluationAction}
          </div>
          <p>{state === "error" ? "Connect to the API to run an evaluation." : "No evaluations yet"}</p>
          {state !== "error" ? <p className="section-note">Run the same fixed scenarios in both modes to compare their results.</p> : null}
        </section>
      )}
    </main>
  );
}
