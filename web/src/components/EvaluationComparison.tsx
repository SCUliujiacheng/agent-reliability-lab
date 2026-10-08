import type { ReactNode } from "react";
import type { EvaluationReport, ModeMetrics } from "../types";

interface EvaluationComparisonProps {
  report: EvaluationReport;
  action?: ReactNode;
}

type MetricKey = keyof Pick<
  ModeMetrics,
  | "task_correctness_rate"
  | "recovery_rate"
  | "tool_sequence_accuracy"
  | "invalid_output_rate"
  | "unnecessary_call_count"
  | "p95_latency_ms"
>;

interface MetricDefinition {
  key: MetricKey;
  label: string;
  higherIsBetter: boolean;
  format: "rate" | "count" | "duration";
}

const METRICS: MetricDefinition[] = [
  { key: "task_correctness_rate", label: "Task correctness", higherIsBetter: true, format: "rate" },
  { key: "recovery_rate", label: "Recovery rate", higherIsBetter: true, format: "rate" },
  { key: "tool_sequence_accuracy", label: "Tool sequence accuracy", higherIsBetter: true, format: "rate" },
  { key: "invalid_output_rate", label: "Accepted invalid outputs", higherIsBetter: false, format: "rate" },
  { key: "unnecessary_call_count", label: "Unnecessary calls", higherIsBetter: false, format: "count" },
  { key: "p95_latency_ms", label: "P95 latency", higherIsBetter: false, format: "duration" },
];

type ChangeState = "unavailable" | "unchanged" | "improved" | "regressed";

const CHANGE_LABELS: Record<ChangeState, string> = {
  unavailable: "Not available",
  unchanged: "Unchanged",
  improved: "Improved",
  regressed: "Regressed",
};

function formatMetric(value: number | null, format: MetricDefinition["format"]): string {
  if (value === null) return "Not available";
  if (format === "rate") return `${(value * 100).toFixed(1)}%`;
  if (format === "duration") return `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })} ms`;
  return value.toLocaleString("en-US");
}

function changeLabel(fragile: number | null, resilient: number | null, higherIsBetter: boolean): ChangeState {
  if (fragile === null || resilient === null) return "unavailable";
  const delta = resilient - fragile;
  if (Math.abs(delta) < Number.EPSILON) return "unchanged";
  return (delta > 0) === higherIsBetter ? "improved" : "regressed";
}

function deltaLabel(fragile: number | null, resilient: number | null, format: MetricDefinition["format"]): string {
  if (fragile === null || resilient === null) return "—";
  const delta = resilient - fragile;
  const sign = delta > 0 ? "+" : "";
  if (format === "rate") return `${sign}${(delta * 100).toFixed(1)} pp`;
  if (format === "duration") return `${sign}${delta.toFixed(1)} ms`;
  return `${sign}${delta.toLocaleString("en-US")}`;
}

export function EvaluationComparison({ report, action }: EvaluationComparisonProps) {
  const fragile = report.modes.fragile.metrics;
  const resilient = report.modes.resilient.metrics;

  return (
    <section className="comparison" id="evaluations" aria-labelledby="comparison-title">
      <div className="section-heading">
        <h2 id="comparison-title">Evaluation comparison</h2>
        <div className="section-actions">
          <time dateTime={report.generated_at}>{new Date(report.generated_at).toLocaleDateString("en-US")}</time>
          {action}
        </div>
      </div>
      <div className="comparison-table-wrap">
        <table className="comparison-table">
          <caption className="sr-only">Evaluation metrics by execution mode</caption>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col">Fragile</th>
              <th scope="col">Resilient</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {METRICS.map((metric) => {
              const fragileValue = fragile[metric.key];
              const resilientValue = resilient[metric.key];
              const state = changeLabel(fragileValue, resilientValue, metric.higherIsBetter);
              return (
                <tr key={metric.key}>
                  <th scope="row">{metric.label}</th>
                  <td>{formatMetric(fragileValue, metric.format)}</td>
                  <td>{formatMetric(resilientValue, metric.format)}</td>
                  <td>
                    <div className="metric-change">
                      <span>{deltaLabel(fragileValue, resilientValue, metric.format)}</span>
                      <small className={`change-state change-state--${state}`}>{CHANGE_LABELS[state]}</small>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="section-note">
        {fragile.case_count} fixed synthetic scenarios. These results compare execution strategies, not model capability.
      </p>
    </section>
  );
}
