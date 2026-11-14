// API-Call Debugging — trace viewer.
//
// Virtual Worker brand theme: warm paper ground, navy ink, Fredoka display titles + stat
// numbers, Nunito body, mono for ids/endpoints/JSON. Provider kinds carry an
// accent dot (navy/amber/green/blue); errors are signaled in danger red.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

// Every field is optional: loop-recorded entries may carry a partial dict
// (e.g. only cacheRead/cacheWrite on remote assistant turns).
interface Usage {
  input?: number;
  output?: number;
  totalTokens?: number;
  cacheRead?: number;
  cacheWrite?: number;
  cost?: number;
}

interface CacheMiss {
  rebilledTokens: number;
  estimatedCost: number;
  cause: string;
}

interface DebugEntry {
  id: string;
  timestamp: string;
  type: "user" | "assistant" | "tool_call" | "tool_result" | "system" | "llm_fallback" | "llm_retry";
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
  cacheMiss?: CacheMiss;
  stopReason?: string | null;
  truncatedChars?: number;  // chars of this result withheld from the model (0 = model saw it all)
  crushStrategy?: string | null;  // PRD-17: json | lines | headtail | passthrough; null = compression off
}

interface DebugRun {
  runId: string;
  startTime: string;
  endTime?: string;
  durationMs?: number | null;
  model?: string | null;
  provider?: string | null;
  reasoningEffort?: string | null;
  endpoint?: string | null;
  stream?: boolean;
  statusCode?: number;
  entries: DebugEntry[];
  totalCost: number;
  totalTokens: number;
  inputTokens?: number;
  outputTokens?: number;
  promptTokensSum?: number;
  // Context-window usage: the biggest single model call's prompt in this run,
  // the model's window (null = unknown), and every call's prompt size in order.
  peakContextTokens?: number;
  contextWindow?: number | null;
  contextSeries?: number[];
  cacheRead?: number;
  cacheWrite?: number;
  cacheMiss?: CacheMiss;
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
  agentId?: string | null;
  firstMessage?: string | null;
  lastMessage?: string | null;
  peakContextTokens?: number;
  lastContextTokens?: number;
  contextWindow?: number | null;
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

function formatK(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return String(n);
}

// --------------------------------------------------------------------------- //
// Context-window meter
// --------------------------------------------------------------------------- //

// How full the context window got. Green under 60%, amber to 85%, red beyond —
// past ~85% a run is close to compaction/trimming and quality usually drops.
function ctxTone(pct: number | null) {
  if (pct == null) return { bar: "bg-primary", text: "text-body" };
  if (pct >= 85) return { bar: "bg-danger", text: "text-danger" };
  if (pct >= 60) return { bar: "bg-warn", text: "text-warn" };
  return { bar: "bg-success", text: "text-success" };
}

function ctxPct(tokens: number, window?: number | null): number | null {
  return window && window > 0 ? Math.round((tokens / window) * 100) : null;
}

function ContextMeter({ tokens, window, compact = false, label = "ctx" }: {
  tokens: number; window?: number | null; compact?: boolean; label?: string;
}) {
  if (!tokens) return null;
  const pct = ctxPct(tokens, window);
  const tone = ctxTone(pct);
  const title = window
    ? `Context window: ${tokens.toLocaleString()} of ${window.toLocaleString()} tokens (${pct}%) — the largest single prompt sent to the model`
    : `Context: ${tokens.toLocaleString()} tokens — the largest single prompt sent (model window unknown)`;
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[10px]" title={title}>
      <span className="text-mute">{label}</span>
      {pct != null && (
        <span className={`relative inline-block h-1.5 overflow-hidden rounded-full bg-line ${compact ? "w-10" : "w-16"}`}>
          <span className={`absolute inset-y-0 left-0 ${tone.bar}`} style={{ width: `${Math.min(100, pct)}%` }} />
        </span>
      )}
      <span className={tone.text}>
        {formatK(tokens)}{window ? ` / ${formatK(window)}` : ""}{pct != null ? ` · ${pct}%` : ""}
      </span>
    </span>
  );
}

