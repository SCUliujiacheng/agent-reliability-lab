import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Navigation } from "./Navigation";

describe("Navigation", () => {
  it("exposes one wordmark and direct links to the working sections", () => {
    render(<Navigation />);

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    for (const [name, href] of [
      ["Runs", "#runs"],
      ["Scenarios", "#scenarios"],
      ["Evaluations", "#evaluations"],
    ] as const) {
      expect(within(navigation).getByRole("link", { name })).toHaveAttribute("href", href);
    }
    expect(screen.getAllByRole("link", { name: "Agent Reliability Lab home" })).toHaveLength(1);
  });

  it("passes the selected section to the app when following a keyboard link", async () => {
    const onNavigate = vi.fn();
    const user = userEvent.setup();
    render(<Navigation onNavigate={onNavigate} />);

    screen.getByRole("link", { name: "Scenarios" }).focus();
    await user.keyboard("{Enter}");
    expect(onNavigate).toHaveBeenCalledWith("#scenarios");
  });
});
