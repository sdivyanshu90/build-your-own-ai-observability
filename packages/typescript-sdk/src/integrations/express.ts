/**
 * Express / Connect middleware.
 *
 * Continues an inbound distributed trace, creates a server span per request,
 * and records the route *template* rather than the concrete path -- the
 * concrete path is unbounded cardinality and makes every dashboard useless.
 *
 * Express resolves `req.route` only during dispatch, so the span is renamed on
 * the way out rather than at creation.
 */

import { extract, withContext } from "../context.js";
import { getClient, type Client } from "../tracer.js";

interface MinimalRequest {
  method?: string;
  originalUrl?: string;
  url?: string;
  headers: Record<string, string | string[] | undefined>;
  route?: { path?: string };
  baseUrl?: string;
}

interface MinimalResponse {
  statusCode: number;
  setHeader(name: string, value: string): void;
  on(event: string, listener: () => void): void;
}

export interface MiddlewareOptions {
  client?: Client;
  excludedPaths?: string[];
  /** Return the trace id to the caller so a bug report is traceable. */
  exposeTraceHeader?: boolean;
}

export const TRACE_HEADER = "x-aiobs-trace-id";

export function aiobsMiddleware(options: MiddlewareOptions = {}) {
  const excluded = new Set(
    options.excludedPaths ?? ["/health", "/live", "/ready", "/metrics"],
  );
  const exposeHeader = options.exposeTraceHeader ?? true;

  return function middleware(
    request: MinimalRequest,
    response: MinimalResponse,
    next: (error?: unknown) => void,
  ): void {
    const path = request.originalUrl ?? request.url ?? "/";
    if (excluded.has(path.split("?")[0] ?? path)) {
      next();
      return;
    }

    const client = options.client ?? getClient();
    const method = request.method ?? "GET";
    const parent = extract(request.headers);
    const span = client.startSpan(`${method} ${path.split("?")[0]}`, {
      kind: "server",
      category: "http_request",
      parent,
    });
    span.setAttributes({
      "http.request.method": method,
      "url.path": path.split("?")[0] ?? path,
      "user_agent.original": String(request.headers["user-agent"] ?? "").slice(
        0,
        512,
      ),
    });

    if (exposeHeader) {
      try {
        response.setHeader(TRACE_HEADER, span.traceId);
      } catch {
        // Headers may already be sent by an earlier middleware; not fatal.
      }
    }

    response.on("finish", () => {
      const template = request.route?.path;
      if (template)
        span.name = `${method} ${(request.baseUrl ?? "") + template}`;
      span.setAttribute("http.response.status_code", response.statusCode);
      if (response.statusCode >= 500) {
        span.setStatus("error", `server returned ${response.statusCode}`);
      }
      span.end();
    });

    withContext(span.context, () => {
      next();
    });
  };
}
