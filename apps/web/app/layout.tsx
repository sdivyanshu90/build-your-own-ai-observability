import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Shell } from "@/components/Shell";
import { Providers } from "./providers";
import "@/styles/globals.css";

export const metadata: Metadata = {
  title: {
    default: "AI Observability",
    template: "%s · AI Observability",
  },
  description:
    "Tracing, retrieval inspection, agent trajectories and cost accounting for AI applications.",
  // Trace ids and prompt content must never leak into a referrer header sent to
  // a third-party host.
  referrer: "strict-origin-when-cross-origin",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0d10" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Keyboard users should not have to tab through the whole navigation
            on every page load. */}
        <a className="skip-link" href="#main">
          Skip to main content
        </a>
        <Providers>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
