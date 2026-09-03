import { useCallback, useEffect, useRef, useState } from "react";

import { getEvaluations, getRuns, getScenarios } from "../api";
import type { EvaluationReport, LoadState, RunSummary, ScenarioSummary } from "../types";

interface OverviewData {
  runs: RunSummary[];
  evaluation: EvaluationReport | null;
  scenarios: ScenarioSummary[];
}

const EMPTY_OVERVIEW: OverviewData = { runs: [], evaluation: null, scenarios: [] };

export function useOverview() {
  const [state, setState] = useState<LoadState>("loading");
  const [data, setData] = useState<OverviewData>(EMPTY_OVERVIEW);
  const [generation, setGeneration] = useState(0);
  const activeGeneration = useRef(0);

  const refresh = useCallback(() => {
    setState("loading");
    setGeneration((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const requestGeneration = activeGeneration.current + 1;
    activeGeneration.current = requestGeneration;
    void Promise.all([
      getRuns(8, controller.signal),
      getEvaluations(1, controller.signal),
      getScenarios(controller.signal),
    ])
      .then(([runs, evaluations, scenarios]) => {
        if (controller.signal.aborted || activeGeneration.current !== requestGeneration) return;
        setData({ runs, evaluation: evaluations[0] ?? null, scenarios });
        setState("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || activeGeneration.current !== requestGeneration) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState("error");
      });

    return () => {
      controller.abort();
    };
  }, [generation]);

  return { data, state, refresh };
}
