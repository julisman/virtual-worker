// API-Call Debugging — trace viewer.
//
// Virtual Worker brand theme: warm paper ground, navy ink, Fredoka display titles + stat
// numbers, Nunito body, mono for ids/endpoints/JSON. Provider kinds carry an
// accent dot (navy/amber/green/blue); errors are signaled in danger red.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { apiFetch as authFetch } from "@/lib/api";

// MLX servers report the model's full snapshot path as its id — show a clean name.
function prettyModel(id: string | null | undefined): string {
  if (!id) return "";
  if (id.toLowerCase().endsWith(".gguf")) return id.split("/").pop() || id;
  const m = id.match(/models--[^/]+--([^/]+)/);
  if (m) return m[1];
  return id.split("/").pop() || id;
}

// --------------------------------------------------------------------------- //
// Types — mirror backend/routes/debug.py response shapes
// --------------------------------------------------------------------------- //

interface Usage {
  input: number;
  output: number;
  totalTokens: number;
  cacheRead: number;
  cacheWrite: number;
  cost: number;
}

interface DebugEntry {
  id: string;
  timestamp: string;
  type: "user" | "assistant" | "tool_call" | "tool_result" | "system" | "llm_fallback";
  text?: string | null;
  thinking?: string | null;
  toolName?: string | null;
  toolCallId?: string | null;
  toolArgs?: Record<string, unknown> | null;
  toolResult?: string | null;
  isError?: boolean;
  model?: string | null;
  provider?: string | null;
  usage?: Usage | null;
  stopReason?: string | null;
}

interface DebugRun {
  runId: string;
  startTime: string;
  endTime?: string;
  durationMs?: number | null;
  model?: string | null;
  provider?: string | null;
  endpoint?: string | null;
  stream?: boolean;
  statusCode?: number;
  entries: DebugEntry[];
  totalCost: number;
  totalTokens: number;
  inputTokens?: number;
  outputTokens?: number;
  cacheRead?: number;
  cacheWrite?: number;
  toolCalls: number;
  errorCount?: number;
  toolBreakdown?: Record<string, number>;
  firstUserText?: string | null;
  lastAssistantText?: string | null;
}

interface DebugSession {
  sessionId: string;
  runs: number;
  toolCalls?: number;
  errorCount?: number;
  totalTokens?: number;
  totalCost?: number;
  firstTime?: string;
  lastTime?: string;
  model?: string | null;
  provider?: string | null;
  firstMessage?: string | null;
  lastMessage?: string | null;
}

interface Summary {
  totalSessions: number;
  totalRuns: number;
  totalToolCalls: number;
  totalErrors: number;
  totalTokens: number;
  totalCost: number;
  providers: Record<string, number>;
  lastActivity?: string | null;
}

// --------------------------------------------------------------------------- //
// Formatters
// --------------------------------------------------------------------------- //

function formatTime(ts?: string | null) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString(undefined, { hour12: false });
  } catch {
    return ts;
  }
}
function formatCost(cost?: number) {
  if (!cost) return "$0";
  return `$${cost.toFixed(6)}`;
}
function formatDuration(ms?: number | null) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

// --------------------------------------------------------------------------- //
// Inline icons (avoid coupling to a specific icon-lib version)
// --------------------------------------------------------------------------- //

