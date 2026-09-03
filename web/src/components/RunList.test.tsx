import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RunList } from "./RunList";
import { runFixture } from "../test/fixtures";

describe("RunList", () => {
  it("renders a semantic six-field recent-runs table", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<RunList runs={[runFixture()]} state="ready" onSelect={onSelect} />);

    expect(screen.getByRole("table", { name: "Recent runs" })).toBeVisible();
    for (const heading of ["Run", "Scenario", "Mode", "Status", "Attempts", "Started"]) {
      expect(screen.getByRole("columnheader", { name: heading })).toBeVisible();
    }
    expect(screen.getByText("11111111-1111-1111-1111-111111111111")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Open run/ }));
    expect(onSelect).toHaveBeenCalledWith("11111111-1111-1111-1111-111111111111");
  });

  it("renders explicit loading, empty, and recoverable error states", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<RunList runs={[]} state="loading" />);
    expect(screen.getByLabelText("Loading recent runs")).toBeVisible();

    rerender(<RunList runs={[]} state="ready" />);
    expect(screen.getByText("No runs yet")).toBeVisible();

    rerender(<RunList runs={[]} state="error" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Runs unavailable");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
