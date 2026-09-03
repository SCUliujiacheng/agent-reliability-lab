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

function present(event: TraceEvent, retry: boolean, recovered: boolean): TracePresentation {
  const tool = textValue(event.payload.tool_name, "Agent step");
  const attempt = numberValue(event.payload.attempt);

  switch (event.event_type) {
    case "run.running":
      return { title: "Agent started", meta: "Run entered execution", tone: "active" };
    case "tool.attempt.started":
      return retry
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

function attemptNumber(event: TraceEvent): number | null {
  const attempt = event.payload.attempt;
  return typeof attempt === "number" && Number.isInteger(attempt) && attempt > 0
    ? attempt
    : null;
}

interface AttemptSemantics {
  retries: Set<string>;
  recoveries: Set<string>;
}

interface ActiveRetryAttempt {
  attempt: number;
  spanId: string;
}

function attemptSemantics(events: TraceEvent[]): AttemptSemantics {
  const scheduledAfter = new Map<string, number>();
  const activeRetryAttempt = new Map<string, ActiveRetryAttempt>();
  const retries = new Set<string>();
  const recovered = new Set<string>();
  for (const event of events) {
    const action = logicalAction(event);
    const attempt = attemptNumber(event);
    if (action === null || attempt === null) continue;
    if (event.event_type === "tool.attempt.failed") {
      activeRetryAttempt.delete(action);
      const delay = event.payload.retry_delay_seconds;
      if (
        event.status === "error"
        && event.payload.transient === true
        && typeof delay === "number"
        && Number.isFinite(delay)
        && delay >= 0
      ) {
        scheduledAfter.set(action, attempt);
      } else {
        scheduledAfter.delete(action);
      }
    } else if (event.event_type === "tool.attempt.started") {
      const priorAttempt = scheduledAfter.get(action);
      scheduledAfter.delete(action);
      if (
        event.status === "ok"
        && priorAttempt !== undefined
        && attempt === priorAttempt + 1
      ) {
        retries.add(event.id);
        activeRetryAttempt.set(action, { attempt, spanId: event.span_id });
      } else {
        activeRetryAttempt.delete(action);
      }
    } else if (event.event_type === "tool.attempt.succeeded") {
      const retryAttempt = activeRetryAttempt.get(action);
      if (
        event.status === "ok"
        && retryAttempt?.attempt === attempt
        && retryAttempt.spanId === event.span_id
      ) recovered.add(event.id);
      activeRetryAttempt.delete(action);
    }
  }
  return { retries, recoveries: recovered };
}

function lineageDepth(
  event: TraceEvent,
  parents: ReadonlyMap<string, string | null>,
): number {
  let parent = event.parent_span_id;
  let depth = 0;
  const visited = new Set([event.span_id]);
  while (parent !== null && parents.has(parent) && depth < 2) {
    if (visited.has(parent)) return 0;
    visited.add(parent);
    depth += 1;
    parent = parents.get(parent) ?? null;
  }
  return depth;
}

const ACTION_GROUP_EVENTS = new Set([
  "tool.attempt.started",
  "fault.injected",
  "tool.output.validation_failed",
  "tool.preflight.failed",
  "tool.attempt.failed",
  "tool.attempt.succeeded",
  "tool.attempt.cancelled",
  "run.waiting_approval",
  "approval.requested",
  "approval.recorded",
  "approval.denied",
  "run.checkpointed",
]);

const ATTEMPT_DETAIL_EVENTS = new Set([
  "fault.injected",
  "tool.output.validation_failed",
  "tool.attempt.failed",
  "tool.attempt.succeeded",
  "tool.attempt.cancelled",
]);

interface ActionRoot {
  tool: string | null;
}

function actionStep(event: TraceEvent): number | null {
  const step = event.payload.action_step;
  return typeof step === "number" && Number.isInteger(step) && step >= 0 ? step : null;
}

function belongsToAction(event: TraceEvent, root: ActionRoot): boolean {
  if (!ACTION_GROUP_EVENTS.has(event.event_type)) return false;
  const tool = event.payload.tool_name;
  const toolScoped = event.event_type.startsWith("tool.") || event.event_type === "fault.injected";
  if (toolScoped) return root.tool !== null && tool === root.tool;
  return typeof tool !== "string" || root.tool === null || tool === root.tool;
}

function eventDepths(events: TraceEvent[]): Map<string, number> {
  const parents = new Map<string, string | null>();
  for (const event of events) {
    if (!parents.has(event.span_id)) parents.set(event.span_id, event.parent_span_id);
  }

  const roots = new Map<number, ActionRoot>();
  const attemptBySpan = new Map<string, string>();
  const depths = new Map<string, number>();
  for (const event of events) {
    const step = actionStep(event);
    if (event.event_type === "policy.action" && step !== null) {
      const tool = event.payload.tool_name;
      roots.set(step, { tool: typeof tool === "string" ? tool : null });
      depths.set(event.id, 0);
      continue;
    }

    let semanticDepth = 0;
    const root = step === null ? undefined : roots.get(step);
    if (root !== undefined && belongsToAction(event, root)) {
      semanticDepth = 1;
      const action = logicalAction(event);
      const attempt = attemptNumber(event);
      const attemptIdentity = action === null || attempt === null
        ? null
        : `${action}:${attempt}`;
      if (event.event_type === "tool.attempt.started" && attemptIdentity !== null) {
        attemptBySpan.set(event.span_id, attemptIdentity);
      } else if (
        ATTEMPT_DETAIL_EVENTS.has(event.event_type)
        && attemptIdentity !== null
        && attemptBySpan.get(event.span_id) === attemptIdentity
      ) {
        semanticDepth = 2;
      }
    }
    depths.set(event.id, Math.min(2, Math.max(lineageDepth(event, parents), semanticDepth)));
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

  const attempts = attemptSemantics(events);
  const depths = eventDepths(events);

  return (
    <ol className="trace-list" aria-label="Execution trace">
      {events.map((event) => {
        const presentation = present(
          event,
          attempts.retries.has(event.id),
          attempts.recoveries.has(event.id),
        );
        const depth = depths.get(event.id) ?? 0;
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
