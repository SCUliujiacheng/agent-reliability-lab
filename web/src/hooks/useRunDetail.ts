import { useCallback, useEffect, useRef, useState } from "react";

import { getRun, getTrace } from "../api";
import type { LoadState, RunSummary, TraceEvent } from "../types";

export function useRunDetail(runId: string | null) {
  const [run, setRun] = useState<RunSummary | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [state, setState] = useState<LoadState>("ready");
  const [generation, setGeneration] = useState(0);
  const activeGeneration = useRef(0);
  const activeRunId = useRef(runId);

  useEffect(() => {
    activeRunId.current = runId;
  }, [runId]);

  const refresh = useCallback((expectedRunId?: string) => {
    if (expectedRunId !== undefined && activeRunId.current !== expectedRunId) return;
    setState("loading");
    setGeneration((value) => value + 1);
  }, []);
  const replaceRun = useCallback((value: RunSummary, expectedRunId: string) => {
    if (activeRunId.current === expectedRunId && value.id === expectedRunId) setRun(value);
  }, []);
  const reloadTrace = useCallback(async (expectedRunId: string) => {
    if (activeRunId.current !== expectedRunId) return;
    const page = await getTrace(expectedRunId);
    if (activeRunId.current !== expectedRunId) return;
    setEvents(page.events);
  }, []);

  useEffect(() => {
    if (runId === null) {
      return;
    }
    const controller = new AbortController();
    const requestGeneration = activeGeneration.current + 1;
    activeGeneration.current = requestGeneration;
    void Promise.all([getRun(runId, controller.signal), getTrace(runId, controller.signal)])
      .then(([nextRun, trace]) => {
        if (controller.signal.aborted || activeGeneration.current !== requestGeneration) return;
        setRun(nextRun);
        setEvents(trace.events);
        setState("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || activeGeneration.current !== requestGeneration) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState("error");
      });

    return () => controller.abort();
  }, [generation, runId]);

  const runMatchesSelection = runId !== null && run?.id === runId;
  return {
    run: runMatchesSelection ? run : null,
    events: runMatchesSelection ? events : [],
    state: runId === null ? "ready" : runMatchesSelection ? state : "loading",
    refresh,
    replaceRun,
    reloadTrace,
  };
}
