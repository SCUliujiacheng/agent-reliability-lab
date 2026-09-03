import type { LoadState, RunSummary } from "../types";
import { StatusMark } from "./StatusMark";

interface RunListProps {
  runs: RunSummary[];
  state: LoadState;
  onSelect?: (runId: string) => void;
  onRetry?: () => void;
}

function formatStarted(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function RunList({ runs, state, onSelect, onRetry }: RunListProps) {
  if (state === "loading") {
    return (
      <div className="state-panel state-panel--loading" aria-label="Loading recent runs" aria-live="polite">
        <span className="loading-line" />
        <span className="loading-line loading-line--short" />
        <span className="loading-line" />
      </div>
    );
  }

  if (state === "error") {
    return (
      <div className="state-panel" role="alert">
        <strong>Runs unavailable</strong>
        <p>The API did not return the recent run list.</p>
        {onRetry ? <button type="button" className="text-button" onClick={onRetry}>Try again</button> : null}
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="state-panel">
        <strong>No runs yet</strong>
        <p>Start a scenario to create the first execution trace.</p>
      </div>
    );
  }

  return (
    <div className="run-table-wrap">
      <table className="run-table" aria-label="Recent runs">
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">Scenario</th>
            <th scope="col">Mode</th>
            <th scope="col">Status</th>
            <th scope="col">Attempts</th>
            <th scope="col">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td data-label="Run">
                <button
                  type="button"
                  className="run-id-button"
                  aria-label={`Open run ${run.scenario_id} ${run.id}`}
                  onClick={() => onSelect?.(run.id)}
                >
                  {run.id}
                </button>
              </td>
              <td data-label="Scenario">{run.scenario_id}</td>
              <td data-label="Mode"><span className="mode-label">{run.mode}</span></td>
              <td data-label="Status"><StatusMark status={run.status} /></td>
              <td data-label="Attempts">{run.attempt_count}</td>
              <td data-label="Started"><time dateTime={run.created_at}>{formatStarted(run.created_at)}</time></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
