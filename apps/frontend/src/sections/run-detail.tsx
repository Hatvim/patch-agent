import { Button, type NavItem, Row, Shell, Tabs, Textarea } from "@patch/ui";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  type AgentRunEventRead,
  type AgentRunStatus,
  type AgentRunWebSocketFrame,
  ApiClientError,
  apiEndpoints,
  type DiffFileRead,
  type EventType,
  type FeedbackCreate,
  patchApi,
  type ReviewFinding,
  type ReviewRunRead,
  type TerminalWebSocketFrame,
} from "../lib/api";
import {
  useAgentRun,
  useAgentRunDiff,
  useAgentRunEvents,
  useAgentRunPullRequest,
  useAgentRunReview,
} from "../lib/queries";
import { useAgentRunStream } from "../lib/ws";

type TabId = "stream" | "diff" | "pr" | "review";

type TimelineEvent = {
  key: string;
  sequence: number;
  event_type: EventType;
  payload: Record<string, unknown>;
};

const terminalStatuses: AgentRunStatus[] = ["succeeded", "failed", "cancelled"];

export function RunDetail({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const agentRunQuery = useAgentRun(runId);
  const eventsQuery = useAgentRunEvents(runId, 100);
  const diffQuery = useAgentRunDiff(runId);
  const prQuery = useAgentRunPullRequest(runId);
  const hasReviewerRunId = Boolean(agentRunQuery.data?.reviewer_run_id);
  const reviewQuery = useAgentRunReview(runId, hasReviewerRunId);

  const handleTerminal = useCallback(
    (_: TerminalWebSocketFrame) => {
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRun(runId)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunEvents(runId, 100)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunPullRequest(runId)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunDiff(runId)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRuns] });
    },
    [queryClient, runId],
  );
  const stream = useAgentRunStream(runId, { onTerminal: handleTerminal });

  // Status priority: any terminal status wins, otherwise prefer whichever
  // source has the latest non-queued reading. Polling (`useAgentRun`) is the
  // safety net — guarantees we converge within 2s even if a WS status_change
  // frame was missed.
  const status = pickStatus(agentRunQuery.data?.status, stream.status);
  const prUrl = stream.prUrl ?? agentRunQuery.data?.pull_request?.url ?? null;
  const branch = agentRunQuery.data?.branch_name ?? null;
  const isTerminal = terminalStatuses.includes(status);
  const canStop = status === "queued" || status === "running";

  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);

  const handleStop = async () => {
    setStopping(true);
    setStopError(null);
    try {
      await patchApi.cancelAgentRun(runId);
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRun(runId)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunEvents(runId, 100)] });
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRuns] });
    } catch (err) {
      setStopError(err instanceof ApiClientError ? err.message : "failed to stop");
    } finally {
      setStopping(false);
    }
  };

  const [activeTab, setActiveTab] = useState<TabId>("stream");
  const [autoSwitched, setAutoSwitched] = useState(false);
  useEffect(() => {
    if (!autoSwitched && isTerminal && (diffQuery.data?.length ?? 0) > 0) {
      setActiveTab("diff");
      setAutoSwitched(true);
    }
  }, [autoSwitched, isTerminal, diffQuery.data]);

  // Safety net: when the live stream reports a terminal status, force-refresh
  // server-side queries even if the terminal frame itself never arrived
  // (e.g. WS closed before the final frame was delivered).
  useEffect(() => {
    if (!stream.status) return;
    if (!terminalStatuses.includes(stream.status)) return;
    void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRun(runId)] });
    void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunPullRequest(runId)] });
    void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRunDiff(runId)] });
    void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRuns] });
  }, [stream.status, queryClient, runId]);

  const timeline = useMemo(
    () => mergeTimeline(eventsQuery.data ?? [], stream.events),
    [eventsQuery.data, stream.events],
  );

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
      title={`run ${runId.slice(0, 8)}`}
      nav={nav}
      activeNav="runs"
      logoHref="/"
      onLogoClick={() => void navigate({ to: "/" })}
    >
      {agentRunQuery.data?.instruction && (
        <div className="mb-4 border-l-2 border-[var(--patch-accent)] bg-[var(--patch-surface)] px-3 py-2 text-sm">
          <div className="mb-1 text-xs uppercase tracking-wide text-[var(--patch-dim)]">task</div>
          <p className="whitespace-pre-wrap break-words">{agentRunQuery.data.instruction}</p>
        </div>
      )}
      <div className="mb-4 grid gap-1 text-sm">
        <div className="flex items-center gap-3">
          <StatusPill status={status} />
          {canStop && (
            <button
              type="button"
              onClick={() => void handleStop()}
              disabled={stopping}
              className="text-[var(--patch-error)] hover:underline disabled:opacity-40"
            >
              {stopping ? "[ stopping... ]" : "[ stop ]"}
            </button>
          )}
          {stopError && <span className="text-xs text-[var(--patch-error)]">{stopError}</span>}
        </div>
        <div>
          <span className="text-[var(--patch-dim)]">branch </span>
          <span>{branch ?? "—"}</span>
        </div>
        <div>
          <span className="text-[var(--patch-dim)]">pr </span>
          {prUrl ? (
            <a href={prUrl} target="_blank" rel="noreferrer" className="text-[var(--patch-accent)] hover:underline">
              {prUrl} →
            </a>
          ) : (
            <span className="text-[var(--patch-dim)]">—</span>
          )}
        </div>
      </div>

      <Tabs
        tabs={[
          { id: "stream", label: "stream" },
          { id: "diff", label: "diff" },
          { id: "pr", label: "pr" },
          ...(hasReviewerRunId ? [{ id: "review", label: "review" }] : []),
        ]}
        active={activeTab}
        onChange={(id) => setActiveTab(id as TabId)}
      />

      {activeTab === "stream" && (
        <StreamTab timeline={timeline} connection={stream.connectionStatus} error={stream.error} />
      )}
      {activeTab === "diff" && (
        <DiffTab
          runId={runId}
          diff={diffQuery.data ?? []}
          isLoading={diffQuery.isLoading}
          isError={diffQuery.isError}
        />
      )}
      {activeTab === "pr" && <PrTab pr={prQuery.data ?? agentRunQuery.data?.pull_request ?? null} />}
      {activeTab === "review" && (
        <ReviewTab
          review={reviewQuery.data ?? null}
          isLoading={reviewQuery.isLoading}
          isError={reviewQuery.isError}
          onNavigateToFix={(fixRunId) => void navigate({ to: "/run/$id", params: { id: fixRunId } })}
        />
      )}
    </Shell>
  );
}