// Per-call prompt size across one run: shows the context growing as tool
// results pile up, relative to the window (or the run's own peak if unknown).
function ContextGrowth({ series, window }: { series: number[]; window?: number | null }) {
  if (series.length === 0) return null;
  const peak = Math.max(...series);
  const scale = window && window > 0 ? window : peak;
  return (
    <div className="border-b border-line py-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-mute">
        <span className="font-semibold uppercase tracking-wide">Context window</span>
        <ContextMeter tokens={peak} window={window} label="peak" />
        <span>start {formatK(series[0])}</span>
        <span>end {formatK(series[series.length - 1])}</span>
        <span>{series.length} model call{series.length === 1 ? "" : "s"}</span>
      </div>
      <div className="relative flex h-12 items-end gap-px rounded-md bg-soft px-1 pt-1"
        role="img" aria-label={`Prompt size per model call, peak ${peak.toLocaleString()} tokens`}>
        {series.map((t, i) => {
          const pct = ctxPct(t, window);
          return (
            <span key={i} className={`min-w-[2px] flex-1 rounded-t-sm ${ctxTone(pct).bar}`}
              style={{ height: `${Math.max(3, Math.min(100, (t / scale) * 100))}%` }}
              title={`call ${i + 1}: ${t.toLocaleString()} tok${pct != null ? ` (${pct}% of window)` : ""}`} />
          );
        })}
      </div>
    </div>
  );
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
  llm_retry: "text-warn",
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
  // Context entries reuse the usage column for window-mode metadata (no token
  // fields) — only treat usage as renderable when it carries actual counts.
  const usage = entry.usage &&
    [entry.usage.input, entry.usage.output, entry.usage.cacheRead,
     entry.usage.cacheWrite, entry.usage.totalTokens].some((v) => typeof v === "number")
    ? entry.usage : null;
  const hasExpandable = !!(entry.thinking || entry.toolArgs || entry.toolResult || usage
    || entry.type === "llm_fallback" || entry.type === "llm_retry");
  const color = entry.isError ? "text-danger" : (TYPE_COLOR[entry.type] ?? "text-body");
  const label =
    entry.type === "tool_call" ? "TOOL_CALL"
    : entry.type === "tool_result" ? (entry.isError ? "TOOL_ERROR" : "TOOL_RESULT")
    : entry.type === "llm_fallback" ? "LLM ERROR"
    : entry.type === "llm_retry" ? "PROVIDER RETRY"
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
          {!entry.text && entry.thinking && (
            <span className={CHIP} title="This turn produced no visible text — only tool calls plus captured reasoning; expand to read it.">
              thinking only
            </span>
          )}
          {usage && (
            <span className="font-mono text-[10px] text-body">
              {(usage.input ?? 0).toLocaleString()}↓
              {(usage.cacheRead ?? 0) > 0 && (
                <span className="text-ink"> ({(usage.cacheRead ?? 0).toLocaleString()} cache read)</span>
              )}{" "}
              {(usage.cacheWrite ?? 0) > 0 && (
                <span className="text-primary"> ({(usage.cacheWrite ?? 0).toLocaleString()} cache write)</span>
              )}{" "}
              {(usage.output ?? 0).toLocaleString()}↑ · {formatCost(usage.cost ?? 0)}
            </span>
          )}
          {entry.cacheMiss && (
            <span className="rounded-full border border-warn/40 bg-warn-soft px-1.5 py-0.5 font-mono text-[10px] text-warn"
              title={`Estimated ${entry.cacheMiss.rebilledTokens.toLocaleString()} tokens rebilled; ${entry.cacheMiss.cause}`}>
              cache miss · ~{entry.cacheMiss.rebilledTokens.toLocaleString()} tok · {entry.cacheMiss.cause}
            </span>
          )}
          {entry.model && <span className="font-mono text-[10px] text-mute" title={entry.model}>{prettyModel(entry.model)}</span>}
          {!!entry.truncatedChars && entry.truncatedChars > 0 && (
            entry.crushStrategy && entry.crushStrategy !== "passthrough" ? (
              <span className={CHIP_INK} title={`This result was compressed structure-aware (${entry.crushStrategy}) to save context — ${entry.truncatedChars.toLocaleString()} chars withheld, but totals/error rows/extremes were kept and the model could recover the original via retrieve_full. Full result below.`}>
                🗜 −{entry.truncatedChars.toLocaleString()} crushed ({entry.crushStrategy})
              </span>
            ) : (
              <span className={CHIP_INK} title={`The model saw a truncated copy of this result — ${entry.truncatedChars.toLocaleString()} trailing chars were cut to fit the context budget. The full result is shown below.`}>
                ✂ {entry.truncatedChars.toLocaleString()} truncated
              </span>
            )
          )}
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
          {entry.type === "llm_retry" && (
            <Block label="Provider blip — retried" tone="warning">
              {entry.text && <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed break-all text-warn">{entry.text}</pre>}
            </Block>
          )}
          {entry.toolResult && (
            <Block label={entry.isError ? "Error" : "Result"} tone={entry.isError ? "alert" : "muted"} copy={entry.toolResult}>
              {!!entry.truncatedChars && entry.truncatedChars > 0 && (
                entry.crushStrategy && entry.crushStrategy !== "passthrough" ? (
                  <p className="mb-1.5 font-mono text-[10px] text-ink" title="Structure-aware compression: totals, error rows and numeric extremes were preserved, and the model could fetch the original on demand with retrieve_full.">
                    🗜 The model saw a compressed ({entry.crushStrategy}) copy — {entry.truncatedChars.toLocaleString()} chars withheld but recoverable via retrieve_full. Full result below.
                  </p>
                ) : (
                  <p className="mb-1.5 font-mono text-[10px] text-ink" title="The model was fed a shortened copy to stay within its context budget; below is the full result as captured.">
                    ✂ The model saw only the first part of this result — {entry.truncatedChars.toLocaleString()} trailing chars were truncated to fit the context budget. Full result below.
                  </p>
                )
              )}
              <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-body">{entry.toolResult}</pre>
            </Block>
          )}
          {usage && (
            <Block label="Usage" tone="muted">
              <div className="grid grid-cols-2 gap-2 font-mono text-[10px] sm:grid-cols-4">
                <Stat k="Input" v={(usage.input ?? 0).toLocaleString()} />
                <Stat k="Output" v={(usage.output ?? 0).toLocaleString()} />
                <Stat k="Cache Read" v={(usage.cacheRead ?? 0).toLocaleString()} />
                <Stat k="Cache Write" v={(usage.cacheWrite ?? 0).toLocaleString()} />
                <Stat k="Total" v={(usage.totalTokens ?? 0).toLocaleString()} />
                <Stat k="Cost" v={formatCost(usage.cost ?? 0)} />
              </div>
              {entry.cacheMiss && (
                <p className="mt-2 rounded-lg border border-warn/30 bg-warn-soft px-2 py-1.5 font-mono text-[10px] text-warn">
                  Cache miss: ~{entry.cacheMiss.rebilledTokens.toLocaleString()} tokens rebilled · {entry.cacheMiss.cause}
                  {entry.cacheMiss.estimatedCost > 0 ? ` · est. ${formatCost(entry.cacheMiss.estimatedCost)}` : ""}
                </p>
              )}
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
  warning: { box: "border-warn bg-warn-soft", label: "text-warn", copyBtn: "border-warn/40 text-warn hover:bg-canvas" },
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

function RunCard({ run, index, errorsOnly, focus = false }: { run: DebugRun; index: number; errorsOnly: boolean; focus?: boolean }) {
  const [expanded, setExpanded] = useState(index === 0 || focus);
  const ref = useRef<HTMLDivElement>(null);
  const errorCount = run.errorCount ?? run.entries.filter((e) => e.isError || e.type === "llm_fallback").length;
  const retryCount = run.entries.filter((e) => e.type === "llm_retry").length;
  const visible = errorsOnly
    ? run.entries.filter((e) => e.type === "llm_fallback" || e.type === "llm_retry" || e.isError)
    : run.entries;

  // Provider cache hit-rate for the header: cacheRead (summed across turns) vs
  // the run's summed prompt tokens (promptTokensSum), so the ratio is honest —
  // inputTokens alone is only the *last* turn's prompt size and would make the
  // rate read as a bogus clamped 100%. Older runs predate promptTokensSum, so
  // fall back to the previous (less accurate) inputTokens-based formula.
  const cacheRead = run.cacheRead ?? 0;
  const cacheWrite = run.cacheWrite ?? 0;
  const inputTok = run.inputTokens ?? 0;
  const promptSum = run.promptTokensSum ?? 0;
  const cacheDenominator = promptSum > 0 ? promptSum : inputTok;
  const displayedInput = promptSum > 0 ? promptSum : inputTok;
  const displayedTotal = displayedInput + (run.outputTokens ?? 0);
  const cachePct = cacheRead > 0 && cacheDenominator > 0
    ? Math.min(100, Math.round((cacheRead / cacheDenominator) * 100))
    : null;

  // Deep-linked run: expand it and scroll it into view so the linked turn is
  // what the user lands on (not just the top of the session).
  useEffect(() => {
    if (focus) {
      setExpanded(true);
      ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [focus]);

  return (
    <div ref={ref} className={`overflow-hidden rounded-xl border bg-canvas ${
      focus ? "border-primary ring-2 ring-primary-line" : errorCount > 0 ? "border-danger" : "border-line"}`}>
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
          {run.reasoningEffort && (
            <span className={CHIP} title="reasoning effort sent to the provider (temperature dropped)">
              effort {run.reasoningEffort}
            </span>
          )}
          {run.toolCalls > 0 && (
            <span className={CHIP}>{run.toolCalls} tool{run.toolCalls > 1 ? "s" : ""}</span>
          )}
          {errorCount > 0 && (
            <span className={CHIP_INK}>{errorCount} error{errorCount > 1 ? "s" : ""}</span>
          )}
          {retryCount > 0 && (
            <span className="rounded-full border border-warn/40 bg-warn-soft px-1.5 py-0.5 font-mono text-[10px] text-warn">
              {retryCount} retr{retryCount > 1 ? "ies" : "y"}
            </span>
          )}
        </div>
        <div className="flex flex-shrink-0 items-center gap-3 font-mono text-[10px] text-body">
          {(run.inputTokens != null || promptSum > 0) && (
            <span title={promptSum > 0
              ? `Cumulative prompt tokens summed across all ${(run.contextSeries?.length || 0) || "the"} model calls (each call resends the history) — the ctx meter shows how full the window actually got`
              : "Input tokens"}>
              {displayedInput.toLocaleString()}↓
            </span>
          )}
          {run.outputTokens != null && <span>{run.outputTokens.toLocaleString()}↑</span>}
          {(run.peakContextTokens ?? 0) > 0 && (
            <ContextMeter tokens={run.peakContextTokens ?? 0} window={run.contextWindow} />
          )}
          <span>{displayedTotal.toLocaleString()} tok</span>
          {cachePct != null && (
            <span
              className={CHIP}
              title={`${cacheRead.toLocaleString()} of ${cacheDenominator.toLocaleString()} prompt tokens served from provider cache${
                cacheWrite > 0 ? ` · ${cacheWrite.toLocaleString()} written to cache` : ""}`}
            >
              {cachePct}% cached
            </span>
          )}
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
          {(run.contextSeries?.length ?? 0) > 0 && (
            <ContextGrowth series={run.contextSeries ?? []} window={run.contextWindow} />
          )}
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

export function DebugPage({ openRequest }: { openRequest?: { id: string; runId?: string; nonce: number } } = {}) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sessions, setSessions] = useState<DebugSession[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [focusRunId, setFocusRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ runs: DebugRun[]; totalEntries: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [search, setSearch] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [runErrorsOnly, setRunErrorsOnly] = useState(false);
  const [auto, setAuto] = useState(false);
  const [agentFilter, setAgentFilter] = useState("");
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});

  useEffect(() => {
    authFetch("/api/agents")
      .then((r) => (r.ok ? r.json() : { agents: [] }))
      .then((d: { agents?: { id: string; name: string }[] }) => {
        const map: Record<string, string> = {};
        (d.agents ?? []).forEach((a) => { map[a.id] = a.name; });
        setAgentNames(map);
      })
      .catch(() => {});
  }, []);

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

  // Clear captured traces. NOTE: Debug traces (debug_db) are a SEPARATE store
  // from chat conversations (chat_store) — clearing a trace here does not delete
  // the conversation, and deleting a conversation elsewhere does not clear its
  // trace. For a live chat channel the next inbound message re-creates the
  // session, so a cleared trace will reappear as new turns arrive.
  const clearSession = useCallback(async (id: string) => {
    if (!window.confirm(
      "Delete this session's debug trace?\n\nThis clears only the captured API-call "
      + "trace — the chat conversation is not affected. A live bot may re-create it "
      + "on the next message.")) return;
    try {
      await authFetch(`/api/debug/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch { /* best-effort */ }
    setSelected(null);
    setDetail(null);
    await loadSessions();
  }, [loadSessions]);

  const clearAll = useCallback(async () => {
    if (!window.confirm(
      "Delete ALL debug traces?\n\nThis clears every captured API-call trace. Chat "
      + "conversations are not affected.")) return;
    try {
      await authFetch("/api/debug/sessions", { method: "DELETE" });
    } catch { /* best-effort */ }
    setSelected(null);
    setDetail(null);
    await loadSessions();
  }, [loadSessions]);

  useEffect(() => { loadSessions(); }, [loadSessions]);
  useEffect(() => { if (selected) loadDetail(selected); }, [selected, loadDetail]);

  // Deep-link from a chat turn or an activity row → preselect that session's
  // trace, and (when given) focus the exact run so the UI lands on that turn.
  useEffect(() => {
    if (openRequest?.id) setSelected(openRequest.id);
    setFocusRunId(openRequest?.runId ?? null);
  }, [openRequest]);

  // Auto-refresh
  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => {
      loadSessions();
      if (selected) loadDetail(selected);
    }, 10_000);
    return () => clearInterval(t);
  }, [auto, selected, loadSessions, loadDetail]);

  // All agents (so every agent is selectable, even before it has traced a run).
  const agentOptions = useMemo(
    () => Object.entries(agentNames).map(([id, name]) => ({ id, name })),
    [agentNames],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return sessions.filter((s) => {
      if (errorsOnly && !(s.errorCount && s.errorCount > 0)) return false;
      if (agentFilter && s.agentId !== agentFilter) return false;
      if (q) {
        const hay = `${s.sessionId} ${s.model ?? ""} ${s.provider ?? ""} ${s.firstMessage ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [sessions, search, errorsOnly, agentFilter]);

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
          <button
            onClick={clearAll}
            disabled={sessions.length === 0}
            title="Delete every captured trace (chat conversations are unaffected)"
            className="rounded-full border border-pressed px-4 py-1.5 text-sm font-medium text-body hover:border-danger hover:bg-danger-soft hover:text-danger disabled:opacity-40 disabled:hover:border-pressed disabled:hover:bg-transparent disabled:hover:text-body"
          >Clear all</button>
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
              {agentOptions.length > 0 && (
                <select
                  value={agentFilter}
                  onChange={(e) => setAgentFilter(e.target.value)}
                  title="Filter by agent"
                  className={`ml-auto rounded-full border px-2 py-1 font-mono text-[10px] outline-none ${agentFilter ? "border-primary bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-body"}`}
                >
                  <option value="">all agents</option>
                  {agentOptions.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              )}
              <button
                onClick={() => setErrorsOnly(!errorsOnly)}
                className={`${agentOptions.length > 0 ? "" : "ml-auto "}rounded-full border px-2.5 py-1 font-mono text-[10px] ${errorsOnly ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}
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
                  aria-current={selected === s.sessionId}
                  className={`relative w-full overflow-hidden rounded-xl border p-3 pl-4 text-left transition-colors ${
                    selected === s.sessionId
                      ? "border-primary bg-primary-soft ring-2 ring-primary-line"
                      : s.errorCount && s.errorCount > 0
                      ? "border-danger bg-canvas hover:bg-soft"
                      : "border-line hover:bg-soft"
                  }`}
                >
                  {/* Active accent bar — clear even on an error (red-chipped) session */}
                  {selected === s.sessionId && <span className="absolute inset-y-0 left-0 w-1 bg-primary" />}
                  <div className="mb-1 flex items-center gap-2">
                    <span className={`size-2 shrink-0 rounded-full ${providerDot(s.provider)}`} />
                    <span className={`truncate font-mono text-xs ${selected === s.sessionId ? "font-semibold text-primary-deep" : "text-ink"}`} title={s.sessionId}>{s.sessionId}</span>
                    {s.provider && <span className={CHIP}>{s.provider}</span>}
                    {s.errorCount ? <span className={`ml-auto ${CHIP_INK}`} title={`${s.errorCount} error(s)`}>⚠ {s.errorCount}</span> : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-mute">
                    <span>{s.runs} runs</span>
                    {s.toolCalls ? <><span>·</span><span className="text-body">{s.toolCalls} tools</span></> : null}
                    {s.totalTokens ? <><span>·</span><span>{s.totalTokens.toLocaleString()} tok</span></> : null}
                    {s.totalCost ? <><span>·</span><span className="text-body">{formatCost(s.totalCost)}</span></> : null}
                  </div>
                  {(s.lastContextTokens ?? 0) > 0 && (
                    <div className="mt-1" title={s.peakContextTokens && s.peakContextTokens !== s.lastContextTokens
                      ? `session peak ${s.peakContextTokens.toLocaleString()} tok` : undefined}>
                      <ContextMeter tokens={s.lastContextTokens ?? 0} window={s.contextWindow} compact label="last ctx" />
                    </div>
                  )}
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
                <span className="font-mono text-[10px] text-mute">newest first ↓</span>
                <div className="ml-auto flex items-center gap-2">
                  <button
                    onClick={() => setRunErrorsOnly(!runErrorsOnly)}
                    className={`rounded-full border px-3 py-1.5 font-mono text-[10px] ${runErrorsOnly ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}
                  >errors only</button>
                  <button
                    onClick={() => clearSession(selected)}
                    title="Delete this session's trace (the chat conversation is unaffected)"
                    className="rounded-full border border-pressed px-3 py-1.5 font-mono text-[10px] text-body hover:border-danger hover:bg-danger-soft hover:text-danger"
                  >delete trace</button>
                </div>
              </div>
              <div className="max-h-[calc(100vh-300px)] space-y-2 overflow-y-auto">
                {shownRuns.length === 0
                  ? <div className="rounded-2xl border border-line bg-canvas p-8 text-center text-sm text-body">No runs match the filter</div>
                  : shownRuns.map((run, i) => <RunCard key={run.runId} run={run} index={i} errorsOnly={runErrorsOnly} focus={run.runId === focusRunId} />)}
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
