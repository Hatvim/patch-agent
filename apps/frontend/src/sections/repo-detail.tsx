import { Button, Input, type NavItem, Shell, Textarea } from "@patch/ui";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { type AgentRunListItemRead, ApiClientError, apiEndpoints, patchApi, type RepositoryRead } from "../lib/api";
import { useAgentRuns, useRepositories } from "../lib/queries";
import { formatAge, statusClass } from "../lib/run-format";
import { useAgentRunStream } from "../lib/ws";

export function RepoDetail({ repoId }: { repoId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const reposQuery = useRepositories();
  const repo = reposQuery.data?.find((r) => r.id === repoId) ?? null;
  const runsQuery = useAgentRuns(50, repoId);

  const handleLogout = () => {
    void patchApi.logout().catch(() => {});
    queryClient.clear();
    void navigate({ to: "/login" });
  };

  const nav: NavItem[] = [
    { id: "runs", label: "runs", onClick: () => void navigate({ to: "/" }) },
    { id: "logout", label: "logout", onClick: () => void handleLogout() },
  ];

  if (reposQuery.isLoading) {
    return (
      <Shell title="repo" nav={nav} activeNav="runs" logoHref="/" onLogoClick={() => void navigate({ to: "/" })}>
        <p className="text-xs text-[var(--patch-dim)]">loading...</p>
      </Shell>
    );
  }

  if (!repo) {
    return (
      <Shell title="repo" nav={nav} activeNav="runs" logoHref="/" onLogoClick={() => void navigate({ to: "/" })}>
        <p className="text-xs text-[var(--patch-error)]">repo not found.</p>
        <button
          type="button"
          onClick={() => void navigate({ to: "/" })}
          className="mt-3 text-[var(--patch-dim)] hover:text-[var(--patch-fg)]"
        >
          [ back to runs ]
        </button>
      </Shell>
    );
  }

  const fullName = `${repo.github_owner}/${repo.github_repo}`;
  const githubUrl = `https://github.com/${fullName}`;
  const runs = runsQuery.data ?? [];
  const activeRun = runs.find((r) => r.status === "queued" || r.status === "running") ?? null;

  return (
    <Shell
      title={fullName}
      nav={nav}
      activeNav="runs"
      logoHref="/"
      onLogoClick={() => void navigate({ to: "/" })}
      right={<span>{repo.default_branch}</span>}
    >
      <p className="mb-4 text-xs text-[var(--patch-dim)]">
        branch: {repo.default_branch}
        {repo.language ? ` · ${repo.language}` : ""}{" "}
        <a href={githubUrl} target="_blank" rel="noreferrer" className="text-[var(--patch-accent)] hover:underline">
          · github →
        </a>
      </p>

      <Section title="new task">
        <NewTaskForm repo={repo} />
      </Section>

      {activeRun && (
        <Section title="active run">
          <LivePreview run={activeRun} />
        </Section>
      )}

      <Section title="runs">
        <RepoRunsTable runs={runs} isLoading={runsQuery.isLoading} />
      </Section>
    </Shell>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="mb-2 text-xs uppercase tracking-wider text-[var(--patch-dim)]">── {title} ──</h2>
      {children}
    </section>
  );
}

function NewTaskForm({ repo }: { repo: RepositoryRead }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [branch, setBranch] = useState("");
  const [instruction, setInstruction] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!instruction.trim()) {
      setError("write an instruction");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const run = await patchApi.createTask({
        repository_id: repo.id,
        instruction: instruction.trim(),
        target_branch: branch.trim() || repo.default_branch,
      });
      setInstruction("");
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRuns] });
      void navigate({ to: "/run/$id", params: { id: run.id } });
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : "failed to submit");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-2">
      <div className="flex flex-col text-xs text-[var(--patch-dim)]">
        branch
        <Input
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder={repo.default_branch}
          className="w-48"
        />
      </div>
      <div className="block text-xs text-[var(--patch-dim)]">
        instruction
        <Textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="describe what to change..."
          rows={4}
          className="font-mono"
        />
      </div>
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={submitting}>
          {submitting ? "..." : "[ run ]"}
        </Button>
        {error && <span className="text-xs text-[var(--patch-error)]">{error}</span>}
      </div>
    </form>
  );
}

