import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";

import { App } from "./App";
import {
  evaluationFixture,
  runFixture,
  scenarioFixture,
  traceFixture,
} from "./test/fixtures";
import type { RunSummary } from "./types";

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function overviewFetch(
  options: {
    runs?: RunSummary[];
    evaluation?: ReturnType<typeof evaluationFixture> | null;
  } = {},
) {
  const runs = options.runs ?? [];
  const evaluation = options.evaluation === undefined ? evaluationFixture() : options.evaluation;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    const url = String(input);
    if (url.startsWith("/v1/runs?")) return response({ items: runs });
    if (url.startsWith("/v1/evaluations?")) {
      return response({ items: evaluation ? [evaluation] : [] });
    }
    if (url === "/v1/scenarios") return response({ items: [scenarioFixture] });
    throw new Error(`Unhandled request: ${url}`);
  });
}

describe("App workflows", () => {
  it("starts independent overview requests in parallel", async () => {
    const requests = [deferred<Response>(), deferred<Response>(), deferred<Response>()];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      void input;
      return requests[fetchMock.mock.calls.length - 1].promise;
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Run evaluation" })).toBeDisabled();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(
      expect.arrayContaining(["/v1/runs?limit=8", "/v1/evaluations?limit=1", "/v1/scenarios"]),
    );
    await act(async () => {
      requests[0].resolve(response({ items: [] }));
      requests[1].resolve(response({ items: [] }));
      requests[2].resolve(response({ items: [scenarioFixture] }));
      await Promise.all(requests.map((item) => item.promise));
    });
    expect(screen.getByRole("button", { name: "Run evaluation" })).toBeEnabled();
  });

  it("launches an API scenario and opens its live run detail", async () => {
    const fetchMock = overviewFetch({ evaluation: null });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/runs" && init?.method === "POST") return response(runFixture(), 201);
      if (url.endsWith("/trace?limit=100&after_sequence=0")) {
        return response({ events: traceFixture, next_after_sequence: 7, has_more: false });
      }
      if (url === `/v1/runs/${runFixture().id}`) return response(runFixture());
      return overviewFetch({ evaluation: null })(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.selectOptions(await screen.findByLabelText("Scenario"), "timeout-recovery");
    await user.selectOptions(screen.getByLabelText("Mode"), "resilient");
    await user.click(screen.getByRole("button", { name: "Start run" }));

    expect(await screen.findByRole("heading", { name: "timeout-recovery" })).toBeVisible();
    expect(screen.getByText("Succeeded")).toBeVisible();
    expect(await screen.findByText(/timeout injected/)).toBeVisible();
  });

  it("runs the launcher through approval, completion, and refreshed trace", async () => {
    const waiting = runFixture({
      scenario_id: "approval-reconstruction",
      status: "waiting_approval",
      approval_required: true,
      result: undefined,
    });
    const completed = runFixture({
      ...waiting,
      status: "succeeded",
      approval_required: false,
      result: { outcome: "prepared", evidence_refs: [] },
    });
    let approvalRecorded = false;
    const base = overviewFetch({ evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/scenarios") {
        return response({
          items: [
            {
              ...scenarioFixture,
              id: "approval-reconstruction",
              approval_required: true,
            },
          ],
        });
      }
      if (url === "/v1/runs" && init?.method === "POST") return response(waiting, 201);
      if (url === `/v1/runs/${waiting.id}` && !init?.method) return response(waiting);
      if (url.endsWith("/approvals") && init?.method === "POST") {
        approvalRecorded = true;
        return response(completed);
      }
      if (url.endsWith("/trace?limit=100&after_sequence=0")) {
        return response({
          events: approvalRecorded ? traceFixture : [],
          next_after_sequence: approvalRecorded ? 7 : 0,
          has_more: false,
        });
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Start run" }));
    await user.click(await screen.findByRole("button", { name: "Allow action" }));

    expect(await screen.findByText("Succeeded")).toBeVisible();
    expect(await screen.findByText(/timeout injected/)).toBeVisible();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approvals"))).toHaveLength(1);
  });

  it("deduplicates double allow and renders the completed response", async () => {
    const waiting = runFixture({
      scenario_id: "approval-reconstruction",
      status: "waiting_approval",
      approval_required: true,
      result: undefined,
    });
    const completed = runFixture({
      ...waiting,
      status: "succeeded",
      approval_required: false,
      result: { outcome: "prepared", evidence_refs: [] },
    });
    const approval = deferred<Response>();
    const base = overviewFetch({ runs: [waiting], evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/trace?limit=100&after_sequence=0")) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (url === `/v1/runs/${waiting.id}` && !init?.method) return response(waiting);
      if (url.endsWith("/approvals") && init?.method === "POST") return approval.promise;
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Open run approval-reconstruction/ }));
    const allow = await screen.findByRole("button", { name: "Allow action" });
    await user.click(allow);
    await user.click(allow);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approvals"))).toHaveLength(1);

    await act(async () => {
      approval.resolve(response(completed));
      await approval.promise;
    });
    expect(await screen.findByText("Succeeded")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Action allowed");
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("automatically clears an approval notice before it can persist over controls", async () => {
    const waiting = runFixture({
      scenario_id: "approval-reconstruction",
      status: "waiting_approval",
      approval_required: true,
      result: undefined,
    });
    const completed = runFixture({
      ...waiting,
      status: "succeeded",
      approval_required: false,
      result: { outcome: "prepared", evidence_refs: [] },
    });
    const base = overviewFetch({ runs: [waiting], evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/trace?limit=100&after_sequence=0")) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (url === `/v1/runs/${waiting.id}` && !init?.method) return response(waiting);
      if (url.endsWith("/approvals") && init?.method === "POST") return response(completed);
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Open run approval-reconstruction/ }));
    const allow = await screen.findByRole("button", { name: "Allow action" });
    vi.useFakeTimers();
    try {
      fireEvent.click(allow);
      await vi.waitFor(() => {
        expect(screen.getByRole("status")).toHaveTextContent("Action allowed");
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("sends deny and refreshes the run after an approval conflict", async () => {
    const waiting = runFixture({
      scenario_id: "approval-reconstruction",
      status: "waiting_approval",
      approval_required: true,
      result: undefined,
    });
    let approvalCalls = 0;
    let detailCalls = 0;
    const base = overviewFetch({ runs: [waiting], evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/trace?limit=100&after_sequence=0")) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (url === `/v1/runs/${waiting.id}` && !init?.method) {
        detailCalls += 1;
        return response(waiting);
      }
      if (url.endsWith("/approvals") && init?.method === "POST") {
        approvalCalls += 1;
        const body = JSON.parse(String(init.body)) as { allow: boolean };
        expect(body.allow).toBe(false);
        return response(
          { error: { code: "approval_conflict", message: "Conflict", details: {} } },
          409,
        );
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Open run approval-reconstruction/ }));
    await user.click(await screen.findByRole("button", { name: "Deny action" }));

    await waitFor(() => expect(detailCalls).toBeGreaterThanOrEqual(2));
    expect(approvalCalls).toBe(1);
    expect(screen.getByRole("status")).toHaveTextContent("Approval state refreshed");
  });

  it("prevents a deferred A response from replacing newer run B", async () => {
    const runA = runFixture({ id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", scenario_id: "run-a" });
    const runB = runFixture({ id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", scenario_id: "run-b" });
    const aDetail = deferred<Response>();
    const aTrace = deferred<Response>();
    const base = overviewFetch({ runs: [runA, runB], evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/v1/runs/${runA.id}`) return aDetail.promise;
      if (url.startsWith(`/v1/runs/${runA.id}/trace`)) return aTrace.promise;
      if (url === `/v1/runs/${runB.id}`) return response(runB);
      if (url.startsWith(`/v1/runs/${runB.id}/trace`)) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      return base(input);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Open run run-a/ }));
    await user.click(screen.getByRole("button", { name: /Open run run-b/ }));
    expect(await screen.findByRole("heading", { name: "run-b" })).toBeVisible();

    await act(async () => {
      aDetail.resolve(response(runA));
      aTrace.resolve(response({ events: traceFixture, next_after_sequence: 7, has_more: false }));
      await Promise.all([aDetail.promise, aTrace.promise]);
    });
    expect(screen.getByRole("heading", { name: "run-b" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "run-a" })).not.toBeInTheDocument();
  });

  it("ignores a deferred approval response after selection moves from A to B", async () => {
    const runA = runFixture({
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      scenario_id: "approval-a",
      status: "waiting_approval",
      approval_required: true,
      result: undefined,
    });
    const completedA = runFixture({
      ...runA,
      status: "succeeded",
      approval_required: false,
      result: { outcome: "prepared", evidence_refs: [] },
    });
    const runB = runFixture({
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      scenario_id: "run-b",
    });
    const approval = deferred<Response>();
    const base = overviewFetch({ runs: [runA, runB], evaluation: null });
    let runATraceCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/v1/runs/${runA.id}` && !init?.method) return response(runA);
      if (url.startsWith(`/v1/runs/${runA.id}/trace`)) {
        runATraceCalls += 1;
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (url === `/v1/runs/${runB.id}` && !init?.method) return response(runB);
      if (url.startsWith(`/v1/runs/${runB.id}/trace`)) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (url === `/v1/runs/${runA.id}/approvals` && init?.method === "POST") {
        return approval.promise;
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Open run approval-a/ }));
    await user.click(await screen.findByRole("button", { name: "Allow action" }));
    await user.click(screen.getByRole("button", { name: "Back to Runs" }));
    await user.click(await screen.findByRole("button", { name: /Open run run-b/ }));
    expect(await screen.findByRole("heading", { name: "run-b" })).toBeVisible();

    await act(async () => {
      approval.resolve(response(completedA));
      await approval.promise;
    });

    expect(screen.getByRole("heading", { name: "run-b" })).toBeVisible();
    expect(screen.queryByText("Loading run detail…")).not.toBeInTheDocument();
    expect(screen.queryByText("Action allowed")).not.toBeInTheDocument();
    expect(runATraceCalls).toBe(1);
  });

  it("renders useful empty/error states without per-run overview trace calls", async () => {
    const fetchMock = overviewFetch({ runs: [], evaluation: null });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    expect(await screen.findByText("No runs yet")).toBeVisible();
    expect(screen.getByText("No evaluations yet")).toBeVisible();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/trace"))).toBe(false);
  });

  it("creates the first evaluation once and renders its metrics without a reload", async () => {
    const evaluation = deferred<Response>();
    const base = overviewFetch({ runs: [], evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/v1/evaluations" && init?.method === "POST") {
        return evaluation.promise;
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    const runEvaluation = await screen.findByRole("button", { name: "Run evaluation" });
    await waitFor(() => expect(runEvaluation).toBeEnabled());
    await user.click(runEvaluation);
    await user.click(runEvaluation);

    expect(screen.getByRole("button", { name: "Running evaluation…" })).toBeDisabled();
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) => String(url) === "/v1/evaluations" && init?.method === "POST",
      ),
    ).toHaveLength(1);

    await act(async () => {
      evaluation.resolve(response(evaluationFixture(), 201));
      await evaluation.promise;
    });

    expect(
      within(screen.getByRole("region", { name: "Reliability metrics" })).getByText("91.7%"),
    ).toBeVisible();
    expect(screen.queryByText("No evaluations yet")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Evaluation complete");
    expect(screen.getByRole("button", { name: "Run evaluation" })).toBeEnabled();
  });

  it("keeps evaluation creation retryable after a stable API failure", async () => {
    const base = overviewFetch({ evaluation: null });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/v1/evaluations" && init?.method === "POST") {
        return response(
          {
            error: {
              code: "evaluation_in_progress",
              message: "Another evaluation is already running.",
              details: {},
            },
          },
          409,
        );
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    const runEvaluation = await screen.findByRole("button", { name: "Run evaluation" });
    await waitFor(() => expect(runEvaluation).toBeEnabled());
    await user.click(runEvaluation);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "The evaluation could not be completed. Retry when the API is available.",
    );
    expect(screen.getByRole("button", { name: "Run evaluation" })).toBeEnabled();
  });

  it("renders a recoverable dashboard error when an overview request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline secret")));
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Dashboard data could not be loaded" }),
    ).toBeVisible();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.queryByText("offline secret")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry dashboard" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
  });

  it("has no automatic axe violations in the loaded overview", async () => {
    vi.stubGlobal("fetch", overviewFetch({ runs: [runFixture()] }));
    const { container } = render(<App />);
    await screen.findByRole("table", { name: "Recent runs" });

    const result = await axe.run(container);
    expect(result.violations).toEqual([]);
  });

  it("uses the visual-contract headline and API-backed benchmark rail", async () => {
    vi.stubGlobal("fetch", overviewFetch({ runs: [runFixture()] }));
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Deterministic recovery evidence" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Run scenario" })).toBeVisible();
    const rail = screen.getByRole("region", { name: "Reliability metrics" });
    for (const label of [
      "Resilient correctness",
      "Recovery",
      "Fragile correctness",
      "Accepted invalid outputs",
    ]) {
      expect(within(rail).getByText(label)).toBeVisible();
    }
    expect(within(rail).getByText("91.7%")).toBeVisible();
    expect(within(rail).getByText("58.4%")).toBeVisible();
    expect(within(rail).getByText("0.0%")).toBeVisible();
  });

  it("keeps launcher controls keyboard operable", async () => {
    const fetchMock = overviewFetch({ evaluation: null });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/v1/runs" && init?.method === "POST") {
        return response(runFixture(), 201);
      }
      if (String(input).endsWith("/trace?limit=100&after_sequence=0")) {
        return response({ events: [], next_after_sequence: 0, has_more: false });
      }
      if (String(input) === `/v1/runs/${runFixture().id}`) return response(runFixture());
      return overviewFetch({ evaluation: null })(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    const start = await screen.findByRole("button", { name: "Start run" });
    start.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { name: "timeout-recovery" })).toBeVisible();
  });
});
