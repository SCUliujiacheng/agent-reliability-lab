import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ErrorBoundary } from "./ErrorBoundary";

let shouldThrow = true;

function FlakyChild() {
  if (shouldThrow) throw new Error("render failed");
  return <p>Recovered content</p>;
}

describe("ErrorBoundary", () => {
  it("lets the user recover after an unexpected render failure", async () => {
    shouldThrow = true;
    const user = userEvent.setup();
    render(
      <ErrorBoundary
        onReset={() => {
          shouldThrow = false;
        }}
      >
        <FlakyChild />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("This view could not be loaded");
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(screen.getByText("Recovered content")).toBeVisible();
  });
});
