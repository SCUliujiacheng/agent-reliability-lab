import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RunDetail } from "./RunDetail";
import { pendingApprovalFixture, runFixture, traceFixture } from "../test/fixtures";

describe("RunDetail", () => {
  it("keeps mobile summary facts, status text, trace, and actions semantic", async () => {
    const onBack = vi.fn();
    const onRunAgain = vi.fn();
    const user = userEvent.setup();
    render(
      <RunDetail
        run={runFixture()}
        events={traceFixture}
        state="ready"
        mutationState="idle"
        onBack={onBack}
        onRunAgain={onRunAgain}
        onApprove={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "timeout-recovery" })).toBeVisible();
    expect(screen.getByText("Succeeded")).toBeVisible();
    for (const term of ["Mode", "Attempts", "Outcome", "Duration"]) {
      expect(screen.getByText(term)).toBeVisible();
    }
    expect(screen.getByText("11111111-1111-1111-1111-111111111111")).toBeVisible();
    expect(screen.getByRole("button", { name: "Export trace" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Back to Runs" }));
    await user.click(screen.getByRole("button", { name: "Run again" }));
    expect(onBack).toHaveBeenCalledOnce();
    expect(onRunAgain).toHaveBeenCalledOnce();
  });

  it("shows the exact reviewed action and submits that identity with the decision", async () => {
    const onApprove = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <RunDetail
        run={runFixture()}
        events={[]}
        state="ready"
        mutationState="idle"
        onBack={vi.fn()}
        onRunAgain={vi.fn()}
        onApprove={onApprove}
      />,
    );
    expect(screen.queryByRole("button", { name: "Allow action" })).not.toBeInTheDocument();

    rerender(
      <RunDetail
        run={runFixture({
          status: "waiting_approval",
          approval_required: true,
          pending_approval: pendingApprovalFixture,
        })}
        events={[]}
        state="ready"
        mutationState="idle"
        onBack={vi.fn()}
        onRunAgain={vi.fn()}
        onApprove={onApprove}
      />,
    );
    expect(screen.getByText("prepare_rollback")).toBeVisible();
    expect(screen.getByText("Step 1")).toBeVisible();
    expect(screen.getByText(/deploy-2026-09-04-001/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Allow action" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Deny action" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow action" }));
    expect(onApprove).toHaveBeenCalledWith(pendingApprovalFixture, true);

    rerender(
      <RunDetail
        run={runFixture({
          status: "waiting_approval",
          approval_required: true,
          pending_approval: pendingApprovalFixture,
        })}
        events={[]}
        state="loading"
        mutationState="error"
        onBack={vi.fn()}
        onRunAgain={vi.fn()}
        onApprove={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Allow action" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deny action" })).toBeDisabled();
  });

  it("preserves long identifiers, summary fields, and one hundred trace rows at mobile width", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    const longId = `${"12345678-".repeat(20)}12345678`;
    const events = Array.from({ length: 100 }, (_, index) => ({
      ...traceFixture[1],
      id: `10000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
      sequence: index + 1,
      payload: {
        ...traceFixture[1].payload,
        tool_name: `diagnose_${"extremely_long_tool_name_".repeat(5)}${index}`,
      },
    }));

    render(
      <RunDetail
        run={runFixture({ id: longId })}
        events={events}
        state="ready"
        mutationState="idle"
        onBack={vi.fn()}
        onRunAgain={vi.fn()}
        onApprove={vi.fn()}
      />,
    );

    expect(screen.getByText(longId)).toBeVisible();
    expect(screen.getAllByTestId("trace-row")).toHaveLength(100);
    for (const term of ["Mode", "Attempts", "Outcome", "Duration"]) {
      expect(screen.getByText(term)).toBeVisible();
    }
  });
});
