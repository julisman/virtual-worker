// Wiki — shared building blocks used by both the managed wiki page and the
// linked-repository detail: the cross-cutting types, the two class strings,
// small presentational atoms (ArgvLine/SourceChip/StatePill/ModalShell), the
// Markdown preview, and a few formatting helpers. Move-only extraction from
// wiki-page.tsx — no behavior change.

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface Wiki {
  id: string;
  name: string;
  description: string;
  createdAt: string;
  ingestedAt: string | null;
  pageCount: number;
  sourceKind?: "managed" | "linked_repository";
  trustTier?: string;
  readOnly?: boolean;
  observedRevision?: string | null;
  availabilityError?: string | null;
  lastError?: string | null;
  lastOperation?: LinkedLastOperation | null;
}

export interface LinkedLastOperation { name: string; status: string; exitCode?: number | null; finishedAt?: string | null }

export interface Page { path: string; title: string; type: string; description: string }
export interface SearchHit { path: string; title: string; score: number; snippet: string }
export interface LinkedOperation {
  name: string;
  description: string;
  argv: string[];
  cwd?: string;
  inputs: Record<string, { type: string; maxLength?: number }>;
  mode: "assistant" | "explicit_user";
  trusted: boolean;
  source: "repo" | "local";
  kind?: "setup" | "sync" | "maintenance"; // omitted → "maintenance" (the general bucket)
  timeoutSeconds?: number;
  outputLimitBytes?: number;
  configHash?: string;
}
// A page-list slice — GET /api/wiki/{id}/page-list?prefix=&offset=&limit=.
export interface PageListItem { path: string; title: string }
export interface PageListPage { items: PageListItem[]; total: number; hasMore: boolean }
// A repository operation run — one job with a streamed log + terminal status.
export type JobStatus = "running" | "succeeded" | "failed" | "cancelled";

export const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";
export const btnCls =
  "rounded-xl border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:text-ink";

// Format a placeholder-dimmed `$ argv` line. Tokens carrying a {name} template
// are rendered muted so it's clear they're filled at run time.
export function ArgvLine({ argv, className = "" }: { argv: string[]; className?: string }) {
  return (
    <code className={`inline-flex flex-wrap items-center gap-x-1.5 rounded-lg border border-pressed bg-soft px-2.5 py-1 font-mono text-[11px] text-body ${className}`}>
      <span className="text-mute">$</span>
      {argv.map((tok, i) => (
        <span key={i} className={/\{[^}]+\}/.test(tok) ? "text-mute" : undefined}>{tok}</span>
      ))}
    </code>
  );
}

export function SourceChip({ source }: { source: LinkedOperation["source"] }) {
  return (
    <span className="rounded bg-soft px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-mute">
      {source === "repo" ? "from repo" : "local"}
    </span>
  );
}

export function StatePill({ status }: { status: JobStatus }) {
  const map: Record<JobStatus, { cls: string; dot: string; label: string }> = {
    running: { cls: "bg-primary-soft text-primary", dot: "bg-primary animate-pulse", label: "Running" },
    succeeded: { cls: "bg-success-soft text-success", dot: "bg-success", label: "Succeeded" },
    failed: { cls: "bg-danger-soft text-danger", dot: "bg-danger", label: "Failed" },
    cancelled: { cls: "bg-warn-soft text-warn", dot: "bg-warn", label: "Cancelled" },
  };
  const m = map[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 font-bold ${m.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />{m.label}
    </span>
  );
}

// An operation's kind label (linked-wiki-revamp §4). Kinds are setup/sync/
// maintenance; setup/sync carry special behavior downstream. `kind` is loosely
// typed to avoid a types-only import cycle with proposals.ts.
export function KindChip({ kind }: { kind?: string }) {
  const k = kind || "maintenance";
  const cls = k === "setup" ? "bg-primary-soft text-primary"
    : k === "sync" ? "bg-success-soft text-success"
    : "bg-soft text-mute";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${cls}`}>{k}</span>
  );
}

// A trust-tier badge (linked-wiki-revamp §3). readonly = content only;
// repo-tooling = may run the repo's own scripts.
export function TierBadge({ tier, mini = false }: { tier: string; mini?: boolean }) {
  const ro = tier === "readonly";
  const size = mini ? "px-1 py-0 text-[9px]" : "px-1.5 py-0.5 text-[10px]";
  return (
    <span className={`rounded font-bold uppercase tracking-wide ${size} ${ro ? "bg-success-soft text-success" : "bg-warn-soft text-warn"}`}>
      {ro ? "read-only" : "repo tooling"}
    </span>
  );
}

export function ModalShell({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-ink/50 p-6 backdrop-blur-sm" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-pressed bg-canvas shadow-2xl" onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  );
}

export function MarkdownPreview({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        h1: (p) => <h1 className="mb-3 text-xl font-semibold text-ink" {...p} />,
        h2: (p) => <h2 className="mb-2 mt-5 text-lg font-semibold text-ink" {...p} />,
        h3: (p) => <h3 className="mb-2 mt-4 text-base font-semibold text-ink" {...p} />,
        p: (p) => <p className="my-2 leading-relaxed text-body" {...p} />,
        a: (p) => <a className="text-primary underline" target="_blank" rel="noreferrer" {...p} />,
        ul: (p) => <ul className="my-2 list-disc space-y-1 pl-5 text-body" {...p} />,
        ol: (p) => <ol className="my-2 list-decimal space-y-1 pl-5 text-body" {...p} />,
        table: (p) => <table className="my-3 w-full text-left text-xs text-body" {...p} />,
        th: (p) => <th className="border border-pressed bg-soft px-2 py-1 text-ink" {...p} />,
        td: (p) => <td className="border border-pressed px-2 py-1" {...p} />,
        code: (p) => <code className="rounded bg-soft px-1 font-mono text-[0.9em] text-ink" {...p} />,
      }}
    >{content}</ReactMarkdown>
  );
}

export function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
export function relTime(iso?: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}
// {name} tokens in argv become typed inputs; each maps to a bounded string.
export function deriveInputs(argv: string[]): Record<string, { type: string; maxLength: number }> {
  const inputs: Record<string, { type: string; maxLength: number }> = {};
  const re = /\{([a-zA-Z0-9_]+)\}/g;
  for (const tok of argv) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(tok))) inputs[m[1]] = { type: "string", maxLength: 500 };
  }
  return inputs;
}
