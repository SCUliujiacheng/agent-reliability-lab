interface MetricCardProps {
  label: string;
  value: string;
  detail: string;
  tone?: "default" | "positive" | "warning" | "fragile";
}

export function MetricCard({ label, value, detail, tone = "default" }: MetricCardProps) {
  return (
    <article className={`metric-card metric-card--${tone}`}>
      <p className="eyebrow">{label}</p>
      <p className="metric-card__value">{value}</p>
      <p className="metric-card__detail">{detail}</p>
    </article>
  );
}
