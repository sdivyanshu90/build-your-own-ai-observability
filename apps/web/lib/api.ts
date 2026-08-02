/**
 * API client.
 *
 * One place that knows how to talk to the platform, so error handling, auth and
 * the query grammar are implemented once rather than in every component.
 *
 * The access token lives in `sessionStorage`, not a cookie. That is a
 * deliberate trade: it is unavailable to a CSRF attack (no ambient
 * credentials), at the cost of being reachable by XSS -- which the strict CSP
 * and the escaping rules in `SafeText` are there to prevent. A cookie would
 * invert those risks and require CSRF tokens on every mutation.
 */

import type {
  AgentGraph,
  CursorPage,
  DashboardSeries,
  ErrorResponse,
  OverviewSummary,
  PercentileResult,
  Project,
  PromptVersion,
  RetrievalStage,
  Trace,
  TraceDetail,
} from "@aiobs/schemas";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:58000";

const TOKEN_KEY = "aiobs.access_token";
const ORG_KEY = "aiobs.organization_id";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: ErrorResponse,
  ) {
    super(body.message);
    this.name = "ApiError";
  }

  /** Whether retrying could plausibly succeed. */
  get retryable(): boolean {
    return [
      "rate_limited",
      "internal_error",
      "dependency_unavailable",
      "timeout",
    ].includes(this.body.code);
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(TOKEN_KEY);
}

/**
 * Session-change subscribers.
 *
 * `sessionStorage` is not observable: the `storage` event fires for other tabs,
 * never for the tab that made the change. Without this, signing in navigates to
 * a workspace whose provider mounted before a token existed and so never
 * fetched anything -- the classic "log in, see an empty app until you reload".
 */
const sessionListeners = new Set<() => void>();

export function onSessionChange(listener: () => void): () => void {
  sessionListeners.add(listener);
  return () => {
    sessionListeners.delete(listener);
  };
}

function notifySessionChange(): void {
  for (const listener of [...sessionListeners]) {
    try {
      listener();
    } catch {
      // A broken subscriber must not prevent the others from being told, nor
      // break the login flow that triggered this.
    }
  }
}

export function setSession(token: string, organizationId: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, token);
  window.sessionStorage.setItem(ORG_KEY, organizationId);
  notifySessionChange();
}

export function clearSession(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(ORG_KEY);
  notifySessionChange();
}

export function getOrganizationId(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(ORG_KEY);
}

type QueryValue = string | number | boolean | undefined | null | string[];

export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      // Repeated parameters, which is how the filter grammar expresses
      // multiple conditions.
      for (const item of value) search.append(key, item);
    } else {
      search.append(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new ApiError(response.status, body as ErrorResponse);
  }
  return body as T;
}

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------

export interface LoginResult {
  access_token: string;
  refresh_token: string;
  organization_id: string;
  role: string;
  expires_in: number;
}

