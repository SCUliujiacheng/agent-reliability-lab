const SCENARIO_LABELS: Readonly<Record<string, string>> = {
  "approval-reconstruction": "Resume after approval",
  "malformed-output-rejected": "Reject malformed output",
  "normal-success": "Normal execution",
  "permanent-invalid-input": "Reject invalid input",
  "rate-limit-recovery": "Retry after rate limit",
  "timeout-recovery": "Retry after timeout",
};

export function scenarioLabel(id: string): string {
  return SCENARIO_LABELS[id] ?? id;
}
