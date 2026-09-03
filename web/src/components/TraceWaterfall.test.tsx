import { render, screen } from "@testing-library/react";

import { TraceWaterfall } from "./TraceWaterfall";
import { traceFixture } from "../test/fixtures";

describe("TraceWaterfall", () => {
  it("shows timeout, retry, and recovered in chronological order", () => {
    render(<TraceWaterfall events={traceFixture} />);

    const rows = screen.getAllByTestId("trace-row").map((row) => row.textContent ?? "");
    const timeout = rows.findIndex((row) => row.includes("timeout injected"));
    const retry = rows.findIndex((row) => row.includes("Retry attempt 2"));
    const recovered = rows.findIndex((row) => row.includes("recovered"));
    expect(timeout).toBeGreaterThan(-1);
    expect(retry).toBeGreaterThan(timeout);
    expect(recovered).toBeGreaterThan(retry);
  });

  it("preserves unknown tool names and formats event durations", () => {
    const unknown = {
      ...traceFixture[1],
      id: "00000000-0000-0000-0000-000000000099",
      payload: { ...traceFixture[1].payload, tool_name: "diagnose_cache" },
      duration_ms: 2150,
    };
    render(<TraceWaterfall events={[unknown]} />);

    expect(screen.getByText(/diagnose_cache/)).toBeVisible();
    expect(screen.getByText("2.2s")).toBeVisible();
  });

  it("keeps all one hundred events visible with bounded indentation", () => {
    const events = Array.from({ length: 100 }, (_, index) => ({
      ...traceFixture[1],
      id: `00000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
      sequence: index + 1,
      payload: {
        ...traceFixture[1].payload,
        action_step: index,
        tool_name: `tool_${"x".repeat(80)}_${index}`,
      },
    }));
    render(<TraceWaterfall events={events} />);

    expect(screen.getAllByTestId("trace-row")).toHaveLength(100);
    expect(screen.getAllByTestId("trace-row")[99]).toHaveTextContent("tool_");
  });
});
