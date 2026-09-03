import { useCallback, useRef, useState } from "react";

import { ApiClientError, approveRun, createRun } from "./api";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { MutationNotice } from "./components/MutationNotice";
import { Navigation } from "./components/Navigation";
import { Overview } from "./components/Overview";
import { RunDetail, type MutationState } from "./components/RunDetail";
import { useOverview } from "./hooks/useOverview";
import { useRunDetail } from "./hooks/useRunDetail";
import type { RunMode } from "./types";

function Dashboard() {
  const overview = useOverview();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const detail = useRunDetail(selectedRunId);
  const [mutationState, setMutationState] = useState<MutationState>("idle");
  const [notice, setNotice] = useState("");
  const approvalInFlight = useRef(false);
  const dismissNotice = useCallback(() => setNotice(""), []);

  const startRun = async (scenarioId: string, mode: RunMode) => {
    if (mutationState === "pending") return;
    setMutationState("pending");
    setNotice("");
    try {
      const run = await createRun(scenarioId, mode);
      setSelectedRunId(run.id);
      setMutationState("success");
    } catch {
      setMutationState("error");
      setNotice("The run could not be started. Retry when the API is available.");
    }
  };

  const approve = async (allow: boolean) => {
    if (selectedRunId === null || approvalInFlight.current) return;
    approvalInFlight.current = true;
    setMutationState("pending");
    setNotice("");
    try {
      const run = await approveRun(selectedRunId, {
        actor: "dashboard-reviewer",
        allow,
        reason: allow ? "Approved in reliability dashboard" : "Denied in reliability dashboard",
      });
      detail.replaceRun(run);
      await detail.reloadTrace();
      setMutationState("success");
      setNotice(allow ? "Action allowed" : "Action denied");
    } catch (error) {
      if (error instanceof ApiClientError && error.status === 409) {
        detail.refresh();
        setNotice("Approval state refreshed");
      } else {
        setNotice("The approval decision could not be recorded.");
      }
      setMutationState("error");
    } finally {
      approvalInFlight.current = false;
    }
  };

  const selectedRunReady =
    selectedRunId !== null && detail.run !== null && detail.run.id === selectedRunId;

  return (
    <div className="app-shell">
      <Navigation />
      <div className="app-content">
        {notice ? <MutationNotice message={notice} onDismiss={dismissNotice} /> : null}
        {selectedRunReady && detail.run ? (
          <RunDetail
            run={detail.run}
            events={detail.events}
            state={detail.state}
            mutationState={mutationState}
            onBack={() => {
              setSelectedRunId(null);
              setMutationState("idle");
              setNotice("");
              overview.refresh();
            }}
            onRunAgain={() => void startRun(detail.run!.scenario_id, detail.run!.mode)}
            onApprove={(allow) => void approve(allow)}
          />
        ) : selectedRunId !== null && detail.state === "error" ? (
          <main className="detail-load-state" role="alert">
            <h1>Run detail unavailable</h1>
            <p>The selected run or its trace could not be loaded.</p>
            <button type="button" className="primary-button" onClick={() => detail.refresh()}>Try again</button>
            <button type="button" className="text-button" onClick={() => setSelectedRunId(null)}>Back to Runs</button>
          </main>
        ) : (
          <>
            {selectedRunId !== null ? <p className="detail-loading" aria-live="polite">Loading run detail…</p> : null}
            <Overview
              state={overview.state}
              runs={overview.data.runs}
              evaluation={overview.data.evaluation}
              scenarios={overview.data.scenarios}
              launching={mutationState === "pending"}
              onSelectRun={(runId) => {
                setSelectedRunId(runId);
                setNotice("");
              }}
              onStart={(scenarioId, mode) => void startRun(scenarioId, mode)}
              onRetry={overview.refresh}
            />
          </>
        )}
      </div>
    </div>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <Dashboard />
    </ErrorBoundary>
  );
}