function StreamTab({
  timeline,
  connection,
  error,
}: {
  timeline: TimelineEvent[];
  connection: string;
  error: string | null;
}) {
  return (
    <div>
      <div className="max-h-[60vh] overflow-y-auto border border-[var(--patch-border)] bg-[var(--patch-surface)] p-3">
        {timeline.length === 0 ? (
          <p className="text-xs text-[var(--patch-dim)]">~ waiting for events ~</p>
        ) : (
          <div className="space-y-3">
            {timeline.map((event) => (
              <EventBlock key={event.key} event={event} />
            ))}
          </div>
        )}
      </div>
      <p className="mt-2 text-xs text-[var(--patch-dim)]">
        ~ stream: <span className={connection === "connected" ? "text-[var(--patch-accent)]" : ""}>{connection}</span>
        {error && <span className="ml-3 text-[var(--patch-error)]">{error}</span>}
      </p>
    </div>
  );
}

function EventBlock({ event }: { event: TimelineEvent }) {
  const [expanded, setExpanded] = useState(false);
  const payload = event.payload ?? {};

  if (event.event_type === "tool_call") {
    const toolName = getPayloadText(payload, "tool_name") ?? "tool";
    const args = (payload.tool_input ?? {}) as Record<string, unknown>;
    const summary = summarizeToolArgs(toolName, args);
    return (
      <div className="text-xs">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-baseline gap-2 text-left"
        >
          <span className="text-[var(--patch-dim)]">#{event.sequence}</span>
          <span className="text-[var(--patch-accent)]">▶ {toolName}</span>
          <span className="truncate text-[var(--patch-fg)]">{summary}</span>
        </button>
        {expanded && (
          <pre className="mt-1 whitespace-pre-wrap break-words pl-4 text-[var(--patch-dim)]">{formatUnknown(args)}</pre>
        )}
      </div>
    );
  }

  if (event.event_type === "tool_result") {
    const toolName = getPayloadText(payload, "tool_name") ?? "tool";
    const status = getPayloadText(payload, "status") ?? "success";
    const duration = typeof payload.duration_ms === "number" ? `${payload.duration_ms}ms` : null;
    const errorMessage = getPayloadText(payload, "error_message");
    const output = payload.tool_output as Record<string, unknown> | undefined;
    const isError = status === "error";
    const summary = summarizeToolOutput(output, errorMessage);
    return (
      <div className="text-xs">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-baseline gap-2 text-left"
        >
          <span className="text-[var(--patch-dim)]">#{event.sequence}</span>
          <span className={isError ? "text-[var(--patch-error)]" : "text-[var(--patch-accent)]"}>
            {isError ? "✗" : "✓"} {toolName}
          </span>
          {duration && <span className="text-[var(--patch-dim)]">{duration}</span>}
          <span className="truncate text-[var(--patch-fg)]">{summary}</span>
        </button>
        {expanded && (
          <pre className="mt-1 whitespace-pre-wrap break-words pl-4 text-[var(--patch-dim)]">
            {formatUnknown(output ?? payload)}
          </pre>
        )}
      </div>
    );
  }

  if (event.event_type === "message") {
    const text = getPayloadText(payload, "content") ?? getPayloadText(payload, "text") ?? "";
    return (
      <div className="text-xs">
        <div className="text-[var(--patch-dim)]">#{event.sequence} message</div>
        <pre className="mt-1 whitespace-pre-wrap break-words pl-4">{text}</pre>
      </div>
    );
  }

  if (event.event_type === "status_change") {
    const newStatus = getPayloadText(payload, "new_status") ?? "";
    return (
      <div className="text-xs text-[var(--patch-dim)]">
        #{event.sequence} → <span className={statusClass(newStatus)}>{newStatus}</span>
      </div>
    );
  }

  if (event.event_type === "error") {
    const message = getPayloadText(payload, "message") ?? formatUnknown(payload) ?? "";
    return (
      <div className="text-xs">
        <div className="text-[var(--patch-error)]">#{event.sequence} error</div>
        <pre className="mt-1 whitespace-pre-wrap break-words pl-4 text-[var(--patch-error)]">{message}</pre>
      </div>
    );
  }

  // summary / plan / other
  const detail = formatUnknown(payload);
  return (
    <div className="text-xs">
      <div className="text-[var(--patch-accent)]">
        <span className="text-[var(--patch-dim)]">#{event.sequence} </span>
        {event.event_type}
      </div>
      {detail && <pre className="mt-1 whitespace-pre-wrap break-words pl-4 text-[var(--patch-dim)]">{detail}</pre>}
    </div>
  );
}

