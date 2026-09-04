import {
  ApiClientError,
  approveRun,
  createEvaluation,
  getRuns,
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

    await approveRun("run-id", { actor: "dashboard-reviewer", allow: false, reason: "unsafe" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      actor: "dashboard-reviewer",
      allow: false,
      reason: "unsafe",
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
});
