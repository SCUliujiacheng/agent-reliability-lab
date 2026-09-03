import { render, screen } from "@testing-library/react";

import { TraceWaterfall } from "./TraceWaterfall";
import { traceFixture } from "../test/fixtures";
import type { TraceEvent } from "../types";

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

  it("shows a clear placeholder for omitted and non-finite API durations", () => {
    const omittedDuration = {
      id: "00000000-0000-0000-0000-000000000090",
      trace_id: "22222222-2222-2222-2222-222222222222",
      span_id: "00000000-0000-0000-0000-000000000190",
      parent_span_id: null,
      sequence: 1,
      event_type: "run.running",
      payload: { from_status: "queued" },
      status: "ok",
      created_at: "2026-09-04T10:45:12Z",
    } satisfies TraceEvent;
    const nonFiniteDuration = {
      ...omittedDuration,
      id: "00000000-0000-0000-0000-000000000091",
      sequence: 2,
      duration_ms: Number.POSITIVE_INFINITY,
    };

    render(<TraceWaterfall events={[omittedDuration, nonFiniteDuration]} />);

    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.queryByText("undefinedms")).not.toBeInTheDocument();
    expect(screen.queryByText("Infinityms")).not.toBeInTheDocument();
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
