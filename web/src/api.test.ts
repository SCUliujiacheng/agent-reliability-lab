import {
  ApiClientError,
  approveRun,
  createEvaluation,
  getRuns,
  getTrace,
  requestJson,
} from "./api";

describe("typed API client", () => {
  it("preserves stable server errors without leaking response bodies", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: "invalid_transition", message: "No.", details: {} },
          }),
          { status: 409, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await expect(getRuns()).rejects.toMatchObject({
      code: "invalid_transition",
      status: 409,
    });
  });

  it("maps non-JSON and network failures to typed errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("proxy secret", { status: 502 })));
    await expect(requestJson("/v1/runs")).rejects.toEqual(
      expect.objectContaining({ code: "http_error", status: 502 }),
    );

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(requestJson("/v1/runs")).rejects.toEqual(
      expect.objectContaining({ code: "network_error", status: 0 }),
    );
  });

  it("sends only the public approval body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await approveRun("run-id", {
      actor: "dashboard-reviewer",
      allow: false,
      reason: "unsafe",
      action_step: 3,
      action_fingerprint: "a".repeat(64),
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      actor: "dashboard-reviewer",
      allow: false,
      reason: "unsafe",
      action_step: 3,
      action_fingerprint: "a".repeat(64),
    });
    expect(ApiClientError).toBeDefined();
  });

  it("creates the catalog evaluation with the narrow public body", async () => {
    const report = { evaluation_id: "11111111-1111-1111-1111-111111111111" };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(createEvaluation()).resolves.toEqual(report);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/v1/evaluations");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ suite: "incident-response" });
  });

  it("loads every trace page before exposing an audit or export", async () => {
    const firstEvent = { id: "event-1", sequence: 1 };
    const secondEvent = { id: "event-2", sequence: 2 };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            events: [firstEvent],
            next_after_sequence: 1,
            has_more: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            events: [secondEvent],
            next_after_sequence: 2,
            has_more: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const trace = await getTrace("run/id");

    expect(trace.events).toEqual([firstEvent, secondEvent]);
    expect(trace.has_more).toBe(false);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/v1/runs/run%2Fid/trace?limit=100&after_sequence=0",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/runs/run%2Fid/trace?limit=100&after_sequence=1",
      expect.any(Object),
    );
  });

  it("fails closed when a trace page claims more data without advancing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            events: [],
            next_after_sequence: 0,
            has_more: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await expect(getTrace("run-id")).rejects.toMatchObject({
      code: "invalid_trace_page",
      status: 502,
    });
  });

  it("rejects an advancing empty page instead of requesting forever", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            events: [],
            next_after_sequence: 1,
            has_more: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockRejectedValue(new Error("a second request must not be made"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTrace("run-id")).rejects.toMatchObject({
      code: "invalid_trace_page",
      status: 502,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("bounds trace pagination even when every cursor advances", async () => {
    let pageNumber = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      pageNumber += 1;
      const firstSequence = (pageNumber - 1) * 100 + 1;
      return Promise.resolve(
        new Response(
          JSON.stringify({
            events: Array.from({ length: 100 }, (_, index) => ({
              id: `event-${firstSequence + index}`,
              sequence: firstSequence + index,
            })),
            next_after_sequence: pageNumber * 100,
            has_more: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTrace("run-id")).rejects.toMatchObject({
      code: "trace_too_large",
      status: 502,
    });
    expect(fetchMock).toHaveBeenCalledTimes(100);
  });

  it.each([
    {
      label: "cursor skips past the final event",
      events: [{ id: "event-1", sequence: 1 }],
      nextAfterSequence: 2,
      hasMore: true,
    },
    {
      label: "event sequence is duplicated",
      events: [
        { id: "event-1", sequence: 1 },
        { id: "event-duplicate", sequence: 1 },
      ],
      nextAfterSequence: 1,
      hasMore: false,
    },
    {
      label: "event sequence contains a gap",
      events: [
        { id: "event-1", sequence: 1 },
        { id: "event-3", sequence: 3 },
      ],
      nextAfterSequence: 3,
      hasMore: false,
    },
  ])("rejects an inconsistent trace page: $label", async (page) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            events: page.events,
            next_after_sequence: page.nextAfterSequence,
            has_more: page.hasMore,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await expect(getTrace("run-id")).rejects.toMatchObject({
      code: "invalid_trace_page",
      status: 502,
    });
  });
});
