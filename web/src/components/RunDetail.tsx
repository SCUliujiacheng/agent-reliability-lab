import type { LoadState, PendingApproval, RunSummary, TraceEvent } from "../types";
import { StatusMark } from "./StatusMark";
import { TraceWaterfall } from "./TraceWaterfall";

export type MutationState = "idle" | "pending" | "success" | "error";

interface RunDetailProps {
  run: RunSummary;
  events: TraceEvent[];
  state: LoadState;
  mutationState: MutationState;
  onBack: () => void;
  onRunAgain: () => void;
  onApprove: (approval: PendingApproval, allow: boolean) => void;
}

function formatDuration(value: number): string {
  if (value >= 60_000) return `${(value / 60_000).toFixed(1)}m`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${value}ms`;
}

function exportTrace(run: RunSummary, events: TraceEvent[]) {
  const blob = new Blob([JSON.stringify({ run, events }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `trace-${run.id}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function RunDetail({
  run,
  events,
  state,
  mutationState,
  onBack,
  onRunAgain,
  onApprove,
}: RunDetailProps) {
  const outcome = run.result?.outcome ?? (run.status === "waiting_approval" ? "Pending review" : "Not available");
  const pending = mutationState === "pending" || state === "loading";
  const approval = run.pending_approval;

  return (
    <main className="detail-page" id="runs">
      <div className="detail-toolbar">
        <button type="button" className="back-button" onClick={onBack}>
          <span aria-hidden="true">←</span> Back to Runs
        </button>
        <div className="detail-actions">
          <button type="button" className="secondary-button" onClick={() => exportTrace(run, events)}>Export trace</button>
          <button type="button" className="primary-button" onClick={onRunAgain} disabled={pending}>Run again</button>
        </div>
      </div>

      <header className="detail-header">
        <div>
          <p className="eyebrow">Run detail</p>
          <h1>{run.scenario_id}</h1>
          <p className="run-reference">{run.id}</p>
        </div>
        <StatusMark status={run.status} />
      </header>

      <dl className="detail-facts">
        <div><dt>Mode</dt><dd>{run.mode}</dd></div>
        <div><dt>Attempts</dt><dd>{run.attempt_count}</dd></div>
        <div><dt>Outcome</dt><dd>{outcome}</dd></div>
        <div><dt>Duration</dt><dd>{formatDuration(run.duration_ms)}</dd></div>
      </dl>

      {run.status === "waiting_approval" && run.approval_required && approval ? (
        <section className="approval-panel" aria-labelledby="approval-title">
          <div>
            <p className="eyebrow">Human checkpoint</p>
            <h2 id="approval-title">Action awaiting approval</h2>
            <p>Review this exact side effect before recording a decision.</p>
            <dl className="approval-details">
              <div><dt>Tool</dt><dd><code>{approval.tool_name}</code></dd></div>
              <div><dt>Action</dt><dd>Step {approval.action_step}</dd></div>
            </dl>
            <div className="approval-arguments">
              <span>Arguments</span>
              <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
            </div>
            <p className="approval-fingerprint">
              Fingerprint <code>{approval.action_fingerprint}</code>
            </p>
          </div>
          <div className="approval-actions">
            <button type="button" className="danger-button" disabled={pending} onClick={() => onApprove(approval, false)}>Deny action</button>
            <button type="button" className="primary-button" disabled={pending} onClick={() => onApprove(approval, true)}>Allow action</button>
          </div>
        </section>
      ) : null}

      <section className="detail-trace" aria-labelledby="trace-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Execution trace</p>
            <h2 id="trace-title">Event waterfall</h2>
          </div>
          <span>{events.length} events</span>
        </div>
        {state === "error" ? (
          <div className="state-panel" role="alert"><strong>Trace unavailable</strong></div>
        ) : state === "loading" && events.length === 0 ? (
          <div className="state-panel" aria-live="polite">Loading trace…</div>
        ) : (
          <TraceWaterfall events={events} />
        )}
      </section>
    </main>
  );
}