export const api = {
  async login(
    email: string,
    password: string,
    organizationId?: string,
  ): Promise<LoginResult> {
    const result = await request<LoginResult>("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        ...(organizationId ? { organization_id: organizationId } : {}),
      }),
    });
    setSession(result.access_token, result.organization_id);
    return result;
  },

  async logout(): Promise<void> {
    try {
      await request<void>("/v1/auth/logout", { method: "POST" });
    } finally {
      clearSession();
    }
  },

  me: () =>
    request<{
      user: { id: string; email: string; display_name: string };
      role: string;
    }>("/v1/auth/me"),

  organizations: () =>
    request<{ id: string; slug: string; name: string; role: string | null }[]>(
      "/v1/auth/organizations",
    ),

  // -------------------------------------------------------------------------
  // projects
  // -------------------------------------------------------------------------

  projects: () => request<Project[]>("/v1/projects"),
  project: (id: string) => request<Project>(`/v1/projects/${id}`),

  // -------------------------------------------------------------------------
  // traces
  // -------------------------------------------------------------------------

  traces: (params: {
    project_id: string;
    environment?: string;
    start: string;
    end: string;
    filter?: string[];
    sort?: string;
    q?: string;
    limit?: number;
    cursor?: string;
  }) => request<CursorPage<Trace>>(`/v1/traces${buildQuery(params)}`),

  trace: (
    traceId: string,
    params: { project_id: string; environment?: string },
  ) => request<TraceDetail>(`/v1/traces/${traceId}${buildQuery(params)}`),

  retrieval: (traceId: string, params: { project_id: string }) =>
    request<RetrievalStage[]>(
      `/v1/traces/${traceId}/retrieval${buildQuery(params)}`,
    ),

  trajectory: (traceId: string, params: { project_id: string }) =>
    request<{ graph: AgentGraph; steps: unknown[] }>(
      `/v1/traces/${traceId}/trajectory${buildQuery(params)}`,
    ),

  compare: (params: { project_id: string; left: string; right: string }) =>
    request<{
      left: Trace;
      right: Trace;
      // Cost deltas arrive as decimal strings; everything else as numbers. The
      // union is the honest type -- narrowing it to `number` would invite a
      // parseFloat somewhere and quietly corrupt money.
      summary_deltas: Record<
        string,
        {
          left: number | string | null;
          right: number | string | null;
          absolute: number | string | null;
          relative: number | null;
        }
      >;
      matched_spans: Record<string, unknown>[];
      only_in_left: string[];
      only_in_right: string[];
      // Most lineage fields are lists of version ids; `release` is a single
      // string. The union is what the API actually sends.
      lineage_differences: Record<
        string,
        {
          left: string[] | string | null;
          right: string[] | string | null;
          changed: boolean;
        }
      >;
    }>(`/v1/traces/compare${buildQuery(params)}`),

  // -------------------------------------------------------------------------
  // metrics
  // -------------------------------------------------------------------------

  overview: (params: {
    project_id: string;
    environment?: string;
    start: string;
    end: string;
    compare_previous?: boolean;
    filter?: string[];
  }) => request<OverviewSummary>(`/v1/metrics/overview${buildQuery(params)}`),

  timeseries: (params: {
    project_id: string;
    environment?: string;
    start: string;
    end: string;
    metric?: string;
    aggregation?: string;
    interval?: string;
    group_by?: string[];
    source?: string;
    filter?: string[];
  }) => request<DashboardSeries>(`/v1/metrics/timeseries${buildQuery(params)}`),

  latency: (params: {
    project_id: string;
    environment?: string;
    start: string;
    end: string;
    group_by?: string[];
    source?: string;
  }) =>
    request<{ unit: string; column: string; groups: PercentileResult[] }>(
      `/v1/metrics/latency${buildQuery(params)}`,
    ),

  values: (params: {
    project_id: string;
    column: string;
    start: string;
    end: string;
    prefix?: string;
  }) =>
    request<{ column: string; values: { value: string; count: number }[] }>(
      `/v1/metrics/values${buildQuery(params)}`,
    ),

  costs: (params: {
    project_id: string;
    environment?: string;
    start: string;
    end: string;
    group_by?: string[];
  }) =>
    request<{
      group_by: string[];
      groups: { keys: string[]; total: string | null; count: number }[];
    }>(`/v1/costs${buildQuery(params)}`),

  // -------------------------------------------------------------------------
  // registries
  // -------------------------------------------------------------------------

  prompts: (projectId: string) =>
    request<
      { id: string; name: string; description: string | null; tags: string[] }[]
    >(`/v1/prompts${buildQuery({ project_id: projectId })}`),

  promptVersions: (promptId: string) =>
    request<PromptVersion[]>(`/v1/prompts/${promptId}/versions`),

  promptAliases: (promptId: string) =>
    request<
      {
        name: string;
        version_id: string;
        previous_version_id: string | null;
        promoted_at: string;
      }[]
    >(`/v1/prompts/${promptId}/aliases`),

  promptDiff: (versionId: string, against: string) =>
    request<{
      identical: boolean;
      left_hash: string;
      right_hash: string;
      engine_changed: boolean;
      message_changes: Record<string, unknown>[];
      variable_changes: {
        added: string[];
        removed: string[];
        modified: string[];
      };
    }>(`/v1/prompts/versions/${versionId}/diff${buildQuery({ against })}`),

  models: (projectId?: string) =>
    request<
      {
        id: string;
        provider: string;
        model_identifier: string;
        family: string | null;
        endpoint_kind: string;
      }[]
    >(`/v1/models${buildQuery({ project_id: projectId })}`),

  modelVersions: (modelId: string) =>
    request<Record<string, unknown>[]>(`/v1/models/${modelId}/versions`),

  datasets: (projectId: string) =>
    request<
      {
        id: string;
        name: string;
        description: string | null;
        license: string | null;
        contains_sensitive_data: boolean;
      }[]
    >(`/v1/datasets${buildQuery({ project_id: projectId })}`),

  datasetVersions: (datasetId: string) =>
    request<Record<string, unknown>[]>(`/v1/datasets/${datasetId}/versions`),

  // -------------------------------------------------------------------------
  // administration
  // -------------------------------------------------------------------------

  apiKeys: (projectId?: string) =>
    request<
      {
        id: string;
        name: string;
        prefix: string;
        project_id: string;
        scopes: string[];
        created_at: string;
        expires_at: string | null;
        revoked_at: string | null;
        last_used_at: string | null;
      }[]
    >(`/v1/api-keys${buildQuery({ project_id: projectId })}`),

  createApiKey: (payload: {
    name: string;
    project_id: string;
    environment_id: string;
    scopes: string[];
  }) =>
    request<{ id: string; name: string; prefix: string; secret: string }>(
      "/v1/api-keys",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  revokeApiKey: (id: string) =>
    request<void>(`/v1/api-keys/${id}`, { method: "DELETE" }),

  members: () =>
    request<
      {
        user: { id: string; email: string; display_name: string };
        role: string;
      }[]
    >("/v1/members"),

  priceBooks: () =>
    request<
      {
        id: string;
        version: string;
        name: string;
        currency: string;
        entry_count: number;
        organization_id: string | null;
        published_at: string;
      }[]
    >("/v1/price-books"),

  priceEntries: (bookId: string) =>
    request<
      {
        id: string;
        provider: string;
        model_identifier: string;
        usage_category: string;
        unit_quantity: number;
        unit_price: string;
        currency: string;
        effective_from: string;
        effective_to: string | null;
        source_url: string | null;
      }[]
    >(`/v1/price-books/${bookId}/entries`),

  retention: (projectId: string) =>
    request<
      {
        id: string;
        raw_span_days: number;
        aggregate_days: number;
        payload_days: number;
      }[]
    >(`/v1/projects/${projectId}/retention`),

  auditEvents: (params: {
    start: string;
    end: string;
    limit?: number;
    cursor?: string;
  }) =>
    request<
      CursorPage<{
        id: string;
        occurred_at: string;
        action: string;
        actor_label: string | null;
        actor_type: string;
        resource_type: string;
        resource_id: string | null;
        outcome: string;
        request_id: string | null;
      }>
    >(`/v1/audit-events${buildQuery(params)}`),

  exports: () =>
    request<
      {
        id: string;
        resource: string;
        format: string;
        status: string;
        row_count: number;
        redacted: boolean;
        created_at: string;
      }[]
    >("/v1/exports"),

  createExport: (
    payload: { project_id: string; resource: string; format: string },
    window: { start: string; end: string },
  ) =>
    request<{ id: string; status: string }>(
      `/v1/exports${buildQuery(window)}`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  health: () =>
    request<{
      status: string;
      version: string;
      checks: Record<string, string>;
    }>("/health"),
};