function LivePreview({ run }: { run: AgentRunListItemRead }) {
  const navigate = useNavigate();
  const stream = useAgentRunStream(run.id);
  const status = stream.status ?? run.status;
  const lastFive = stream.events.slice(-5);

  return (
    <div className="border border-[var(--patch-border)] bg-[var(--patch-surface)] p-3">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span>
          <span className="text-[var(--patch-dim)]">#{run.id.slice(0, 8)} </span>
          <span className={statusClass(status)}>{status}</span>
          <span className="text-[var(--patch-dim)]"> · {run.branch_name ?? "—"}</span>
        </span>
        <button
          type="button"
          onClick={() => void navigate({ to: "/run/$id", params: { id: run.id } })}
          className="text-[var(--patch-accent)] hover:underline"
        >
          [ open run → ]
        </button>
      </div>
      {lastFive.length === 0 ? (
        <p className="text-xs text-[var(--patch-dim)]">~ waiting for events ~</p>
      ) : (
        <div className="space-y-1 text-xs">
          {lastFive.map((event) => {
            const toolName = typeof event.payload?.tool_name === "string" ? (event.payload.tool_name as string) : null;
            return (
              <div key={event.sequence} className="flex gap-2">
                <span className="text-[var(--patch-dim)]">#{event.sequence}</span>
                <span className="text-[var(--patch-accent)]">{event.type}</span>
                {toolName && <span>{toolName}</span>}
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-2 text-xs text-[var(--patch-dim)]">
        ~ stream:{" "}
        <span className={stream.connectionStatus === "connected" ? "text-[var(--patch-accent)]" : ""}>
          {stream.connectionStatus}
        </span>
      </p>
    </div>
  );
}

function RepoRunsTable({ runs, isLoading }: { runs: AgentRunListItemRead[]; isLoading: boolean }) {
  const navigate = useNavigate();

  if (isLoading) return <p className="text-xs text-[var(--patch-dim)]">loading...</p>;
  if (runs.length === 0) return <p className="text-xs text-[var(--patch-dim)]">no runs yet.</p>;

  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs text-[var(--patch-dim)]">
        <tr>
          <th className="py-1 pr-3 font-normal">id</th>
          <th className="py-1 pr-3 font-normal">task</th>
          <th className="py-1 pr-3 font-normal">status</th>
          <th className="py-1 pr-3 font-normal">branch</th>
          <th className="py-1 pr-3 font-normal">pr</th>
          <th className="py-1 pr-3 font-normal">age</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr
            key={run.id}
            onClick={() => void navigate({ to: "/run/$id", params: { id: run.id } })}
            className="cursor-pointer border-t border-[var(--patch-border)] hover:bg-[var(--patch-surface)] hover:text-[var(--patch-accent)]"
          >
            <td className="py-1 pr-3 font-mono">{run.id.slice(0, 8)}</td>
            <td className="max-w-[22rem] truncate py-1 pr-3" title={run.instruction ?? ""}>
              {run.instruction ?? <span className="text-[var(--patch-dim)]">—</span>}
            </td>
            <td className={`py-1 pr-3 ${statusClass(run.status)}`}>{run.status}</td>
            <td className="py-1 pr-3 text-[var(--patch-dim)]">{run.branch_name ?? "—"}</td>
            <td className="py-1 pr-3 text-[var(--patch-dim)]">
              {run.pull_request ? `#${run.pull_request.github_pr_number}` : "—"}
            </td>
            <td className="py-1 pr-3 text-[var(--patch-dim)]">{formatAge(run.queued_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
