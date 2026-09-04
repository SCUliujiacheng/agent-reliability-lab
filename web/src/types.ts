export type RunMode = "fragile" | "resilient";
export type RunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed";

export interface RunResult {
  outcome?: string;
  summary?: string;
  code?: string;
  reason?: string;
  explanation?: string;
  evidence_refs: string[];
}

export interface PendingApproval {
  action_step: number;
  action_fingerprint: string;
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface RunSummary {
  id: string;
  trace_id: string;
  scenario_id: string;
  mode: RunMode;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  duration_ms: number;
  approval_required: boolean;
  attempt_count: number;
  pending_approval?: PendingApproval;
  result?: RunResult;
}

export interface ScenarioFault {
  tool_name: string;
  attempt: number;
  type: "timeout" | "rate_limit" | "tool_error" | "malformed_output";
}

export interface ScenarioSummary {
  id: string;
  expected_outcome: string;
  expected_tool_sequence: string[];
  faults: ScenarioFault[];
  approval_required: boolean;
}

export interface TracePayload {
  tool_name?: string;
  attempt?: number;
  after_attempt?: number;
  action_step?: number;
  step?: number;
  code?: string;
  kind?: string;
  transient?: boolean;
  cached?: boolean;
  retry_delay_seconds?: number;
  source?: string;
  actor?: string;
  allow?: boolean;
  outcome?: string;
  summary?: string;
  reason?: string;
  from_status?: string;
}

export interface TraceEvent {
  id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  sequence: number;
  event_type: string;
  payload: TracePayload;
  duration_ms?: number | null;
  status: string;
  created_at: string;
}

export interface ModeMetrics {
  case_correct_count: number;
  case_count: number;
  task_correctness_rate: number;
  terminal_success_count: number;
  tool_sequence_accuracy: number;
  tool_sequence_case_count: number;
  recovered_transient_fault_count: number;
  transient_fault_count: number;
  recovery_rate: number | null;
  invalid_output_accepted_count: number;
  accepted_output_count: number;
  invalid_output_rate: number;
  invalid_output_detected_count: number;
  invalid_output_rejected_count: number;
  malformed_fault_injected_count: number;
  unnecessary_call_count: number;
  retry_attempt_count: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  estimated_cost_usd: string;
}

export interface EvaluationCase {
  scenario_id: string;
  mode: RunMode;
  run_id: string;
  final_status: "succeeded" | "failed" | "waiting_approval";
  expected_outcome: string;
  observed_outcome: string;
  correct: boolean;
  attempt_count: number;
  retry_attempt_count: number;
}

export interface EvaluationModeResult {
  mode: RunMode;
  metrics: ModeMetrics;
  cases: EvaluationCase[];
}

export interface EvaluationComparisonData {
  task_correctness_rate_delta: number;
  recovery_rate_delta: number | null;
  tool_sequence_accuracy_delta: number;
  invalid_output_rate_delta: number;
  unnecessary_call_count_delta: number;
  retry_attempt_count_delta: number;
  p50_latency_ms_delta: number;
  p95_latency_ms_delta: number;
  fragile_worse_recovery_scenarios: string[];
}

export interface EvaluationReport {
  evaluation_id: string;
  generated_at: string;
  provenance: {
    report_version: string;
    grader_version: string;
    suite_hash: string;
    git_revision: string;
    git_dirty: boolean;
    package_version: string;
  };
  modes: {
    fragile: EvaluationModeResult;
    resilient: EvaluationModeResult;
  };
  comparison: EvaluationComparisonData | null;
}

export interface TracePage {
  events: TraceEvent[];
  next_after_sequence: number;
  has_more: boolean;
}

export type LoadState = "loading" | "ready" | "error";

export interface ApprovalInput {
  actor: string;
  allow: boolean;
  action_step: number;
  action_fingerprint: string;
  reason?: string;
}
