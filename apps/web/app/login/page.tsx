"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, TextInput } from "@/components/ui";

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="auth-page" />}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.login(email, password);
      // Only same-origin relative paths are honoured, so `?next=` cannot be
      // used to bounce a freshly-authenticated user to an attacker's site.
      const next = params.get("next");
      const safeNext =
        next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
      router.replace(safeNext);
    } catch (cause) {
      // The message is deliberately identical for an unknown email and a wrong
      // password: distinguishing them turns the login form into an account
      // enumeration oracle.
      setError(
        cause instanceof ApiError && cause.status === 429
          ? "Too many attempts. Wait a moment and try again."
          : "Sign in failed. Check your email and password.",
      );
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit} noValidate>
        <h1 style={{ margin: "0 0 0.25rem", fontSize: "1.125rem" }}>
          <span aria-hidden="true" style={{ color: "var(--accent)" }}>
            ◈{" "}
          </span>
          Sign in
        </h1>
        <p
          style={{
            margin: "0 0 1.25rem",
            color: "var(--text-muted)",
            fontSize: "0.8125rem",
          }}
        >
          AI Observability Platform
        </p>

        <div style={{ display: "grid", gap: "0.75rem" }}>
          <TextInput
            id="email"
            label="Email"
            type="email"
            value={email}
            onChange={setEmail}
            autoComplete="username"
            required
          />
          <TextInput
            id="password"
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete="current-password"
            required
          />
        </div>

        {error && (
          <p
            role="alert"
            style={{
              marginTop: "0.75rem",
              marginBottom: 0,
              padding: "0.5rem 0.625rem",
              borderRadius: "var(--radius)",
              background: "var(--error-subtle)",
              color: "var(--error)",
              fontSize: "0.8125rem",
            }}
          >
            <span aria-hidden="true">✕ </span>
            {error}
          </p>
        )}

        <Button
          type="submit"
          variant="primary"
          disabled={submitting || !email || !password}
          style={{ marginTop: "1rem", width: "100%", padding: "0.5rem" }}
        >
          {submitting ? "Signing in…" : "Sign in"}
        </Button>

        <p
          style={{
            marginTop: "1rem",
            marginBottom: 0,
            fontSize: "0.75rem",
            color: "var(--text-faint)",
          }}
        >
          Seeded development credentials are printed by <code>make seed</code>.
          They are not valid in any deployment that ran its own bootstrap.
        </p>
      </form>
    </div>
  );
}
