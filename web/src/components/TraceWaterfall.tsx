import type { TraceEvent } from "../types";

interface TraceWaterfallProps {
  events: TraceEvent[];
}

interface TracePresentation {
  title: string;
  meta: string;
  tone: "neutral" | "active" | "warning" | "success" | "danger";
}

function textValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function numberValue(value: unknown, fallback = 1): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function humanize(value: string): string {
  const text = value.replaceAll(".", " ").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function present(event: TraceEvent): TracePresentation {
  const tool = textValue(event.payload.tool_name, "Agent step");
  const attempt = numberValue(event.payload.attempt);

  switch (event.event_type) {
    case "run.running":
      return { title: "Agent started", meta: "Run entered execution", tone: "active" };
    case "tool.attempt.started":
      return attempt > 1
        ? { title: `Retry attempt ${attempt} · ${tool}`, meta: "Tool execution", tone: "active" }
        : { title: `${tool} · attempt ${attempt}`, meta: "Tool execution", tone: "neutral" };
    case "fault.injected":
      return {
        title: `${textValue(event.payload.kind, "fault")} injected`,
        meta: tool,
        tone: "warning",
      };
    case "tool.attempt.failed":
      return {
        title: `${tool} · ${textValue(event.payload.code, "attempt failed")}`,
        meta: event.payload.transient ? "Transient failure" : "Failure",
        tone: "danger",
      };
    case "tool.attempt.succeeded":
      return attempt > 1
        ? { title: `${tool} · recovered`, meta: `Attempt ${attempt} succeeded`, tone: "success" }
        : { title: `${tool} · completed`, meta: "Tool succeeded", tone: "success" };
    case "run.succeeded":
      return {
        title: `Run completed · ${textValue(event.payload.outcome, "success")}`,
        meta: "Terminal success",
        tone: "success",
      };
    case "run.failed":
      return {
        title: `Run failed · ${textValue(event.payload.code, "unknown error")}`,
        meta: "Terminal failure",
        tone: "danger",
      };
    case "approval.requested":
      return { title: "Approval requested", meta: tool, tone: "warning" };
    case "approval.recorded":
      return {
        title: event.payload.allow ? "Action allowed" : "Action denied",
        meta: textValue(event.payload.actor, "Reviewer decision"),
        tone: event.payload.allow ? "success" : "danger",
      };
    default:
      return { title: humanize(event.event_type), meta: tool, tone: "neutral" };
  }
}

function formatDuration(durationMs: number | null | undefined): string {
  if (typeof durationMs !== "number" || !Number.isFinite(durationMs)) return "—";
  if (durationMs >= 1000) return `${(Math.round(durationMs / 100) / 10).toFixed(1)}s`;
  return `${durationMs}ms`;
}

function eventTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function TraceWaterfall({ events }: TraceWaterfallProps) {
  if (events.length === 0) {
    return (
      <div className="state-panel trace-empty">
        <strong>No trace events yet</strong>
        <p>Events will appear as the agent advances through the scenario.</p>
      </div>
    );
  }

  return (
    <ol className="trace-list" aria-label="Execution trace">
      {events.map((event) => {
        const presentation = present(event);
        const depth = Math.min(Math.max(numberValue(event.payload.action_step, 0), 0), 2);
        return (
          <li
            className={`trace-row trace-row--${presentation.tone} trace-row--depth-${depth}`}
            data-testid="trace-row"
            key={event.id}
          >
            <span className="trace-row__rail" aria-hidden="true"><span /></span>
            <div className="trace-row__copy">
              <strong>{presentation.title}</strong>
              <span>{presentation.meta}</span>
            </div>
            <div className="trace-row__timing">
              <time dateTime={event.created_at}>{eventTime(event.created_at)}</time>
              <span>{formatDuration(event.duration_ms)}</span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
