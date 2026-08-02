"use client";

/**
 * Application-wide context.
 *
 * Two things live here because every screen needs them and neither belongs in a
 * component:
 *
 * - the React Query client, configured so a 401 logs out instead of retrying
 *   forever against an expired token;
 * - the workspace selection (project, environment, time range), which is the
 *   implicit scope of nearly every API call.
 *
 * The workspace lives in React state mirrored to the URL by the pages that care,
 * not in a global store. Deep links must survive a reload, and a URL that
 * encodes what you are looking at is the difference between "look at this trace"
 * being a link and being a screenshot.
 */

import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Project } from "@aiobs/schemas";

import {
  ApiError,
  api,
  clearSession,
  getToken,
  onSessionChange,
} from "@/lib/api";
import { TIME_RANGES, type TimeRange } from "@/lib/format";

interface Workspace {
  projects: Project[];
  project: Project | null;
  projectId: string | null;
  environment: string;
  range: TimeRange;
  loading: boolean;
  error: unknown;
  setProjectId: (id: string) => void;
  setEnvironment: (name: string) => void;
  setRange: (range: TimeRange) => void;
  reload: () => void;
}

const WorkspaceContext = createContext<Workspace | null>(null);

const PROJECT_STORAGE_KEY = "aiobs.project_id";
const RANGE_STORAGE_KEY = "aiobs.range";
/** Environment is remembered per project: "staging" means a different thing in
 *  each one, and carrying the choice across projects is more surprising than
 *  helpful. */
const environmentStorageKey = (projectId: string) =>
  `aiobs.environment.${projectId}`;

export function useWorkspace(): Workspace {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside <Providers>");
  return value;
}

function createQueryClient(onUnauthorized: () => void): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (error instanceof ApiError && error.isAuthError) onUnauthorized();
      },
    }),
    defaultOptions: {
      queries: {
        // Observability data is append-only and time-windowed; refetching on
        // every focus produces noise and cost without new information.
        refetchOnWindowFocus: false,
        staleTime: 15_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError)
            return error.retryable && failureCount < 2;
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const router = useRouter();

  const onUnauthorized = useCallback(() => {
    clearSession();
    router.replace("/login");
  }, [router]);

  const [client] = useState(() => createQueryClient(onUnauthorized));

  return (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>{children}</WorkspaceProvider>
    </QueryClientProvider>
  );
}

function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState<string | null>(null);
  const [environment, setEnvironmentState] = useState<string>("production");
  const [range, setRangeState] = useState<TimeRange>("24h");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const stored = window.localStorage.getItem(RANGE_STORAGE_KEY);
    if (stored && (TIME_RANGES as readonly string[]).includes(stored)) {
      setRangeState(stored as TimeRange);
    }
  }, []);

  // Signing in or out happens without remounting this provider, so it has to
  // be told rather than discovering it on the next full page load.
  useEffect(
    () =>
      onSessionChange(() => {
        setProjects([]);
        setProjectIdState(null);
        setReloadToken((token) => token + 1);
      }),
    [],
  );

  useEffect(() => {
    let cancelled = false;
    if (!getToken()) {
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    setLoading(true);
    api
      .projects()
      .then((result) => {
        if (cancelled) return;
        setProjects(result);
        setError(null);
        const remembered = window.localStorage.getItem(PROJECT_STORAGE_KEY);
        const chosen =
          result.find((project) => project.id === remembered) ??
          result[0] ??
          null;
        setProjectIdState(chosen?.id ?? null);
        if (chosen) setEnvironmentState(rememberedEnvironment(chosen));
      })
      .catch((cause) => {
        if (!cancelled) setError(cause);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  const setProjectId = useCallback(
    (id: string) => {
      setProjectIdState(id);
      window.localStorage.setItem(PROJECT_STORAGE_KEY, id);
      const project = projects.find((item) => item.id === id);
      if (project) setEnvironmentState(rememberedEnvironment(project));
    },
    [projects],
  );

  const setEnvironment = useCallback(
    (name: string) => {
      setEnvironmentState(name);
      if (projectId)
        window.localStorage.setItem(environmentStorageKey(projectId), name);
    },
    [projectId],
  );

  const setRange = useCallback((next: TimeRange) => {
    setRangeState(next);
    window.localStorage.setItem(RANGE_STORAGE_KEY, next);
  }, []);

  const value = useMemo<Workspace>(
    () => ({
      projects,
      project: projects.find((item) => item.id === projectId) ?? null,
      projectId,
      environment,
      range,
      loading,
      error,
      setProjectId,
      setEnvironment,
      setRange,
      reload: () => setReloadToken((token) => token + 1),
    }),
    [
      projects,
      projectId,
      environment,
      range,
      loading,
      error,
      setProjectId,
      setEnvironment,
      setRange,
    ],
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

/**
 * The environment to show for a project: the one last chosen for it, otherwise
 * production, otherwise whatever exists. Falling back to production by default
 * is deliberate — an engineer opening a dashboard almost always means the
 * environment users are actually in.
 */
function rememberedEnvironment(project: Project): string {
  const remembered = window.localStorage.getItem(
    environmentStorageKey(project.id),
  );
  if (
    remembered &&
    project.environments.some((item) => item.name === remembered)
  ) {
    return remembered;
  }
  const production = project.environments.find((item) => item.is_production);
  return production?.name ?? project.environments[0]?.name ?? "production";
}
