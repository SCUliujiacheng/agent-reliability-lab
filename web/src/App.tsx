import { useCallback, useRef, useState } from "react";

import { ApiClientError, approveRun, createEvaluation, createRun } from "./api";
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
  const [evaluationPending, setEvaluationPending] = useState(false);
  const [notice, setNotice] = useState("");
  const selectedRunIdRef = useRef<string | null>(null);
  const selectionGeneration = useRef(0);
  const approvalsInFlight = useRef(new Set<string>());
  const evaluationInFlight = useRef(false);
  const dismissNotice = useCallback(() => setNotice(""), []);
  const selectRun = useCallback((runId: string | null) => {
    selectedRunIdRef.current = runId;
    selectionGeneration.current += 1;
    setSelectedRunId(runId);
  }, []);

  const startRun = async (scenarioId: string, mode: RunMode) => {
    if (mutationState === "pending") return;
    setMutationState("pending");
    setNotice("");
    try {
      const run = await createRun(scenarioId, mode);
      selectRun(run.id);
      setMutationState("success");
    } catch {
      setMutationState("error");
      setNotice("The run could not be started. Retry when the API is available.");
    }
  };

  const runEvaluation = async () => {
    if (evaluationInFlight.current) return;
    evaluationInFlight.current = true;
    setEvaluationPending(true);
    setNotice("");
    try {
      const report = await createEvaluation();
      overview.replaceEvaluation(report);
      setNotice("Evaluation complete");
    } catch {
      setNotice("The evaluation could not be completed. Retry when the API is available.");
    } finally {
      evaluationInFlight.current = false;
      setEvaluationPending(false);
    }
  };

  const approve = async (allow: boolean) => {
    if (selectedRunId === null) return;
    const approvalRunId = selectedRunId;
    const approvalGeneration = selectionGeneration.current;
    const approvalKey = `${approvalRunId}:${approvalGeneration}`;
    if (approvalsInFlight.current.has(approvalKey)) return;
    approvalsInFlight.current.add(approvalKey);
    const isCurrentSelection = () =>
      selectedRunIdRef.current === approvalRunId
      && selectionGeneration.current === approvalGeneration;
    setMutationState("pending");
    setNotice("");
    try {
      const run = await approveRun(approvalRunId, {
        actor: "dashboard-reviewer",
        allow,
        reason: allow ? "Approved in reliability dashboard" : "Denied in reliability dashboard",
      });
      if (!isCurrentSelection()) return;
      detail.replaceRun(run, approvalRunId);
      await detail.reloadTrace(approvalRunId);
      if (!isCurrentSelection()) return;
      setMutationState("success");
      setNotice(allow ? "Action allowed" : "Action denied");
    } catch (error) {
      if (!isCurrentSelection()) return;
      if (error instanceof ApiClientError && error.status === 409) {
        detail.refresh(approvalRunId);
        setNotice("Approval state refreshed");
      } else {
        setNotice("The approval decision could not be recorded.");
      }
      setMutationState("error");
    } finally {
      approvalsInFlight.current.delete(approvalKey);
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
              selectRun(null);
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
            <button type="button" className="text-button" onClick={() => selectRun(null)}>Back to Runs</button>
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
              evaluating={evaluationPending}
              onSelectRun={(runId) => {
                selectRun(runId);
                setNotice("");
              }}
              onStart={(scenarioId, mode) => void startRun(scenarioId, mode)}
              onEvaluate={() => void runEvaluation()}
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
