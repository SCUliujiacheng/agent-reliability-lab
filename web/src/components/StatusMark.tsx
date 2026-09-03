import type { RunStatus } from "../types";

const STATUS_LABELS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  waiting_approval: "Waiting approval",
  succeeded: "Succeeded",
  failed: "Failed",
};

const STATUS_GLYPHS: Record<RunStatus, string> = {
  queued: "○",
  running: "•",
  waiting_approval: "!",
  succeeded: "✓",
  failed: "×",
};

interface StatusMarkProps {
  status: RunStatus;
}

export function StatusMark({ status }: StatusMarkProps) {
  return (
    <span className={`status-mark status-mark--${status}`}>
      <span className="status-mark__glyph" aria-hidden="true">
        {STATUS_GLYPHS[status]}
      </span>
      {STATUS_LABELS[status]}
    </span>
  );
}

export function statusLabel(status: RunStatus): string {
  return STATUS_LABELS[status];
}