function summarizeToolArgs(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "exec_command") return String(args.command ?? "");
  if (toolName === "write_file" || toolName === "patch_file") return String(args.path ?? "");
  if (toolName === "submit_pull_request") return String(args.title ?? "");
  if (toolName === "write_stdin") return `pid=${String(args.pid ?? "")}`;
  return Object.keys(args).slice(0, 3).join(", ");
}

function summarizeToolOutput(output: Record<string, unknown> | undefined, errorMessage: string | null): string {
  if (errorMessage) return errorMessage;
  if (!output) return "";
  if (typeof output.exit_code === "number") return `exit ${output.exit_code}`;
  if (typeof output.bytes_written === "number") return `${output.bytes_written} bytes`;
  if (typeof output.url === "string") return output.url;
  if (output.ok === true) return "ok";
  if (output.ok === false) return "failed";
  return "";
}

function DiffTab({
  runId,
  diff,
  isLoading,
  isError,
}: {
  runId: string;
  diff: DiffFileRead[];
  isLoading: boolean;
  isError: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activePath, setActivePath] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeFile = diff.find((f) => f.file_path === activePath) ?? diff[0] ?? null;
  const rows = useMemo(() => parseUnifiedDiff(activeFile?.patch), [activeFile?.patch]);

  const handleFeedback = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const payload: FeedbackCreate = { instruction: instruction.trim() };
    if (!payload.instruction) {
      setError("write a follow-up instruction");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const child = await patchApi.submitFeedback(runId, payload);
      void queryClient.invalidateQueries({ queryKey: ["patch", apiEndpoints.agentRuns] });
      void navigate({ to: "/run/$id", params: { id: child.id } });
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (isLoading) return <p className="text-xs text-[var(--patch-dim)]">loading diff...</p>;
  if (isError) return <p className="text-xs text-[var(--patch-error)]">diff unavailable.</p>;
  if (diff.length === 0) return <p className="text-xs text-[var(--patch-dim)]">no files changed yet.</p>;

  return (
    <div className="grid gap-3 lg:grid-cols-[260px_minmax(0,1fr)]">
      <div className="border border-[var(--patch-border)]">
        {diff.map((file) => {
          const isActive = activeFile?.file_path === file.file_path;
          return (
            <button
              key={file.file_path}
              type="button"
              onClick={() => setActivePath(file.file_path)}
              className={`block w-full border-b border-[var(--patch-border)] px-2 py-1 text-left text-xs last:border-b-0 ${
                isActive
                  ? "bg-[var(--patch-surface)] text-[var(--patch-accent)]"
                  : "text-[var(--patch-fg)] hover:bg-[var(--patch-surface)]"
              }`}
            >
              <div className="truncate">{file.file_path}</div>
              <div className="text-[var(--patch-dim)]">
                <span className="text-[var(--patch-accent)]">+{file.additions}</span>{" "}
                <span className="text-[var(--patch-error)]">-{file.deletions}</span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="border border-[var(--patch-border)]">
        <div className="border-b border-[var(--patch-border)] px-2 py-1 text-xs text-[var(--patch-dim)]">
          {activeFile?.file_path ?? "—"}
        </div>
        <div className="max-h-[60vh] overflow-auto bg-[var(--patch-bg)]">
          {activeFile?.patch ? (
            <pre className="px-2 py-1 text-xs leading-5">
              {rows.map((row) => (
                <div key={row.id} className={rowClass(row.tone)}>
                  {row.text}
                </div>
              ))}
            </pre>
          ) : (
            <p className="p-3 text-xs text-[var(--patch-dim)]">no textual patch (binary, rename, or too large).</p>
          )}
        </div>
      </div>

      <form onSubmit={handleFeedback} className="lg:col-span-2 mt-2">
        <div className="block text-xs text-[var(--patch-dim)]">
          request changes
          <Textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="describe the follow-up changes..."
            rows={3}
          />
        </div>
        <div className="mt-2 flex items-center gap-3">
          <Button type="submit" disabled={submitting}>
            {submitting ? "..." : "[ request changes ]"}
          </Button>
          {error && <span className="text-xs text-[var(--patch-error)]">{error}</span>}
        </div>
      </form>
    </div>
  );
}

function PrTab({
  pr,
}: {
  pr: {
    title: string;
    state: string;
    head_branch: string;
    base_branch: string;
    url: string;
    body: string;
    github_pr_number: number;
  } | null;
}) {
  if (!pr) {
    return <p className="text-xs text-[var(--patch-dim)]">no pr yet.</p>;
  }

  return (
    <div className="space-y-3 text-sm">
      <Row right={<span>#{pr.github_pr_number}</span>}>{pr.title}</Row>
      <div className="text-xs text-[var(--patch-dim)]">
        state <span className={prStateClass(pr.state)}>{pr.state}</span> · branch {pr.head_branch} → {pr.base_branch}
      </div>
      <a
        href={pr.url}
        target="_blank"
        rel="noreferrer"
        className="inline-block border border-[var(--patch-border-strong)] px-3 py-1 text-sm hover:border-[var(--patch-fg)] hover:text-[var(--patch-accent)]"
      >
        [ view on github → ]
      </a>
      {pr.body && (
        <pre className="whitespace-pre-wrap border border-[var(--patch-border)] bg-[var(--patch-surface)] p-3 text-xs leading-5 text-[var(--patch-dim)]">
          {pr.body}
        </pre>
      )}
    </div>
  );
}

function ReviewTab({
  review,
  isLoading,
  isError,
  onNavigateToFix,
}: {
  review: ReviewRunRead | null;
  isLoading: boolean;
  isError: boolean;
  onNavigateToFix: (fixRunId: string) => void;
}) {
  if (isLoading) return <p className="text-xs text-[var(--patch-dim)]">running review...</p>;
  if (isError) return <p className="text-xs text-[var(--patch-error)]">review unavailable.</p>;
  if (!review) return <p className="text-xs text-[var(--patch-dim)]">no review yet.</p>;

  const { findings, status, fix_run_id } = review;
  const isReviewRunning = status === "queued" || status === "running";

  const bySeverity = (s: string) => findings.filter((f) => f.severity === s);
  const critical = bySeverity("critical");
  const high = bySeverity("high");
  const medium = bySeverity("medium");
  const low = bySeverity("low");

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 text-xs">
        <span className="text-[var(--patch-dim)]">auto-review</span>
        <span
          className={
            status === "succeeded"
              ? "text-[var(--patch-accent)]"
              : status === "failed"
                ? "text-[var(--patch-error)]"
                : "text-[var(--patch-warn)]"
          }
        >
          {status}
        </span>
        {isReviewRunning && <span className="text-[var(--patch-dim)]">~ analysing diff...</span>}
      </div>

      {findings.length === 0 && !isReviewRunning && (
        <p className="text-xs text-[var(--patch-accent)]">~ no issues found. PR looks good.</p>
      )}

      {(
        [
          ["critical", critical],
          ["high", high],
          ["medium", medium],
          ["low", low],
        ] as [string, ReviewFinding[]][]
      )
        .filter(([, items]) => items.length > 0)
        .map(([severity, items]) => (
          <div key={severity}>
            <div className={`mb-2 text-xs font-semibold uppercase tracking-wide ${severityClass(severity)}`}>
              {severity} ({items.length})
            </div>
            <div className="space-y-2">
              {items.map((finding) => (
                <FindingCard
                  key={`${severity}-${finding.file_path}-${finding.issue.slice(0, 48) || finding.suggestion.slice(0, 48)}`}
                  finding={finding}
                />
              ))}
            </div>
          </div>
        ))}

      {fix_run_id && (
        <div className="border border-[var(--patch-border)] bg-[var(--patch-surface)] p-3 text-xs">
          <div className="mb-1 text-[var(--patch-warn)]">auto-fix triggered</div>
          <button
            type="button"
            onClick={() => onNavigateToFix(fix_run_id)}
            className="text-[var(--patch-accent)] hover:underline"
          >
            [ view fix run → ]
          </button>
        </div>
      )}
    </div>
  );
}

function FindingCard({ finding }: { finding: ReviewFinding }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border border-[var(--patch-border)] bg-[var(--patch-surface)] text-xs">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-baseline gap-2 px-3 py-2 text-left"
      >
        <span className={`shrink-0 font-semibold ${severityClass(finding.severity)}`}>
          [{finding.severity.toUpperCase()}]
        </span>
        <span className="truncate text-[var(--patch-dim)]">{finding.file_path}</span>
        <span className="ml-auto shrink-0 text-[var(--patch-dim)]">{expanded ? "▲" : "▼"}</span>
      </button>
      <div className="border-t border-[var(--patch-border)] px-3 py-2">
        <p>{finding.issue}</p>
        {expanded && (
          <div className="mt-2 space-y-1">
            <div className="text-[var(--patch-dim)]">
              category <span className="text-[var(--patch-fg)]">{finding.category}</span>
            </div>
            <div className="text-[var(--patch-dim)]">suggestion</div>
            <p className="whitespace-pre-wrap pl-2 text-[var(--patch-accent)]">{finding.suggestion}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function severityClass(severity: string) {
  if (severity === "critical") return "text-[var(--patch-error)]";
  if (severity === "high") return "text-[var(--patch-warn)]";
  if (severity === "medium") return "text-[var(--patch-fg)]";
  return "text-[var(--patch-dim)]";
}

const STATUS_RANK: Record<string, number> = {
  queued: 0,
  running: 1,
  succeeded: 2,
  failed: 2,
  cancelled: 2,
};

function pickStatus(db: AgentRunStatus | undefined, live: AgentRunStatus | null): AgentRunStatus {
  const candidates: AgentRunStatus[] = [];
  if (db) candidates.push(db);
  if (live) candidates.push(live);
  if (candidates.length === 0) return "queued";
  // Take the most "advanced" of the two — never let a stale queued from a
  // cached fetch override a live running/terminal update, or vice versa.
  return candidates.reduce((a, b) => ((STATUS_RANK[a] ?? 0) >= (STATUS_RANK[b] ?? 0) ? a : b));
}

function statusClass(status: string) {
  if (status === "succeeded") return "text-[var(--patch-accent)]";
  if (status === "failed" || status === "cancelled") return "text-[var(--patch-error)]";
  if (status === "running") return "text-[var(--patch-warn)]";
  return "text-[var(--patch-dim)]";
}

function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-block border border-[var(--patch-border-strong)] px-2 py-0.5 text-xs uppercase tracking-wide ${statusClass(status)}`}
    >
      {status}
    </span>
  );
}

function prStateClass(state: string) {
  if (state === "merged") return "text-[var(--patch-accent)]";
  if (state === "closed") return "text-[var(--patch-error)]";
  if (state === "draft") return "text-[var(--patch-dim)]";
  return "text-[var(--patch-warn)]";
}

function mergeTimeline(history: AgentRunEventRead[], live: AgentRunWebSocketFrame[]): TimelineEvent[] {
  const map = new Map<number, TimelineEvent>();
  for (const event of history) {
    map.set(event.sequence, {
      key: `h-${event.sequence}`,
      sequence: event.sequence,
      event_type: event.event_type,
      payload: event.payload,
    });
  }
  for (const event of live) {
    map.set(event.sequence, {
      key: `l-${event.sequence}`,
      sequence: event.sequence,
      event_type: event.type,
      payload: event.payload,
    });
  }
  return Array.from(map.values()).sort((a, b) => a.sequence - b.sequence);
}

function getPayloadText(payload: Record<string, unknown> | undefined, key: string) {
  const value = payload?.[key];
  return typeof value === "string" ? value : null;
}

function formatUnknown(value: unknown) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

type DiffRow = { id: string; text: string; tone: "context" | "added" | "removed" | "hunk" };

function parseUnifiedDiff(patch: string | null | undefined): DiffRow[] {
  if (!patch) return [];
  return patch.split("\n").map((line, idx) => {
    if (line.startsWith("@@")) return { id: `${idx}-h`, text: line, tone: "hunk" };
    if (line.startsWith("+")) return { id: `${idx}-a`, text: line, tone: "added" };
    if (line.startsWith("-")) return { id: `${idx}-r`, text: line, tone: "removed" };
    return { id: `${idx}-c`, text: line, tone: "context" };
  });
}

function rowClass(tone: DiffRow["tone"]) {
  if (tone === "added") return "text-[var(--patch-accent)]";
  if (tone === "removed") return "text-[var(--patch-error)]";
  if (tone === "hunk") return "text-[var(--patch-warn)]";
  return "text-[var(--patch-dim)]";
}
