import { render, screen, within } from "@testing-library/react";

import { EvaluationComparison } from "./EvaluationComparison";
import { evaluationFixture } from "../test/fixtures";

describe("EvaluationComparison", () => {
  it("renders sentinel API metrics and labels correctness improvement in text", () => {
    render(<EvaluationComparison report={evaluationFixture()} />);

    expect(screen.getByText("58.4%")).toBeVisible();
    expect(screen.getByText("91.7%")).toBeVisible();
    expect(screen.getByText("+33.3 percentage points")).toBeVisible();
    expect(screen.getAllByText("Improved").length).toBeGreaterThan(0);
  });

  it("uses lower-is-better direction for invalid outputs and latency", () => {
    render(<EvaluationComparison report={evaluationFixture()} />);

    const invalid = screen.getByRole("row", { name: /Accepted invalid outputs/i });
    const latency = screen.getByRole("row", { name: /P95 latency/i });
    expect(within(invalid).getByText("Improved")).toBeVisible();
    expect(within(latency).getByText("Regressed")).toBeVisible();
  });

  it("renders null recovery as Not available", () => {
    render(
      <EvaluationComparison
        report={evaluationFixture(
          { recovery_rate: null },
          { recovery_rate: null },
        )}
      />,
    );

    expect(screen.getAllByText("Not available").length).toBeGreaterThanOrEqual(2);
  });
});
