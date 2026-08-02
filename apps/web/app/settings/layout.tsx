"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const SECTIONS = [
  { href: "/settings", label: "Overview" },
  { href: "/settings/api-keys", label: "API keys" },
  { href: "/settings/members", label: "Members & roles" },
  { href: "/settings/retention", label: "Retention" },
  { href: "/settings/price-books", label: "Price books" },
  { href: "/settings/exports", label: "Exports" },
  { href: "/settings/audit", label: "Audit log" },
];

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <>
      <nav aria-label="Settings sections" className="tabs">
        {SECTIONS.map((section) => {
          const active = pathname === section.href;
          return (
            <Link
              key={section.href}
              href={section.href}
              className="tab"
              aria-current={active ? "page" : undefined}
              aria-selected={active}
              role="tab"
            >
              {section.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </>
  );
}
