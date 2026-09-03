import type { EvaluationReport, ModeMetrics } from "../types";

interface EvaluationComparisonProps {
  report: EvaluationReport;
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
  { key: "recovery_rate", label: "Recovery rate", higherIsBetter: true, format: "rate" },
  { key: "tool_sequence_accuracy", label: "Tool sequence accuracy", higherIsBetter: true, format: "rate" },
  { key: "invalid_output_rate", label: "Accepted invalid outputs", higherIsBetter: false, format: "rate" },
  { key: "unnecessary_call_count", label: "Unnecessary calls", higherIsBetter: false, format: "count" },
  { key: "p95_latency_ms", label: "P95 latency", higherIsBetter: false, format: "duration" },
];

function formatRate(value: number | null): string {
  return value === null ? "Not available" : `${(value * 100).toFixed(1)}%`;
}

function formatMetric(value: number | null, format: MetricDefinition["format"]): string {
  if (value === null) return "Not available";
  if (format === "rate") return formatRate(value);
  if (format === "duration") return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ms`;
  return value.toLocaleString();
}

function changeLabel(
  fragile: number | null,
  resilient: number | null,
  higherIsBetter: boolean,
): string {
  if (fragile === null || resilient === null) return "Not available";
  const delta = resilient - fragile;
  if (Math.abs(delta) < Number.EPSILON) return "Unchanged";
  return (delta > 0) === higherIsBetter ? "Improved" : "Regressed";
}

function deltaLabel(
  fragile: number | null,
  resilient: number | null,
  format: MetricDefinition["format"],
): string {
  if (fragile === null || resilient === null) return "Not available";
  const delta = resilient - fragile;
  const sign = delta > 0 ? "+" : "";
  if (format === "rate") return `${sign}${(delta * 100).toFixed(1)} pp`;
  if (format === "duration") return `${sign}${delta.toFixed(1)} ms`;
  return `${sign}${delta.toLocaleString()}`;
}

export function EvaluationComparison({ report }: EvaluationComparisonProps) {
  const fragile = report.modes.fragile.metrics;
  const resilient = report.modes.resilient.metrics;
  const correctnessDelta = resilient.task_correctness_rate - fragile.task_correctness_rate;
  const correctnessState = changeLabel(
    fragile.task_correctness_rate,
    resilient.task_correctness_rate,
    true,
  );

  return (
    <section className="comparison" id="evaluations" aria-labelledby="comparison-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Latest evaluation</p>
          <h2 id="comparison-title">Fragile vs resilient</h2>
        </div>
        <time dateTime={report.generated_at}>{new Date(report.generated_at).toLocaleDateString()}</time>
      </div>

      <div className="correctness-band">
        <div>
          <span>Fragile correctness</span>
          <strong>{formatRate(fragile.task_correctness_rate)}</strong>
        </div>
        <span className="comparison-arrow" aria-hidden="true">→</span>
        <div>
          <span>Resilient correctness</span>
          <strong>{formatRate(resilient.task_correctness_rate)}</strong>
        </div>
        <div className={`comparison-delta comparison-delta--${correctnessState.toLowerCase()}`}>
          <strong>{correctnessDelta >= 0 ? "+" : ""}{(correctnessDelta * 100).toFixed(1)} percentage points</strong>
          <span>{correctnessState}</span>
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
                    <span className={`change-state change-state--${state.toLowerCase().replace(" ", "-")}`}>
                      {state}
                    </span>
                    <small>{deltaLabel(fragileValue, resilientValue, metric.format)}</small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
