import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  api,
  buildQuery,
  clearSession,
  getToken,
  setSession,
} from "@/lib/api";

describe("buildQuery", () => {
  it("repeats a parameter for each item, which is how the filter grammar composes", () => {
    const query = buildQuery({
      filter: ["status:eq:error", "model:contains:gpt"],
    });
    expect(query).toBe(
      "?filter=status%3Aeq%3Aerror&filter=model%3Acontains%3Agpt",
    );
  });

  it("omits absent values instead of sending empty strings", () => {
    expect(buildQuery({ a: undefined, b: null, c: "", d: 0, e: false })).toBe(
      "?d=0&e=false",
    );
  });

  it("returns an empty string when nothing is set", () => {
    expect(buildQuery({ a: undefined })).toBe("");
  });

  it("percent-encodes values so a cursor cannot break out of the query string", () => {
    expect(buildQuery({ cursor: "a b&c=d" })).toBe("?cursor=a+b%26c%3Dd");
  });
});

describe("ApiError", () => {
  it("classifies transient failures as retryable and permanent ones as not", () => {
    const transient = new ApiError(503, {
      code: "dependency_unavailable",
      message: "down",
    } as never);
    const permanent = new ApiError(400, {
      code: "validation_error",
      message: "bad",
    } as never);
    expect(transient.retryable).toBe(true);
    expect(permanent.retryable).toBe(false);
  });

  it("recognises an authentication failure", () => {
    expect(
      new ApiError(401, { code: "unauthenticated", message: "" } as never)
        .isAuthError,
    ).toBe(true);
    expect(
      new ApiError(403, { code: "permission_denied", message: "" } as never)
        .isAuthError,
    ).toBe(false);
  });
});

describe("session storage", () => {
  afterEach(() => clearSession());

  it("keeps the token out of cookies so it is unavailable to CSRF", () => {
    setSession("token-abc", "org-1");
    expect(getToken()).toBe("token-abc");
    expect(document.cookie).not.toContain("token-abc");
  });

  it("clears both token and organization on sign-out", () => {
    setSession("token-abc", "org-1");
    clearSession();
    expect(getToken()).toBeNull();
  });
});

describe("request handling", () => {
  const originalFetch = global.fetch;

  beforeEach(() => clearSession());
  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("sends the bearer token when a session exists", async () => {
    setSession("token-abc", "org-1");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.projects();

    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>).authorization).toBe(
      "Bearer token-abc",
    );
  });

  it("raises a typed ApiError carrying the server error body", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: "not_found",
          message: "no such trace",
          request_id: "req-9",
        }),
        {
          status: 404,
        },
      ),
    ) as unknown as typeof fetch;

    await expect(api.trace("abc", { project_id: "p" })).rejects.toMatchObject({
      status: 404,
      body: { code: "not_found", request_id: "req-9" },
    });
  });

  it("treats 204 as a successful empty response rather than a parse failure", async () => {
    setSession("token-abc", "org-1");
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(null, { status: 204 }),
      ) as unknown as typeof fetch;
    await expect(api.revokeApiKey("key-1")).resolves.toBeUndefined();
  });

  it("clears the session even when logout fails, so a stale token is never kept", async () => {
    setSession("token-abc", "org-1");
    global.fetch = vi
      .fn()
      .mockRejectedValue(new Error("network down")) as unknown as typeof fetch;
    await expect(api.logout()).rejects.toThrow("network down");
    expect(getToken()).toBeNull();
  });
});