function Chevron({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2"
      className={`transition-transform ${open ? "rotate-90" : ""}`}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}
function Spinner() {
  return <span className="inline-block size-5 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

// Type accent — entry rows recolor by role; the textual label carries meaning.
const TYPE_COLOR: Record<string, string> = {
  user: "text-primary",
  assistant: "text-ink",
  tool_call: "text-info",
  tool_result: "text-body",
  llm_fallback: "text-danger",
  system: "text-mute",
};

// Provider kind accent dot — navy chat default, amber Anthropic, green OpenAI,
// blue Google, muted local.
function providerDot(provider?: string | null): string {
  const p = (provider ?? "").toLowerCase();
  if (p.includes("anthropic") || p.includes("claude")) return "bg-warn";
  if (p.includes("openai") || p.includes("gpt")) return "bg-success";
  if (p.includes("google") || p.includes("gemini")) return "bg-info";
  if (p.includes("local") || p.includes("mlx") || p.includes("ollama")) return "bg-mute";
  return "bg-primary";
}

// Reusable chip styles (pill shape — the brand's interactive signature).
const CHIP = "rounded-full border border-line bg-soft px-1.5 py-0.5 font-mono text-[10px] text-body";
const CHIP_INK = "rounded-full bg-danger px-1.5 py-0.5 font-mono text-[10px] text-white";

// --------------------------------------------------------------------------- //
// Entry node
// --------------------------------------------------------------------------- //

function EntryNode({ entry }: { entry: DebugEntry }) {
  const [expanded, setExpanded] = useState(false);
  const hasExpandable = !!(entry.thinking || entry.toolArgs || entry.toolResult || entry.usage);
  const color = entry.isError ? "text-danger" : (TYPE_COLOR[entry.type] ?? "text-body");
  const label =
    entry.type === "tool_call" ? "TOOL_CALL"
    : entry.type === "tool_result" ? (entry.isError ? "TOOL_ERROR" : "TOOL_RESULT")
    : entry.type === "llm_fallback" ? "LLM ERROR"
    : entry.type.toUpperCase();

  return (
    <div className="py-1.5">
      <div
        className={`flex items-start gap-2 rounded-lg px-2 py-1 -mx-2 ${hasExpandable ? "cursor-pointer hover:bg-soft" : ""}`}
        onClick={() => hasExpandable && setExpanded(!expanded)}
      >
        <div className="flex flex-1 flex-wrap items-center gap-2 min-w-0">
          <span className={`font-mono text-[10px] ${color}`}>{label}</span>
          {entry.toolName && (
            <span className={CHIP}>{entry.toolName}</span>
          )}
          {entry.usage && (
            <span className="font-mono text-[10px] text-body">
              {entry.usage.input.toLocaleString()}↓
              {entry.usage.cacheRead > 0 && (
                <span className="text-ink"> ({entry.usage.cacheRead.toLocaleString()}c)</span>
              )}{" "}
              {entry.usage.output.toLocaleString()}↑ · {formatCost(entry.usage.cost)}
            </span>
          )}
          {entry.model && <span className="font-mono text-[10px] text-mute" title={entry.model}>{prettyModel(entry.model)}</span>}
          <span className="ml-auto font-mono text-[10px] text-mute">{formatTime(entry.timestamp)}</span>
        </div>
        {hasExpandable && <span className="mt-0.5 text-mute"><Chevron open={expanded} /></span>}
      </div>
      {entry.text && <p className="mt-0.5 line-clamp-3 font-mono text-xs text-body">{entry.text}</p>}

      {expanded && (
        <div className="mt-1 ml-4 space-y-2">
          {entry.thinking && (
            <Block label="Thinking" tone="muted"><pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-body">{entry.thinking}</pre></Block>
          )}
          {entry.toolArgs && entry.type !== "llm_fallback" && (
            <Block label="Request body" tone="json" copy={JSON.stringify(entry.toolArgs, null, 2)}>
              <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-panel-ink">{JSON.stringify(entry.toolArgs, null, 2)}</pre>
            </Block>
          )}
          {entry.type === "llm_fallback" && (
            <Block label="Upstream provider error" tone="alert">
              {entry.text && <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed break-all text-danger">{entry.text}</pre>}
              {entry.toolArgs?.reason ? (
                <p className="mt-1 font-mono text-[10px] text-body">reason: {String(entry.toolArgs?.reason)}</p>
              ) : null}
            </Block>
          )}
          {entry.toolResult && (
            <Block label={entry.isError ? "Error" : "Result"} tone={entry.isError ? "alert" : "muted"} copy={entry.toolResult}>
              <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-body">{entry.toolResult}</pre>
            </Block>
          )}
          {entry.usage && (
            <Block label="Usage" tone="muted">
              <div className="grid grid-cols-2 gap-2 font-mono text-[10px] sm:grid-cols-4">
                <Stat k="Input" v={entry.usage.input.toLocaleString()} />
                <Stat k="Output" v={entry.usage.output.toLocaleString()} />
                <Stat k="Cache Read" v={entry.usage.cacheRead.toLocaleString()} />
                <Stat k="Cache Write" v={entry.usage.cacheWrite.toLocaleString()} />
                <Stat k="Total" v={entry.usage.totalTokens.toLocaleString()} />
                <Stat k="Cost" v={formatCost(entry.usage.cost)} />
              </div>
            </Block>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <span className="text-mute">{k}</span>
      <p className="font-semibold text-ink">{v}</p>
    </div>
  );
}

// Brand tones: warm nested box for neutral detail, danger box for alerts, and a
// dark navy panel for the request-body JSON viewer.
const TONE: Record<string, { box: string; label: string; copyBtn: string }> = {
  muted: { box: "border-line bg-softer", label: "text-body", copyBtn: "border-pressed text-body hover:bg-canvas hover:text-ink" },
  alert: { box: "border-danger bg-danger-soft", label: "text-danger", copyBtn: "border-danger/40 text-danger hover:bg-canvas" },
  json: { box: "border-panel bg-panel", label: "text-panel-ink", copyBtn: "border-panel-ink/30 text-panel-ink hover:bg-panel-ink/10" },
};

function Block({ label, tone, copy, children }: { label: string; tone: string; copy?: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const t = TONE[tone] ?? TONE.muted;
  return (
    <div className={`rounded-xl border p-3 ${t.box}`}>
      <div className="mb-1 flex items-center gap-1.5">
        <span className={`text-[10px] font-semibold uppercase tracking-wide ${t.label}`}>{label}</span>
        {copy && (
          <button
            className={`ml-auto rounded-full border px-2 py-0.5 font-mono text-[10px] ${t.copyBtn}`}
            onClick={(e) => {
              e.stopPropagation();
              navigator.clipboard?.writeText(copy).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              });
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>
      {children}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Run card
// --------------------------------------------------------------------------- //

function RunCard({ run, index, errorsOnly }: { run: DebugRun; index: number; errorsOnly: boolean }) {
  const [expanded, setExpanded] = useState(index === 0);
  const errorCount = run.errorCount ?? run.entries.filter((e) => e.isError || e.type === "llm_fallback").length;
  const visible = errorsOnly
    ? run.entries.filter((e) => e.type === "llm_fallback" || e.isError)
    : run.entries;

  return (
    <div className={`overflow-hidden rounded-xl border bg-canvas ${errorCount > 0 ? "border-danger" : "border-line"}`}>
      <button onClick={() => setExpanded(!expanded)} className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-soft">
        <span className="text-mute"><Chevron open={expanded} /></span>
        <div className="flex flex-1 flex-wrap items-center gap-2">
          <span className="font-mono text-[10px] text-mute">#{index + 1}</span>
          <span className="font-mono text-xs text-body">{run.runId.slice(0, 8)}</span>
          {run.endpoint && (
            <span className={CHIP}>{run.endpoint}</span>
          )}
          {run.durationMs != null && (
            <span className={CHIP}>{formatDuration(run.durationMs)}</span>
          )}
          {run.model && <span className={CHIP} title={run.model}>{prettyModel(run.model)}</span>}
          {run.toolCalls > 0 && (
            <span className={CHIP}>{run.toolCalls} tool{run.toolCalls > 1 ? "s" : ""}</span>
          )}
          {errorCount > 0 && (
            <span className={CHIP_INK}>{errorCount} error{errorCount > 1 ? "s" : ""}</span>
          )}
        </div>
        <div className="flex flex-shrink-0 items-center gap-3 font-mono text-[10px] text-body">
          {run.inputTokens != null && <span>{run.inputTokens.toLocaleString()}↓</span>}
          {run.outputTokens != null && <span>{run.outputTokens.toLocaleString()}↑</span>}
          <span>{run.totalTokens.toLocaleString()} tok</span>
          <span className="text-primary-deep">{formatCost(run.totalCost)}</span>
        </div>
      </button>

      {!expanded && (run.firstUserText || run.lastAssistantText) && (
        <div className="space-y-0.5 px-4 pb-2">
          {run.firstUserText && <p className="truncate font-mono text-[10px] text-body">▸ {run.firstUserText}</p>}
          {run.lastAssistantText && <p className="truncate font-mono text-[10px] text-mute">◂ {run.lastAssistantText}</p>}
        </div>
      )}

      {expanded && (
        <div className="border-t border-line px-4 py-2">
          {run.toolBreakdown && Object.keys(run.toolBreakdown).length > 0 && (
            <div className="mb-1 flex flex-wrap gap-1.5 border-b border-line py-2">
              {Object.entries(run.toolBreakdown).sort((a, b) => b[1] - a[1]).map(([name, count]) => (
                <span key={name} className={CHIP}>{name} ×{count}</span>
              ))}
            </div>
          )}
          {visible.length === 0
            ? <p className="py-2 text-[10px] italic text-mute">No matching entries</p>
            : visible.map((e) => <EntryNode key={e.id} entry={e} />)}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function DebugPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sessions, setSessions] = useState<DebugSession[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ runs: DebugRun[]; totalEntries: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [search, setSearch] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [runErrorsOnly, setRunErrorsOnly] = useState(false);
  const [auto, setAuto] = useState(false);

  const loadSessions = useCallback(async () => {
    try {
      const res = await authFetch("/api/debug/sessions");
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      setSummary(data.summary);
      setSessions(data.sessions ?? []);
    } catch {
      setSummary(null);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    setLoadingDetail(true);
    try {
      const res = await authFetch(`/api/debug/sessions/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(String(res.status));
      setDetail(await res.json());
    } catch {
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => { loadSessions(); }, [loadSessions]);
  useEffect(() => { if (selected) loadDetail(selected); }, [selected, loadDetail]);

  // Auto-refresh
  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => {
      loadSessions();
      if (selected) loadDetail(selected);
    }, 10_000);
    return () => clearInterval(t);
  }, [auto, selected, loadSessions, loadDetail]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return sessions.filter((s) => {
      if (errorsOnly && !(s.errorCount && s.errorCount > 0)) return false;
      if (q) {
        const hay = `${s.sessionId} ${s.model ?? ""} ${s.provider ?? ""} ${s.firstMessage ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [sessions, search, errorsOnly]);

  if (loading) {
    return <div className="flex h-96 items-center justify-center text-primary"><Spinner /></div>;
  }

  const runs = detail?.runs ? [...detail.runs].reverse() : [];
  const shownRuns = runErrorsOnly ? runs.filter((r) => (r.errorCount ?? 0) > 0) : runs;

  return (
    <div className="p-6">
      <div className="-mx-6 -mt-6 mb-5 flex items-center gap-3 border-b border-pressed bg-softer px-6 py-4">
        <div>
          <h1 className="font-display text-2xl font-bold tracking-tight text-ink">API Call Debugging</h1>
          <p className="mt-0.5 text-sm text-body">Traces of every LLM API call (local + external providers)</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setAuto(!auto)}
            className={`rounded-full border px-4 py-1.5 text-sm font-medium ${auto ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft hover:text-ink"}`}
          >{auto ? "Auto 10s" : "Paused"}</button>
          <button
            onClick={() => { loadSessions(); if (selected) loadDetail(selected); }}
            className="rounded-full border border-pressed px-4 py-1.5 text-sm font-medium text-body hover:bg-soft hover:text-ink"
          >Refresh</button>
        </div>
      </div>

      {summary && (
        <div className="mb-5 grid grid-cols-2 gap-4 rounded-2xl border border-line bg-canvas p-4 sm:grid-cols-3 lg:grid-cols-6">
          <Metric k="Sessions" v={summary.totalSessions} />
          <Metric k="Runs" v={summary.totalRuns} />
          <Metric k="Tool Calls" v={summary.totalToolCalls} />
          <Metric k="Errors" v={summary.totalErrors} c={summary.totalErrors > 0 ? "text-danger" : "text-mute"} />
          <Metric k="Tokens" v={summary.totalTokens.toLocaleString()} />
          <Metric k="Cost" v={formatCost(summary.totalCost)} />
        </div>
      )}

      <div className="grid grid-cols-12 gap-6">
        {/* Sessions */}
        <div className="col-span-12 lg:col-span-4">
          <div className="rounded-2xl border border-line bg-canvas p-4">
            <div className="mb-3 flex items-center gap-2">
              <h2 className="text-sm font-semibold text-ink">Sessions</h2>
              <span className="text-mute">({filtered.length})</span>
              <button
                onClick={() => setErrorsOnly(!errorsOnly)}
                className={`ml-auto rounded-full border px-2.5 py-1 font-mono text-[10px] ${errorsOnly ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}
              >errors</button>
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search session / model / provider…"
              className="mb-3 w-full rounded-lg border border-line bg-softer px-3 py-1.5 font-mono text-xs text-ink outline-none placeholder:text-mute focus:border-primary"
            />
            <div className="max-h-[calc(100vh-360px)] space-y-2 overflow-y-auto">
              {filtered.length === 0 && (
                <p className="py-6 text-center text-xs italic text-mute">
                  {sessions.length === 0 ? "No API calls captured yet" : "No sessions match"}
                </p>
              )}
              {filtered.map((s) => (
                <button
                  key={s.sessionId}
                  onClick={() => setSelected(s.sessionId)}
                  className={`w-full rounded-xl border p-3 text-left transition-colors ${
                    selected === s.sessionId
                      ? "border-line bg-canvas shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]"
                      : s.errorCount && s.errorCount > 0
                      ? "border-danger bg-canvas hover:bg-soft"
                      : "border-line hover:bg-soft"
                  }`}
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span className={`size-2 shrink-0 rounded-full ${providerDot(s.provider)}`} />
                    <span className="truncate font-mono text-xs text-ink" title={s.sessionId}>{s.sessionId}</span>
                    {s.provider && <span className={CHIP}>{s.provider}</span>}
                    {s.errorCount ? <span className={`ml-auto ${CHIP_INK}`}>{s.errorCount}</span> : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-mute">
                    <span>{s.runs} runs</span>
                    {s.toolCalls ? <><span>·</span><span className="text-body">{s.toolCalls} tools</span></> : null}
                    {s.totalTokens ? <><span>·</span><span>{s.totalTokens.toLocaleString()} tok</span></> : null}
                    {s.totalCost ? <><span>·</span><span className="text-body">{formatCost(s.totalCost)}</span></> : null}
                  </div>
                  <div className="mt-1 font-mono text-[10px] text-mute">{formatTime(s.lastTime)}</div>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Detail */}
        <div className="col-span-12 lg:col-span-8">
          {!selected && (
            <div className="rounded-2xl border border-line bg-canvas p-8 text-center">
              <p className="text-body">Select a session to inspect its API calls</p>
            </div>
          )}
          {selected && loadingDetail && !detail && (
            <div className="flex justify-center rounded-2xl border border-line bg-canvas p-8 text-primary"><Spinner /></div>
          )}
          {selected && detail && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="font-display text-sm font-semibold text-ink">{runs.length} runs · {detail.totalEntries} entries</span>
                <button
                  onClick={() => setRunErrorsOnly(!runErrorsOnly)}
                  className={`ml-auto rounded-full border px-3 py-1.5 font-mono text-[10px] ${runErrorsOnly ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}
                >errors only</button>
              </div>
              <div className="max-h-[calc(100vh-300px)] space-y-2 overflow-y-auto">
                {shownRuns.length === 0
                  ? <div className="rounded-2xl border border-line bg-canvas p-8 text-center text-sm text-body">No runs match the filter</div>
                  : shownRuns.map((run, i) => <RunCard key={run.runId} run={run} index={i} errorsOnly={runErrorsOnly} />)}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ k, v, c }: { k: string; v: number | string; c?: string }) {
  return (
    <div className="flex flex-col">
      <span className="font-mono text-[10px] uppercase tracking-wide text-mute">{k}</span>
      <span className={`font-display text-2xl font-bold ${c ?? "text-ink"}`}>{v}</span>
    </div>
  );
}

export default DebugPage;
