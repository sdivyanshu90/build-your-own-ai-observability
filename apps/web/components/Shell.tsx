"use client";

/**
 * Application chrome: navigation, workspace pickers, session.
 *
 * The login route renders bare -- showing a project switcher to someone who is
 * not authenticated is both useless and a small information leak.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { useWorkspace } from "@/app/providers";
import { api, clearSession, getToken } from "@/lib/api";
import { TIME_RANGES, type TimeRange } from "@/lib/format";
import { Select } from "./ui";

const NAV: { href: string; label: string; match: (path: string) => boolean }[] =
  [
    { href: "/", label: "Overview", match: (path) => path === "/" },
    {
      href: "/traces",
      label: "Traces",
      match: (path) => path.startsWith("/traces"),
    },
    {
      href: "/latency",
      label: "Latency",
      match: (path) => path.startsWith("/latency"),
    },
    {
      href: "/costs",
      label: "Cost",
      match: (path) => path.startsWith("/costs"),
    },
    {
      href: "/prompts",
      label: "Prompts",
      match: (path) => path.startsWith("/prompts"),
    },
    {
      href: "/models",
      label: "Models",
      match: (path) => path.startsWith("/models"),
    },
    {
      href: "/datasets",
      label: "Datasets",
      match: (path) => path.startsWith("/datasets"),
    },
    {
      href: "/settings",
      label: "Settings",
      match: (path) => path.startsWith("/settings"),
    },
  ];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const workspace = useWorkspace();
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);

  const isLogin = pathname === "/login";

  useEffect(() => {
    const token = getToken();
    setAuthenticated(Boolean(token));
    if (!token && !isLogin) {
      // Preserve where they were going so the round trip through login is not
      // also a loss of context.
      const next = encodeURIComponent(pathname ?? "/");
      router.replace(`/login?next=${next}`);
    }
  }, [pathname, isLogin, router]);

  if (isLogin) {
    return <main id="main">{children}</main>;
  }

  if (authenticated === null) {
    return (
      <main id="main" style={{ padding: "2rem" }}>
        <p role="status">Checking session…</p>
      </main>
    );
  }

  if (!authenticated) {
    return (
      <main id="main" style={{ padding: "2rem" }}>
        <p role="status">Redirecting to sign in…</p>
      </main>
    );
  }

  const project = workspace.project;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <Link href="/" className="app-brand-link">
            <span aria-hidden="true" className="app-logo">
              ◈
            </span>
            AI Observability
          </Link>
        </div>

        <nav aria-label="Primary" className="app-nav">
          {NAV.map((item) => {
            const active = item.match(pathname ?? "");
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`app-nav-link${active ? " is-active" : ""}`}
                aria-current={active ? "page" : undefined}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="app-controls">
          <Select
            label="Project"
            value={workspace.projectId ?? ""}
            onChange={workspace.setProjectId}
            options={workspace.projects.map((item) => ({
              value: item.id,
              label: item.name,
            }))}
            disabled={workspace.projects.length === 0}
          />
          <Select
            label="Environment"
            value={workspace.environment}
            onChange={workspace.setEnvironment}
            options={(project?.environments ?? []).map((env) => ({
              value: env.name,
              label: env.is_production ? `${env.name} (prod)` : env.name,
            }))}
            disabled={!project}
          />
          <Select
            label="Range"
            value={workspace.range}
            onChange={(value) => workspace.setRange(value as TimeRange)}
            options={TIME_RANGES.map((value) => ({
              value,
              label: `Last ${value}`,
            }))}
          />
          <button
            type="button"
            className="link-button"
            onClick={async () => {
              await api.logout().catch(() => clearSession());
              router.replace("/login");
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      <main id="main" className="app-main">
        {children}
      </main>
    </div>
  );
}
