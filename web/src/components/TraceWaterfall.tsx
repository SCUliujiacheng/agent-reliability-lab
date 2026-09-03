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

function present(event: TraceEvent, recovered: boolean): TracePresentation {
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
      return recovered
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

function logicalAction(event: TraceEvent): string | null {
  const tool = event.payload.tool_name;
  const step = event.payload.action_step;
  if (
    typeof tool !== "string"
    || tool.length === 0
    || typeof step !== "number"
    || !Number.isInteger(step)
  ) return null;
  return `${step}:${tool}`;
}

function recoveredEvents(events: TraceEvent[]): Set<string> {
  const failedAttempts = new Map<string, number>();
  const recovered = new Set<string>();
  for (const event of events) {
    const action = logicalAction(event);
    const attempt = event.payload.attempt;
    if (action === null || typeof attempt !== "number" || !Number.isInteger(attempt)) continue;
    if (event.event_type === "tool.attempt.failed") {
      failedAttempts.set(action, Math.max(failedAttempts.get(action) ?? 0, attempt));
    } else if (event.event_type === "tool.attempt.succeeded") {
      const failedAttempt = failedAttempts.get(action);
      if (failedAttempt !== undefined && failedAttempt < attempt) recovered.add(event.id);
    }
  }
  return recovered;
}

function spanDepths(events: TraceEvent[]): Map<string, number> {
  const parents = new Map<string, string | null>();
  for (const event of events) {
    if (!parents.has(event.span_id)) parents.set(event.span_id, event.parent_span_id);
  }

  const depths = new Map<string, number>();
  for (const event of events) {
    let parent = event.parent_span_id;
    let depth = 0;
    const visited = new Set([event.span_id]);
    while (parent !== null && parents.has(parent) && depth < 2) {
      if (visited.has(parent)) {
        depth = 0;
        break;
      }
      visited.add(parent);
      depth += 1;
      parent = parents.get(parent) ?? null;
    }
    depths.set(event.span_id, depth);
  }
  return depths;
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

  const recovered = recoveredEvents(events);
  const depths = spanDepths(events);

  return (
    <ol className="trace-list" aria-label="Execution trace">
      {events.map((event) => {
        const presentation = present(event, recovered.has(event.id));
        const depth = depths.get(event.span_id) ?? 0;
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
