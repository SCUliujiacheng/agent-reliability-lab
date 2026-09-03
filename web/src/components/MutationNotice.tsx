import { useEffect } from "react";

const AUTO_DISMISS_MS = 5_000;

interface MutationNoticeProps {
  message: string;
  onDismiss: () => void;
}

export function MutationNotice({ message, onDismiss }: MutationNoticeProps) {
  useEffect(() => {
    const timeout = window.setTimeout(onDismiss, AUTO_DISMISS_MS);
    return () => window.clearTimeout(timeout);
  }, [message, onDismiss]);

  return (
    <div className="mutation-notice">
      <span role="status">{message}</span>
      <button
        type="button"
        className="mutation-notice__dismiss"
        aria-label="Dismiss notification"
        onClick={onDismiss}
      >
        <span aria-hidden="true">×</span>
      </button>
    </div>
  );
}
