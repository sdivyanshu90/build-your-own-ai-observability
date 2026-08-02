"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import {
  Card,
  Column,
  DataTable,
  ErrorState,
  Loading,
  Tag,
} from "@/components/ui";

interface MemberRow {
  user: { id: string; email: string; display_name: string };
  role: string;
}

/** What each role may do, mirroring the server-side matrix. Shown so a person
 *  granting a role can see its consequences without reading the source. */
const ROLE_CAPABILITIES: Record<string, string> = {
  owner:
    "Everything, including deleting the organization and transferring ownership.",
  administrator:
    "Manage members, keys, retention, price books and projects. Cannot delete the organization.",
  developer:
    "Read all telemetry, write registries, create and revoke their own API keys.",
  analyst:
    "Read telemetry and registries, create exports. No configuration changes.",
  viewer: "Read telemetry and registries only.",
};

export default function MembersPage() {
  const members = useQuery({
    queryKey: ["members"],
    queryFn: () => api.members(),
  });

  const columns: Column<MemberRow>[] = [
    {
      key: "name",
      header: "Member",
      render: (row) => row.user.display_name || row.user.email,
    },
    { key: "email", header: "Email", render: (row) => row.user.email },
    { key: "role", header: "Role", render: (row) => <Tag>{row.role}</Tag> },
    {
      key: "capabilities",
      header: "Can do",
      render: (row) => (
        <span style={{ color: "var(--text-muted)" }}>
          {ROLE_CAPABILITIES[row.role] ?? "Custom role."}
        </span>
      ),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Members & roles</h1>
          <p>
            Roles are enforced by the API on every request, not by hiding
            buttons. A member who crafts a request they are not permitted to
            make receives a 403 and an audit entry.
          </p>
        </div>
      </header>

      <Card title="Members" padded={false}>
        {members.isLoading && <Loading />}
        {members.isError && (
          <ErrorState error={members.error} onRetry={() => members.refetch()} />
        )}
        {members.data && (
          <DataTable
            columns={columns}
            rows={members.data as MemberRow[]}
            rowKey={(row) => row.user.id}
            caption="Organization members and their roles"
          />
        )}
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Role reference">
          <dl
            style={{
              margin: 0,
              display: "grid",
              gap: "0.5rem",
              fontSize: "0.8125rem",
            }}
          >
            {Object.entries(ROLE_CAPABILITIES).map(([role, description]) => (
              <div key={role}>
                <dt style={{ fontWeight: 600 }}>{role}</dt>
                <dd style={{ margin: 0, color: "var(--text-muted)" }}>
                  {description}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      </div>
    </>
  );
}
