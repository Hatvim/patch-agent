import { type NavItem, Row, Shell } from "@patch/ui";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { type MouseEvent, useState } from "react";
import {
  type AgentRunListItemRead,
  ApiClientError,
  apiEndpoints,
  type GithubRepoSummary,
  githubOauthStartUrl,
  patchApi,
} from "../lib/api";
import { repositoryLabel, useAgentRuns, useGithubRepositories, useRepositories } from "../lib/queries";
import { formatAge, statusClass } from "../lib/run-format";

export function RunsList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const runsQuery = useAgentRuns(50);

  const handleLogout = () => {
    void patchApi.logout().catch(() => {});
    queryClient.clear();
    void navigate({ to: "/login" });
  };

  const nav: NavItem[] = [
    { id: "runs", label: "runs", onClick: () => void navigate({ to: "/" }) },
    { id: "logout", label: "logout", onClick: () => void handleLogout() },
  ];

  return (
    <Shell
      title="patch"
      nav={nav}
      activeNav="runs"
      logoHref="/"
      onLogoClick={() => void navigate({ to: "/" })}
      right={<span>runs: {runsQuery.data?.length ?? 0}</span>}
    >
      <Section title="repos">
        <RepoStrip />
      </Section>

      <Section title="recent runs">
        <RunsTable runs={runsQuery.data ?? []} isLoading={runsQuery.isLoading} />
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

function RepoStrip() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const reposQuery = useRepositories();
  const repos = reposQuery.data ?? [];
  const githubReposQuery = useGithubRepositories(true);
  const [connectingFullName, setConnectingFullName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showBrowser, setShowBrowser] = useState(true);

  const connectedFullNames = new Set(repos.map((r) => `${r.github_owner}/${r.github_repo}`));

  const handleConnect = async (repo: GithubRepoSummary) => {
    setConnectingFullName(repo.full_name);
    setError(null);
    try {
      await patchApi.createRepository({ owner: repo.owner, name: repo.name });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.repositories] });
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : "failed to connect repo");
    } finally {
      setConnectingFullName(null);
    }
  };

  const handleDelete = async (event: MouseEvent, id: string) => {
    event.stopPropagation();
    await patchApi.deleteRepository(id).catch(() => {});
    void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.repositories] });
  };

  const githubAuthError =
    githubReposQuery.error instanceof ApiClientError &&
    (githubReposQuery.error.status === 401 || githubReposQuery.error.status === 404);

  const githubRepos = githubReposQuery.data ?? [];
  const filteredGithubRepos = githubRepos.filter((r) => !connectedFullNames.has(r.full_name));

  return (
    <div>
      {repos.length === 0 && !reposQuery.isLoading && (
        <p className="mb-2 text-xs text-[var(--patch-dim)]">no repos connected yet.</p>
      )}
      {repos.map((repo) => (
        <Row
          key={repo.id}
          onClick={() => void navigate({ to: "/repo/$id", params: { id: repo.id } })}
          right={
            <button
              type="button"
              onClick={(event) => void handleDelete(event, repo.id)}
              className="text-[var(--patch-error)] hover:underline"
            >
              [ disconnect ]
            </button>
          }
        >
          {repositoryLabel(repo)} <span className="text-[var(--patch-dim)]">· {repo.default_branch}</span>
        </Row>
      ))}

      <div className="mt-3">
        {githubAuthError ? (
          <a
            href={githubOauthStartUrl()}
            className="inline-block border border-[var(--patch-border-strong)] px-3 py-1 text-sm hover:border-[var(--patch-fg)] hover:text-[var(--patch-accent)]"
          >
            [ + connect github ]
          </a>
        ) : (
          <div>
            <div className="mb-2 flex items-center gap-3 text-xs">
              <span className="text-[var(--patch-dim)]">── your github repos ──</span>
              <button
                type="button"
                onClick={() => setShowBrowser((v) => !v)}
                className="text-[var(--patch-dim)] hover:text-[var(--patch-fg)]"
              >
                [ {showBrowser ? "hide" : "show"} ]
              </button>
              <button
                type="button"
                onClick={() => void githubReposQuery.refetch()}
                className="text-[var(--patch-dim)] hover:text-[var(--patch-fg)]"
              >
                [ refresh ]
              </button>
            </div>
            {showBrowser &&
              (githubReposQuery.isLoading ? (
                <p className="text-xs text-[var(--patch-dim)]">loading github repos...</p>
              ) : filteredGithubRepos.length === 0 ? (
                <p className="text-xs text-[var(--patch-dim)]">
                  {githubRepos.length === 0 ? "no github repos found." : "all your github repos are connected."}
                </p>
              ) : (
                <div className="max-h-80 overflow-auto border border-[var(--patch-border)]">
                  {filteredGithubRepos.map((repo) => {
                    const isConnecting = connectingFullName === repo.full_name;
                    return (
                      <Row
                        key={repo.full_name}
                        right={
                          <button
                            type="button"
                            disabled={isConnecting}
                            onClick={() => void handleConnect(repo)}
                            className="text-[var(--patch-accent)] hover:underline disabled:opacity-40"
                          >
                            {isConnecting ? "..." : "[ connect ]"}
                          </button>
                        }
                      >
                        {repo.full_name}{" "}
                        <span className="text-[var(--patch-dim)]">
                          · {repo.default_branch}
                          {repo.private ? " · private" : ""}
                          {repo.language ? ` · ${repo.language}` : ""}
                        </span>
                      </Row>
                    );
                  })}
                </div>
              ))}
            {error && <p className="mt-2 text-xs text-[var(--patch-error)]">{error}</p>}
          </div>
        )}
      </div>
    </div>
  );
}

function RunsTable({ runs, isLoading }: { runs: AgentRunListItemRead[]; isLoading: boolean }) {
  const navigate = useNavigate();

  if (isLoading) {
    return <p className="text-xs text-[var(--patch-dim)]">loading...</p>;
  }
  if (runs.length === 0) {
    return <p className="text-xs text-[var(--patch-dim)]">no runs yet.</p>;
  }

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
            <td className="max-w-[28rem] truncate py-1 pr-3" title={run.instruction ?? ""}>
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
