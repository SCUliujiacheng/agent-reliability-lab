import { render, screen } from "@testing-library/react";

import { TraceWaterfall } from "./TraceWaterfall";
import { runtimeRetryTraceFixture, traceFixture } from "../test/fixtures";
import type { TraceEvent } from "../types";

describe("TraceWaterfall", () => {
  it("shows timeout, retry, and recovered in chronological order", () => {
    render(<TraceWaterfall events={runtimeRetryTraceFixture} />);

    const rows = screen.getAllByTestId("trace-row").map((row) => row.textContent ?? "");
    const timeout = rows.findIndex((row) => row.includes("timeout injected"));
    const retry = rows.findIndex((row) => row.includes("Retry attempt 2"));
    const recovered = rows.findIndex((row) => row.includes("recovered"));
    expect(timeout).toBeGreaterThan(-1);
    expect(retry).toBeGreaterThan(timeout);
    expect(recovered).toBeGreaterThan(retry);
  });

  it("groups a real runtime retry chain below its policy action", () => {
    render(<TraceWaterfall events={runtimeRetryTraceFixture} />);

    const rows = screen.getAllByTestId("trace-row");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Policy action"),
      expect.stringContaining("search_recent_logs · attempt 1"),
      expect.stringContaining("timeout injected"),
      expect.stringContaining("search_recent_logs · tool_timeout"),
      expect.stringContaining("Retry attempt 2 · search_recent_logs"),
      expect.stringContaining("search_recent_logs · recovered"),
      expect.stringContaining("Run checkpointed"),
    ]);
    expect(rows.map((row) => row.className)).toEqual([
      expect.stringContaining("trace-row--depth-0"),
      expect.stringContaining("trace-row--depth-1"),
      expect.stringContaining("trace-row--depth-2"),
      expect.stringContaining("trace-row--depth-2"),
      expect.stringContaining("trace-row--depth-1"),
      expect.stringContaining("trace-row--depth-2"),
      expect.stringContaining("trace-row--depth-1"),
    ]);
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

  it("does not call an isolated second attempt a retry or recovery", () => {
    const isolatedStart = {
      ...traceFixture[4],
      id: "00000000-0000-0000-0000-000000000091",
      sequence: 1,
      payload: { tool_name: "search_recent_logs", attempt: 2, action_step: 7 },
    };
    const isolatedSuccess = {
      ...traceFixture[5],
      id: "00000000-0000-0000-0000-000000000092",
      sequence: 2,
      payload: { tool_name: "search_recent_logs", attempt: 2, action_step: 7 },
    };

    render(<TraceWaterfall events={[isolatedStart, isolatedSuccess]} />);

    expect(screen.getByText("search_recent_logs · attempt 2")).toBeVisible();
    expect(screen.getByText("search_recent_logs · completed")).toBeVisible();
    expect(screen.queryByText(/Retry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/recovered/)).not.toBeInTheDocument();
  });

  it.each([
    ["permanent failure", false, undefined, "error"],
    ["transient failure without a scheduled delay", true, undefined, "error"],
    ["a failed event whose status is not error", true, 0.1, "ok"],
  ])("does not infer retry or recovery after %s", (
    _label,
    transient,
    retryDelay,
    status,
  ) => {
    const failed = {
      ...traceFixture[3],
      id: "00000000-0000-0000-0000-000000000097",
      sequence: 1,
      payload: {
        tool_name: "search_recent_logs",
        attempt: 1,
        action_step: 7,
        code: "tool_failed",
        transient,
        ...(retryDelay === undefined ? {} : { retry_delay_seconds: retryDelay }),
      },
      status,
    };
    const secondStart = {
      ...traceFixture[4],
      id: "00000000-0000-0000-0000-000000000098",
      sequence: 2,
      payload: { tool_name: "search_recent_logs", attempt: 2, action_step: 7 },
    };
    const secondSuccess = {
      ...traceFixture[5],
      id: "00000000-0000-0000-0000-000000000099",
      sequence: 3,
      payload: { tool_name: "search_recent_logs", attempt: 2, action_step: 7 },
    };

    render(<TraceWaterfall events={[failed, secondStart, secondSuccess]} />);

    expect(screen.getByText("search_recent_logs · attempt 2")).toBeVisible();
    expect(screen.getByText("search_recent_logs · completed")).toBeVisible();
    expect(screen.queryByText(/Retry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/recovered/)).not.toBeInTheDocument();
  });

  it("requires a recovered success to close the started retry span", () => {
    const mismatchedSuccess = {
      ...runtimeRetryTraceFixture[5],
      span_id: "00000000-0000-0000-0000-000000000399",
    };

    render(
      <TraceWaterfall
        events={[...runtimeRetryTraceFixture.slice(0, 5), mismatchedSuccess]}
      />,
    );

    expect(screen.getByText("Retry attempt 2 · search_recent_logs")).toBeVisible();
    expect(screen.getByText("search_recent_logs · completed")).toBeVisible();
    expect(screen.queryByText(/recovered/)).not.toBeInTheDocument();
  });

  it("derives bounded indentation from span lineage instead of action step", () => {
    const rootSpan = "00000000-0000-0000-0000-000000000200";
    const childSpan = "00000000-0000-0000-0000-000000000201";
    const grandchildSpan = "00000000-0000-0000-0000-000000000202";
    const root = {
      ...traceFixture[0],
      id: "00000000-0000-0000-0000-000000000093",
      span_id: rootSpan,
      payload: { from_status: "queued", action_step: 99 },
    };
    const child = {
      ...traceFixture[1],
      id: "00000000-0000-0000-0000-000000000094",
      span_id: childSpan,
      parent_span_id: rootSpan,
      payload: { tool_name: "child", attempt: 1, action_step: 0 },
    };
    const grandchild = {
      ...traceFixture[1],
      id: "00000000-0000-0000-0000-000000000095",
      span_id: grandchildSpan,
      parent_span_id: childSpan,
      payload: { tool_name: "grandchild", attempt: 1, action_step: 0 },
    };
    const unknownParent = {
      ...traceFixture[1],
      id: "00000000-0000-0000-0000-000000000096",
      span_id: "00000000-0000-0000-0000-000000000203",
      parent_span_id: "00000000-0000-0000-0000-000000000299",
      payload: { tool_name: "orphan", attempt: 1, action_step: 99 },
    };

    render(<TraceWaterfall events={[root, child, grandchild, unknownParent]} />);

    const rows = screen.getAllByTestId("trace-row");
    expect(rows[0]).toHaveClass("trace-row--depth-0");
    expect(rows[1]).toHaveClass("trace-row--depth-1");
    expect(rows[2]).toHaveClass("trace-row--depth-2");
    expect(rows[3]).toHaveClass("trace-row--depth-0");
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
    expect(screen.getAllByTestId("trace-row")[99]).toHaveClass("trace-row--depth-0");
  });
});
