import type {
  ApprovalInput,
  EvaluationReport,
  RunMode,
  RunSummary,
  ScenarioSummary,
  TracePage,
} from "./types";

interface StableErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  try {
    const headers = new Headers(init.headers);
    if (init.body !== undefined) headers.set("content-type", "application/json");
    const response = await fetch(path, { ...init, headers });
    if (!response.ok) {
      const envelope = await stableEnvelope(response);
      if (envelope !== null) {
        throw new ApiClientError(
          response.status,
          envelope.error.code,
          envelope.error.message,
          envelope.error.details,
        );
      }
      throw new ApiClientError(
        response.status,
        "http_error",
        "The server could not complete the request.",
      );
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiClientError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiClientError(0, "network_error", "The API is not reachable.");
  }
}

async function stableEnvelope(response: Response): Promise<StableErrorEnvelope | null> {
  if (!response.headers.get("content-type")?.includes("application/json")) return null;
  try {
    const value = (await response.json()) as unknown;
    if (!isRecord(value) || !isRecord(value.error)) return null;
    const { code, message, details } = value.error;
    if (typeof code !== "string" || typeof message !== "string" || !isRecord(details)) {
      return null;
    }
    return { error: { code, message, details } };
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function getRuns(limit = 8, signal?: AbortSignal): Promise<RunSummary[]> {
  const response = await requestJson<{ items: RunSummary[] }>(
    `/v1/runs?limit=${limit}`,
    { signal },
  );
  return response.items;
}

export async function getEvaluations(
  limit = 1,
  signal?: AbortSignal,
): Promise<EvaluationReport[]> {
  const response = await requestJson<{ items: EvaluationReport[] }>(
    `/v1/evaluations?limit=${limit}`,
    { signal },
  );
  return response.items;
}

export function createEvaluation(
  suite = "incident-response",
): Promise<EvaluationReport> {
  return requestJson("/v1/evaluations", {
    method: "POST",
    body: JSON.stringify({ suite }),
  });
}

export async function getScenarios(signal?: AbortSignal): Promise<ScenarioSummary[]> {
  const response = await requestJson<{ items: ScenarioSummary[] }>("/v1/scenarios", {
    signal,
  });
  return response.items;
}

export function getRun(runId: string, signal?: AbortSignal): Promise<RunSummary> {
  return requestJson(`/v1/runs/${encodeURIComponent(runId)}`, { signal });
}

export function getTrace(runId: string, signal?: AbortSignal): Promise<TracePage> {
  return requestJson(
    `/v1/runs/${encodeURIComponent(runId)}/trace?limit=100&after_sequence=0`,
    { signal },
  );
}

export function createRun(
  scenarioId: string,
  mode: RunMode,
  signal?: AbortSignal,
): Promise<RunSummary> {
  return requestJson("/v1/runs", {
    method: "POST",
    signal,
    body: JSON.stringify({ scenario_id: scenarioId, mode }),
  });
}

export function approveRun(runId: string, input: ApprovalInput): Promise<RunSummary> {
  return requestJson(`/v1/runs/${encodeURIComponent(runId)}/approvals`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function resumeRun(runId: string): Promise<RunSummary> {
  return requestJson(`/v1/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
  });
}
