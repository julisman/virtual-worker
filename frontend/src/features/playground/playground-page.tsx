// Chat — talk to the active local model via the OpenAI-compatible /v1 gateway.
//
// Unsloth-inspired: centered greeting on an empty thread, a large rounded
// composer ("Ask anything") with an attach menu, system prompt, temperature,
// and an up-arrow send. Monochrome (Uber-inspired) theme; plain React.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { apiFetch as authFetch, apiJson, apiPost } from "@/lib/api";
import type { Agent } from "@/features/agents/agents-page";
import { ChatProposalCard } from "./chat-proposal-card";
import { QueuedMessages, type QueuedEntry } from "./queued-messages";
import appIcon from "@/assets/icon.svg";

const AGENT_KEY = "vw:chat-agent";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

interface ChatStep { name: string; args: Record<string, unknown>; done: boolean; result?: string; error?: boolean }

// One entry in an assistant turn's work log: either a run of narration text or a
// tool call, kept in the order they streamed so the timeline reads chronologically.
type TimelineItem = { kind: "text"; text: string } | { kind: "tool"; step: ChatStep };

interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  images?: string[]; // data URLs (multimodal — needs a vision model)
  steps?: ChatStep[]; // tool activity (agentic / Search) — flat; kept for old sessions + meta
  timeline?: TimelineItem[]; // ordered narration + tool calls before the final answer (folds into one panel)
  proposalIds?: string[]; // wiki approval proposals filed by tools this turn (optional; old sessions lack it)
  meta?: {
    ms?: number; tokens?: number; tokSec?: number; tools?: string[]; skills?: string[]; runId?: string;
    kind?: string; taskId?: string; fromAgentId?: string; // inbox handoff meta (kind: "inbox" | "inbox_reply")
  };
  compacted?: boolean; // a summary of earlier turns, produced by /compact or auto-compact
}

// `/` menu entries: built-in commands + the agent's skills, unified.
type SlashItem = { kind: "command" | "skill"; id: string; name: string; description: string };
const SLASH_COMMANDS = [
  { id: "compact", name: "/compact", description: "Summarize older turns to shrink the context" },
  { id: "clear", name: "/clear", description: "Start a fresh chat (this one stays saved)" },
];

interface Pending {
  kind: "text" | "image";
  name: string;
  text?: string;
  dataUrl?: string;
}

interface OpenRequest { id: string | null; nonce: number }
interface AgentRequest { id: string; nonce: number }
interface ChatProps {
  openRequest: OpenRequest;          // sidebar asks to open a session (id) or start new (null)
  agentRequest: AgentRequest;        // "Chat with this agent" asks to select an agent
  onSessionId: (id: string | null) => void;  // report the active session id back up
  onSaved?: () => void;              // optional hook fired when a turn is saved
  onTrace: (sessionId: string, runId?: string) => void;  // "Trace in Debug" → open this turn's trace
}

const newSessionId = () =>
  "chat:" + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));

// --------------------------------------------------------------------------- //
// Icons
// --------------------------------------------------------------------------- //

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}
// Three bouncing dots — the "…" working animation.
function WorkingDots() {
  return (
    <span className="inline-flex items-center gap-0.5" aria-hidden>
      <span className="size-1 animate-bounce rounded-full bg-current [animation-delay:-0.3s]" />
      <span className="size-1 animate-bounce rounded-full bg-current [animation-delay:-0.15s]" />
      <span className="size-1 animate-bounce rounded-full bg-current" />
    </span>
  );
}
const Up = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
);
const Plus = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14" /></svg>
);
const Stop = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
);
const Paperclip = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
);
const Globe = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18" /></svg>
);
const Sliders = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg>
);
const Mic = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8" /></svg>
);
const Speaker = () => (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M11 5 6 9H2v6h4l5 4z" /><path d="M15.5 8.5a5 5 0 0 1 0 7M19 6a9 9 0 0 1 0 12" /></svg>
);
const ThumbUp = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v11M2 12.5V19a2 2 0 0 0 2 2h13.5a2 2 0 0 0 2-1.6l1.3-6.5a1.5 1.5 0 0 0-1.5-1.8H14V6a2.5 2.5 0 0 0-2.5-2.5L7 10" /></svg>
);
const ThumbDown = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M17 14V3M22 11.5V5a2 2 0 0 0-2-2H6.5a2 2 0 0 0-2 1.6L3.2 11.1A1.5 1.5 0 0 0 4.7 13H10v5a2.5 2.5 0 0 0 2.5 2.5L17 14" /></svg>
);
const Pencil = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" /></svg>
);

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

// mlx_lm.server reports the model's full snapshot path as its id; show a clean
// name (the real id is still what we send in requests).
function prettyModel(id: string): string {
  if (!id) return id;
  if (id.toLowerCase().endsWith(".gguf")) return id.split("/").pop() || id;
  const m = id.match(/models--[^/]+--([^/]+)/);
  if (m) return m[1];
  return id.split("/").pop() || id;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 || n % 1000 === 0 ? 0 : 1)}K`;
  return String(n);
}

function stepVerb(name: string): string {
  if (name === "browser_fetch") return "Reading";
  if (name === "browser_search") return "Searching";
  if (name === "browser_open") return "Opening browser";
  if (name === "browser_snapshot") return "Scanning page";
  if (name === "browser_click") return "Clicking";
  if (name === "browser_click_selector") return "Clicking (selector)";
  if (name === "browser_type") return "Typing";
  if (name === "browser_read") return "Reading page";
  if (name === "browser_press") return "Pressing key";
  if (name === "browser_back") return "Going back";
  if (name === "browser_evaluate") return "Running page script";
  if (name === "browser_console_messages") return "Reading console";
  if (name === "browser_network_requests") return "Reading network";
  if (name === "browser_tabs") return "Listing tabs";
  if (name === "browser_tab_select") return "Switching tab";
  if (name === "browser_tab_close") return "Closing tab";
  if (name === "load_skill") return "Loading skill";
  if (name === "load_skill_resource") return "Loading resource";
  if (name.startsWith("mcp__")) return "MCP: " + name.slice(5).replace("__", " · ");
  return name;
}

// The compact target shown after a step's verb: the file/query/url it acted on,
// with the line range appended for ranged reads so paginated reads of the same
// file (query.md:40–80 vs :80–140) read as distinct instead of looking like dupes.
function stepTarget(s: ChatStep): string {
  const a = s.args;
  const base = String(a.query ?? a.url ?? a.slug ?? a.path ?? a.name ?? "");
  if (a.path && (a.start_line || a.end_line)) {
    return `${base}:${a.start_line ? Number(a.start_line) : ""}–${a.end_line ? Number(a.end_line) : ""}`;
  }
  return base;
}

// Heuristic: did a tool result signal failure? The read-only shell prefixes
// rejections with "refused"; other tools lead with an error/unavailable phrase.
// Used only to flag the step (✕ vs ✓) — the full text is still shown on expand.
function isErrorResult(r: string): boolean {
  const s = r.trimStart().toLowerCase();
  return s.startsWith("refused") || s.startsWith("error") || s.startsWith("failed")
    || s.startsWith("no linked wiki") || s.includes("is unavailable");
}

// Status glyph for a tool step: running → spinner, failed → ✕, else ✓.
function StepIcon({ step }: { step: ChatStep }) {
  if (!step.done) return <Spinner />;
  if (step.error) return <span className="text-danger" aria-label="failed">✕</span>;
  return <span className="text-success" aria-label="done">✓</span>;
}

// One tool call in the work timeline. Clickable (a nested <details>) when it has
// args or a result to reveal — the "View" affordance from the ChatGPT/Claude
// activity feed. Big results scroll inside their own box rather than flooding.
function ToolRow({ step }: { step: ChatStep }) {
  const argKeys = Object.keys(step.args).filter((k) => k !== "wiki" && step.args[k] != null && step.args[k] !== "");
  const hasDetail = !!step.result || argKeys.length > 0;
  const head = (
    <>
      <StepIcon step={step} />
      <span className="font-medium">{stepVerb(step.name)}</span>
      <span className="truncate font-mono text-mute">{stepTarget(step)}</span>
    </>
  );
  if (!hasDetail) return <div className="flex items-center gap-1.5 text-xs text-body">{head}</div>;
  return (
    <details className="text-xs text-body">
      <summary className="flex cursor-pointer items-center gap-1.5">
        {head}<span className="text-mute/60">· View</span>
      </summary>
      <div className="mt-1 space-y-1 pl-5">
        {argKeys.length ? (
          <pre className="overflow-x-auto rounded bg-base/60 p-1.5 font-mono text-[11px] text-mute">
            {argKeys.map((k) => `${k}: ${String(step.args[k])}`).join("  ")}
          </pre>
        ) : null}
        {step.result ? (
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-base/60 p-1.5 text-[11px] text-body">{step.result}</pre>
        ) : null}
      </div>
    </details>
  );
}

// A run of consecutive same-tool calls, collapsed to one "verb ×N" row so a
// paginated read of five ranges doesn't render as five near-identical lines.
// Expand to see each call (itself a ToolRow, so its output is one more click).
function ToolGroupRow({ steps }: { steps: ChatStep[] }) {
  const running = steps.some((s) => !s.done);
  const errored = steps.some((s) => s.error);
  const icon = running
    ? <Spinner />
    : errored
      ? <span className="text-danger" aria-label="some failed">✕</span>
      : <span className="text-success" aria-label="done">✓</span>;
  return (
    <details className="text-xs text-body">
      <summary className="flex cursor-pointer items-center gap-1.5">
        {icon}
        <span className="font-medium">{stepVerb(steps[0].name)}</span>
        <span className="text-mute">×{steps.length}</span>
        <span className="text-mute/60">· View</span>
      </summary>
      <div className="mt-1 space-y-1 pl-5">
        {steps.map((s, k) => <ToolRow key={k} step={s} />)}
      </div>
    </details>
  );
}

// Fold the flat timeline into render groups: narration runs pass through; a run
// of consecutive tool calls with the SAME name collapses into one ToolGroupRow.
type WorkGroup =
  | { kind: "text"; text: string; key: number }
  | { kind: "tool"; steps: ChatStep[]; key: number };
function groupTimeline(timeline: TimelineItem[]): WorkGroup[] {
  const out: WorkGroup[] = [];
  for (const t of timeline) {
    if (t.kind === "text") { out.push({ kind: "text", text: t.text, key: out.length }); continue; }
    const last = out[out.length - 1];
    if (last && last.kind === "tool" && last.steps[0].name === t.step.name) last.steps.push(t.step);
    else out.push({ kind: "tool", steps: [t.step], key: out.length });
  }
  return out;
}

// Typewriter pacing: the model/upstream deliver text in bursts (many tokens per
// SSE chunk, then a pause), so appending each burst straight to the DOM makes the
// answer appear in lumps. This decouples arrival from reveal — pushed text queues,
// and a rAF loop drains it at a steady, backlog-adaptive rate so the reveal feels
// like ChatGPT/Claude even when the whole answer arrives at once. `push` feeds the
// queue, `flush` reveals the remainder instantly (turn ended / a tool interrupts),
// `stop` drops the queue (aborted / narration folded away). Logic elsewhere uses
// the full accumulated string; this only paces what the eye sees.
function makeTyper(onChunk: (s: string) => void) {
  let pending = "";
  let raf = 0;
  let onEmpty: (() => void) | null = null;
  const settle = () => { if (onEmpty) { const f = onEmpty; onEmpty = null; f(); } };
  const tick = () => {
    raf = 0;
    if (!pending) { settle(); return; }
    // Reveal ~1/8 of the backlog each frame (min 2, cap 12 chars) — a 500-char
    // burst drains in ~0.7s; a slow trickle reveals near real-time.
    const n = Math.min(pending.length, Math.max(2, Math.min(12, Math.ceil(pending.length / 8))));
    onChunk(pending.slice(0, n));
    pending = pending.slice(n);
    if (pending) raf = requestAnimationFrame(tick); else settle();
  };
  return {
    push(s: string) { pending += s; if (!raf) raf = requestAnimationFrame(tick); },
    // Keep pacing to completion (used at turn end, so even an answer that arrived
    // as one final burst types out smoothly instead of snapping into place).
    drain(): Promise<void> {
      return new Promise((res) => {
        if (!pending) return res();
        onEmpty = res;
        if (!raf) raf = requestAnimationFrame(tick);
      });
    },
    flush() { if (raf) { cancelAnimationFrame(raf); raf = 0; } if (pending) { onChunk(pending); pending = ""; } settle(); },
    stop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } pending = ""; settle(); },
  };
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

// Markdown renderer for assistant + user turns. GFM extensions (tables, task
// lists, strikethrough, autolinks) are on. Every tag is mapped to a Tailwind
// class that references theme tokens, so dark mode re-skins automatically.
// Streaming-safe: react-markdown handles partial input; an unclosed ``` just
// renders as text until its closer arrives.
// The model doesn't always carry a tool's returned `/api/agents/.../files?path=`
// image link verbatim into its reply — asked to re-show a screenshot it may
// instead write the bare working-dir-relative path it saw in a file listing.
// Resolve any non-absolute image src against that route so it renders either way.
function resolveImageSrc(src: string | undefined, agentId?: string | null): string | undefined {
  if (!src || !agentId || /^(https?:|data:|blob:)/i.test(src) || src.startsWith("/")) return src;
  return `/api/agents/${encodeURIComponent(agentId)}/files?path=${encodeURIComponent(src)}`;
}

function RichText({ text, agentId, onImagePreview }: {
  text: string; agentId?: string | null; onImagePreview?: (src: string) => void;
}) {
  return (
    <div className="vw-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (p) => <h1 className="mb-2 mt-3 font-display text-xl font-semibold text-ink" {...p} />,
          h2: (p) => <h2 className="mb-2 mt-3 font-display text-lg font-semibold text-ink" {...p} />,
          h3: (p) => <h3 className="mb-1.5 mt-3 font-display text-base font-semibold text-ink" {...p} />,
          h4: (p) => <h4 className="mb-1 mt-2.5 text-sm font-semibold text-ink" {...p} />,
          h5: (p) => <h5 className="mb-1 mt-2 text-xs font-semibold uppercase tracking-wide text-ink" {...p} />,
          h6: (p) => <h6 className="mb-1 mt-2 text-xs font-semibold uppercase tracking-wide text-mute" {...p} />,
          p: (p) => <p className="my-2 leading-relaxed" {...p} />,
          strong: (p) => <strong className="font-semibold text-ink" {...p} />,
          em: (p) => <em className="italic" {...p} />,
          a: (p) => <a className="text-primary underline decoration-primary-line underline-offset-2 hover:text-primary-deep" target="_blank" rel="noreferrer" {...p} />,
          ul: (p) => <ul className="my-2 list-disc space-y-1 pl-5" {...p} />,
          ol: (p) => <ol className="my-2 list-decimal space-y-1 pl-5" {...p} />,
          li: (p) => <li className="leading-relaxed" {...p} />,
          blockquote: (p) => <blockquote className="my-2 border-l-4 border-primary-line pl-3 italic text-body" {...p} />,
          hr: (p) => <hr className="my-3 border-pressed" {...p} />,
          img: (p) => {
            const resolved = resolveImageSrc(p.src, agentId);
            return (
              <button
                type="button"
                onClick={() => resolved && onImagePreview?.(resolved)}
                className="my-2 block cursor-zoom-in"
                aria-label="Preview image"
              >
                <img className="max-w-full rounded-xl border border-line" loading="lazy"
                     {...p} src={resolved} />
              </button>
            );
          },
          table: (p) => (
            <div className="my-3 overflow-x-auto rounded-xl border border-line">
              <table className="min-w-full text-left text-[13px]" {...p} />
            </div>
          ),
          thead: (p) => <thead className="bg-soft" {...p} />,
          th: (p) => <th className="border-b border-line px-3 py-2 font-semibold text-ink" {...p} />,
          td: (p) => <td className="border-b border-line px-3 py-1.5 align-top text-body" {...p} />,
          code: ({ className, children, ...rest }) => {
            const inline = !/language-/.test(className ?? "");
            if (inline) {
              return (
                <code className="rounded bg-soft px-1.5 py-0.5 font-mono text-[0.9em] text-ink" {...rest}>
                  {children}
                </code>
              );
            }
            return <code className={className} {...rest}>{children}</code>;
          },
          pre: (p) => (
            <pre className="my-2 overflow-x-auto rounded-xl bg-panel p-3 font-mono text-[12px] leading-relaxed text-panel-ink" {...p} />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

// The folded work log for one assistant turn: narration + tool calls in order.
// Open while the turn is still running (so you watch it work), then auto-collapses
// the moment it finishes and the answer lands. The inner list auto-scrolls to the
// newest entry as it streams. Both are overridable — a manual toggle sticks.
function WorkPanel({ timeline, label, live, agentId, onImagePreview }: {
  timeline: TimelineItem[]; label: string; live: boolean; agentId?: string | null;
  onImagePreview?: (src: string) => void;
}) {
  // Collapsed by default — even while running. The summary shows the newest
  // activity as a live status line; expand to watch the full timeline.
  const [open, setOpen] = useState(false);
  const prevLive = useRef(live);
  const bodyRef = useRef<HTMLDivElement>(null);
  // Finished (live true → false): collapse to just the summary line.
  useEffect(() => {
    if (prevLive.current && !live) setOpen(false);
    prevLive.current = live;
  }, [live]);
  // Keep the latest entry in view while the log grows and the panel is open.
  useEffect(() => {
    if (open && live && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [timeline, open, live]);
  const groups = groupTimeline(timeline);
  // While running + collapsed, surface the newest activity (tool or narration)
  // as a live status line so the folded panel says what it's doing right now.
  const lastItem = live && !open ? timeline[timeline.length - 1] : undefined;
  return (
    <details
      className="mb-2 rounded-xl border border-pressed bg-softer/60 px-3 py-2"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer items-center gap-2 text-xs font-medium text-mute">
        <span aria-hidden>⚙️</span>
        {label}
        {lastItem ? (
          <span className="min-w-0 flex-1 truncate font-normal text-mute/80">
            · {lastItem.kind === "tool"
              ? <>{stepVerb(lastItem.step.name)} <span className="font-mono">{stepTarget(lastItem.step)}</span></>
              : lastItem.text}
          </span>
        ) : null}
        <span className="ml-auto shrink-0 font-normal text-mute/60">{open ? "collapse" : "expand"}</span>
      </summary>
      <div ref={bodyRef} className="mt-2 max-h-80 space-y-2 overflow-y-auto">
        {groups.map((g) => g.kind === "text" ? (
          <div key={g.key} className="text-[13px] leading-relaxed text-body">
            <RichText text={g.text} agentId={agentId} onImagePreview={onImagePreview} />
          </div>
        ) : g.steps.length === 1 ? (
          <ToolRow key={g.key} step={g.steps[0]} />
        ) : (
          <ToolGroupRow key={g.key} steps={g.steps} />
        ))}
      </div>
    </details>
  );
}

// Agentic stream (/api/chat): typed SSE events from the tool loop.
type AgentEvent =
  | { type: "run"; runId: string }
  | { type: "text"; delta: string }
  | { type: "text_reset" }
  | { type: "status"; message: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; result: string }
  | { type: "vision_status"; state: "reading_image" }
  | { type: "error"; message: string }
  | { type: "done"; outputTokens?: number; runId?: string; sessionId?: string };

async function streamAgent(body: object, sessionId: string, signal: AbortSignal, onEvent: (e: AgentEvent) => void): Promise<void> {
  const res = await authFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-vw-session": sessionId },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const evt = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of evt.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        try { onEvent(JSON.parse(t.slice(5).trim())); } catch { /* */ }
      }
    }
  }
}

async function readFile(file: File): Promise<Pending> {
  if (file.type.startsWith("image/")) {
    const dataUrl: string = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result as string);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
    return { kind: "image", name: file.name, dataUrl };
  }
  const text = await file.text();
  return { kind: "text", name: file.name, text };
}

// --------------------------------------------------------------------------- //
// Feedback bar — 👍 / 👎 / edit on an assistant turn. Posts to /api/feedback;
// the signals are listed under Analytics → feedback and export as JSONL. This is
// purely additive — it never touches the streaming/save path above.
// --------------------------------------------------------------------------- //

function FeedbackBar({ sessionId, agentId, model, userPrompt, original, turnIndex }: {
  sessionId: string | null; agentId: string | null; model: string;
  userPrompt: string; original: string; turnIndex: number;
}) {
  const [signal, setSignal] = useState<"up" | "down" | "edit" | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(original);
  const [saved, setSaved] = useState<string | null>(null);

  const post = useCallback(async (body: Record<string, unknown>) => {
    try {
      await apiJson("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId, agentId: agentId ?? undefined, model,
          messageId: `${sessionId ?? "chat"}:${turnIndex}`, turnIndex,
          userPrompt, original, ...body,
        }),
      });
      return true;
    } catch { return false; }
  }, [sessionId, agentId, model, userPrompt, original, turnIndex]);

  const rate = async (s: "up" | "down") => {
    setSignal(s);
    if (await post({ signal: s })) setSaved("Feedback saved");
  };
  const saveEdit = async () => {
    const corrected = draft.trim();
    if (!corrected) return;
    if (await post({ signal: "edit", correctedText: corrected })) {
      setSignal("edit"); setEditing(false); setSaved("Correction saved");
    }
  };

  const btn = (active: boolean) =>
    `grid size-7 place-items-center rounded-full border transition-colors ${
      active ? "border-primary bg-primary-soft text-primary-deep"
             : "border-pressed text-mute hover:bg-soft hover:text-ink"}`;

  if (editing) {
    return (
      <div className="mt-2 rounded-xl border border-primary-line bg-softer p-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={3}
          className="w-full resize-y rounded-lg border border-pressed bg-canvas px-2.5 py-2 text-[13px] text-ink outline-none focus:border-primary"
          placeholder="Write the corrected reply…"
        />
        <div className="mt-2 flex items-center gap-2">
          <button onClick={saveEdit} disabled={!draft.trim()}
            className="rounded-full bg-primary px-3 py-1 text-xs font-medium text-white hover:bg-primary-deep disabled:opacity-40">
            Save correction
          </button>
          <button onClick={() => { setEditing(false); setDraft(original); }}
            className="rounded-full border border-pressed px-3 py-1 text-xs text-body hover:bg-soft hover:text-ink">
            Cancel
          </button>
          <span className="text-[10px] text-mute">kept as a training example under Analytics</span>
        </div>
      </div>
    );
  }

  return (
    <div className="mt-2 flex items-center gap-1.5">
      <button onClick={() => rate("up")} className={btn(signal === "up")} title="Good reply"><ThumbUp /></button>
      <button onClick={() => rate("down")} className={btn(signal === "down")} title="Bad reply"><ThumbDown /></button>
      <button onClick={() => { setDraft(original); setEditing(true); }} className={btn(signal === "edit")} title="Edit this reply into a correction"><Pencil /></button>
      {saved && <span className="ml-1 text-[10px] text-success">✓ {saved}</span>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function PlaygroundPage({ openRequest, agentRequest, onSessionId, onSaved, onTrace }: ChatProps) {
  // `model` = the active local model id; discoverable remote providers additionally
  // get a per-conversation model picker below.
  const [model, setModel] = useState("");
  const [modelLabel, setModelLabel] = useState("");

  // Agents — the chat runs *as* one (persona + tools + skills). Default to the
  // last-used agent, else the seeded "Assistant".
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState<string | null>(() => localStorage.getItem(AGENT_KEY));
  const selectedAgent = agents.find((a) => a.id === agentId) || null;

  // When the agent runs on an external provider (BYOK / subscription) it ignores
  // the local active model — so the Chat should label it with the agent's own
  // model, and stays usable even when no local model is active.
  const externalAgent = !!selectedAgent
    && (selectedAgent.modelSource === "byok" || selectedAgent.modelSource === "subscription")
    && !!selectedAgent.provider;
  const modelPickerAgent = externalAgent
    && (selectedAgent?.provider === "openai-compatible" || selectedAgent?.provider === "opencode-go");
  const providerTag = externalAgent
    ? ({ openai: "ChatGPT", deepseek: "DeepSeek", anthropic: "Claude", "openai-compatible": "Custom gateway", "opencode-go": "OpenCode Go" }[selectedAgent!.provider] ?? selectedAgent!.provider)
    : null;
  const [customModels, setCustomModels] = useState<string[]>([]);
  const [customModel, setCustomModel] = useState("");
  const [loadingCustomModels, setLoadingCustomModels] = useState(false);
  // Non-picker external providers (Claude/ChatGPT/DeepSeek) always run on the
  // agent's saved model — never the local active model, which may be empty.
  const requestModel = modelPickerAgent
    ? (customModel || selectedAgent?.model || "")
    : externalAgent
      ? (selectedAgent?.model || "")
      : model;
  const effectiveLabel = externalAgent
    ? `${requestModel || "(no model set)"}${providerTag ? ` · ${providerTag}` : ""}`
    : modelLabel;
  const hasModel = externalAgent ? (!modelPickerAgent || !!requestModel) : !!modelLabel;

  const [system, setSystem] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [temperature, setTemperature] = useState(0.7);
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [searchOn, setSearchOn] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showAttachMenu, setShowAttachMenu] = useState(false);

  // Chat `/` skill picker. Typing "/" surfaces the active agent's enabled skills;
  // picking one stages it (chip) and preloads its full instructions on the next
  // send, so the model uses it without having to decide to call load_skill.
  const [showSkillMenu, setShowSkillMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [slashIdx, setSlashIdx] = useState(0);
  const [forcedSkills, setForcedSkills] = useState<string[]>([]);
  const [skillMeta, setSkillMeta] = useState<Record<string, { name: string; description: string; whenToUse: string }>>({});

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<Pending[]>([]);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [elapsed, setElapsed] = useState(0); // seconds the current turn has been running
  // Transient "reading the attached image" indicator (vision_status SSE event) —
  // shown while a text-only agent hands an image to its vision helper. Cleared
  // on the next text/tool/done/error event (or the run stopping/failing).
  const [readingImage, setReadingImage] = useState(false);
  const [retryStatus, setRetryStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Mid-run message queueing + steering: messages typed while a run is
  // streaming queue up here (no attachments — those still block on the run
  // finishing) instead of being dropped. Each entry can wait for the run to
  // finish (auto-sent as one combined turn) or be "steered" straight into the
  // live run via POST /api/chat/steer.
  const [queued, setQueued] = useState<QueuedEntry[]>([]);
  const nextQueuedId = () =>
    "q:" + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
  const runIdRef = useRef<string | undefined>(undefined);   // current run's id (from the "run" SSE event)
  const abortedRef = useRef(false);                          // true when the current run was Stop-aborted
  const erroredRef = useRef(false);                          // true when the current run ended in a real error (not Stop)
  const steeredRef = useRef<ChatMessage[]>([]);               // steered user messages delivered into this run
  const prevStreamingRef = useRef(false);                     // detects the streaming true→false transition

  // Tick an elapsed-seconds counter while a turn streams, so a long tool call
  // (e.g. a 50s PIS read) visibly keeps moving instead of looking frozen.
  useEffect(() => {
    if (!streaming) { setElapsed(0); return; }
    const start = Date.now();
    const t = setInterval(() => setElapsed(Math.round((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(t);
  }, [streaming]);

  useEffect(() => {
    if (!previewImage) return;
    const close = (e: KeyboardEvent) => { if (e.key === "Escape") setPreviewImage(null); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [previewImage]);

  // active session (list + selection live in the app sidebar; we own messages)
  const [sessionId, setSessionId] = useState<string | null>(null);
  const openNonce = useRef(0);

  // Active model's context window (tokens), from the runtime — powers the usage
  // meter and the ~80% auto-compact threshold. Null when unknown (external upstream).
  const [ctxWindow, setCtxWindow] = useState<number | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Voice (PRD-08) — local on-device STT (mic → composer) + TTS (read aloud).
  // Both controls hide when /api/voice/capability reports the backend absent,
  // so Chat stays fully usable keyboard-first with no voice models installed.
  const [voiceCap, setVoiceCap] = useState<{ stt: boolean; tts: boolean }>({ stt: false, tts: false });
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [speakingIdx, setSpeakingIdx] = useState<number | null>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    let alive = true;
    apiJson<{ sttAvailable: boolean; ttsAvailable: boolean }>("/api/voice/capability")
      .then((c) => { if (alive) setVoiceCap({ stt: !!c.sttAvailable, tts: !!c.ttsAvailable }); })
      .catch(() => { if (alive) setVoiceCap({ stt: false, tts: false }); });
    return () => { alive = false; };
  }, []);

  const loadModels = useCallback(async () => {
    // The runtime knows the single ACTIVE model. MLX's /v1/models lists several,
    // so it's NOT a reliable source — trust the runtime when it manages the
    // server, and only fall back to /v1/models for an external upstream.
    try {
      const st = await apiJson<{ managed: boolean; healthy: boolean; modelPath: string | null; modelName: string | null; ctx: number | null }>("/api/runtime/status");
      if (st.managed) {
        setModelLabel(st.modelName || "");
        setModel(st.healthy ? (st.modelPath || st.modelName || "") : "");
        setCtxWindow(st.healthy ? (st.ctx ?? null) : null);
        return;
      }
    } catch { /* not managed / endpoint missing → fall back */ }
    try {
      const data = await (await authFetch("/v1/models")).json();
      const id = data?.data?.[0]?.id ?? "";
      setModel(id);
      setModelLabel(id ? prettyModel(id) : "");
      setCtxWindow(null); // external upstream — window size unknown
    } catch { setModel(""); setModelLabel(""); setCtxWindow(null); }
  }, []);
  // Poll so the chat tracks the active model when you switch it in the Models tab.
  // An external-provider agent (BYOK / subscription) never uses the local model,
  // so skip the poll entirely — it was hitting /api/runtime/status every 4s (and
  // each of those fanned out to the local server) for nothing.
  useEffect(() => {
    if (externalAgent) {
      setModel(""); setModelLabel(""); setCtxWindow(null);
      return;
    }
    loadModels();
    const t = setInterval(loadModels, 4000);
    return () => clearInterval(t);
  }, [loadModels, externalAgent]);

  const loadCustomModels = useCallback(async (surfaceError = false) => {
    if (!modelPickerAgent || !selectedAgent?.provider) return;
    setLoadingCustomModels(true);
    try {
      const result = await apiJson<{ models: string[] }>(`/api/providers/${encodeURIComponent(selectedAgent.provider)}/models`);
      const discovered = result.models || [];
      setCustomModels(discovered);
      setCustomModel((current) => current || selectedAgent?.model || discovered[0] || "");
    } catch (e) {
      setCustomModels([]);
      if (surfaceError) setError(`Could not load gateway models: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoadingCustomModels(false);
    }
  }, [modelPickerAgent, selectedAgent?.model, selectedAgent?.provider]);

  // Start from the assistant's saved default, then discover the provider's full
  // list. The user's selection remains local to this chat, not the agent record.
  useEffect(() => {
    setCustomModel(modelPickerAgent ? (selectedAgent?.model || "") : "");
    setCustomModels([]);
    if (modelPickerAgent) loadCustomModels();
  }, [modelPickerAgent, selectedAgent?.id, selectedAgent?.model, loadCustomModels]);

  const customModelOptions = useMemo(() => {
    const current = customModel || selectedAgent?.model || "";
    return current && !customModels.includes(current) ? [current, ...customModels] : customModels;
  }, [customModels, customModel, selectedAgent?.model]);

  // Load agents; default the selection to the last-used (or first) agent.
  const loadAgents = useCallback(async () => {
    try {
      const { agents: list } = await apiJson<{ agents: Agent[] }>("/api/agents");
      setAgents(list);
      setAgentId((cur) => (cur && list.some((a) => a.id === cur) ? cur : list[0]?.id ?? null));
    } catch { /* */ }
  }, []);
  useEffect(() => { loadAgents(); }, [loadAgents]);

  // Skill metadata (slug → name/description) for the `/` picker labels.
  useEffect(() => {
    apiJson<{ skills: { slug: string; name: string; description: string; whenToUse: string }[] }>("/api/skills")
      .then((r) => {
        const m: Record<string, { name: string; description: string; whenToUse: string }> = {};
        for (const s of r.skills) m[s.slug] = { name: s.name, description: s.description, whenToUse: s.whenToUse };
        setSkillMeta(m);
      })
      .catch(() => { /* skills are optional */ });
  }, []);

  // Persist the choice; track the agent's preferred temperature on switch.
  useEffect(() => {
    if (agentId) localStorage.setItem(AGENT_KEY, agentId);
    const a = agents.find((x) => x.id === agentId);
    if (a) { setTemperature(a.temperature); setReasoningEffort(a.reasoningEffort ?? ""); }
  }, [agentId, agents]);

  // "Chat with this agent" from the Agents page selects that agent here.
  // Reload the agent list first: this page stays mounted, so a just-created
  // agent would otherwise be missing from the dropdown and the <select> would
  // fall back to showing the first option ("Assistant").
  const agentNonce = useRef(0);
  useEffect(() => {
    if (agentRequest.nonce === agentNonce.current) return;
    agentNonce.current = agentRequest.nonce;
    if (agentRequest.id) { loadAgents(); setAgentId(agentRequest.id); }
  }, [agentRequest, loadAgents]);

  // ---- session open/new driven by the sidebar (openRequest) ---- //
  useEffect(() => {
    if (openRequest.nonce === openNonce.current) return;
    openNonce.current = openRequest.nonce;
    if (openRequest.nonce === 0) return; // initial empty state, nothing to open

    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setError(null); setNotice(null); setInput("");

    if (openRequest.id) {
      apiJson<{ messages: ChatMessage[]; agentId?: string | null }>(`/api/sessions/${encodeURIComponent(openRequest.id)}`)
        .then((s) => {
          setMessages(s.messages || []); setSessionId(openRequest.id); onSessionId(openRequest.id);
          if (s.agentId) setAgentId(s.agentId);
        })
        .catch(() => { setMessages([]); setSessionId(null); onSessionId(null); });
    } else {
      setMessages([]); setSessionId(null); onSessionId(null);
    }
  }, [openRequest, onSessionId]);

  // ---- inbox flow-back: a light poll while this session is open, so a
  // handoff landing in the currently-open session (another agent closing a
  // task back to this one) shows up without a manual reload. Paused while
  // streaming or the tab is hidden; only ever appends — never replaces. ----
  const streamingRef = useRef(streaming);
  useEffect(() => { streamingRef.current = streaming; }, [streaming]);
  const sessionIdRef = useRef(sessionId);
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  useEffect(() => {
    if (!sessionId) return;
    const tick = async () => {
      if (streamingRef.current || document.visibilityState !== "visible") return;
      const sid = sessionId;
      try {
        const s = await apiJson<{ messages: ChatMessage[]; agentId?: string | null }>(`/api/sessions/${encodeURIComponent(sid)}`);
        // Re-check after the await: a run may have started, or the open session
        // may have changed, while the request was in flight.
        if (streamingRef.current || sessionIdRef.current !== sid) return;
        const incoming = s.messages || [];
        setMessages((prev) => (incoming.length > prev.length ? [...prev, ...incoming.slice(prev.length)] : prev));
      } catch { /* transient — the next tick retries */ }
    };
    const iv = setInterval(tick, 5000);
    return () => clearInterval(iv);
  }, [sessionId]);

  const saveCurrent = useCallback(async (msgs: ChatMessage[], id: string) => {
    const firstUser = msgs.find((m) => m.role === "user")?.content || "New chat";
    try {
      await apiJson(`/api/sessions/${encodeURIComponent(id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: firstUser.slice(0, 80), model: requestModel, agentId, messages: msgs }),
      });
      onSaved?.();
    } catch { /* */ }
  }, [requestModel, agentId, onSaved]);

  // Rough token estimate for the usage meter (~4 chars/token, matching the
  // backend's estimate_tokens). Cheap; recomputed as the conversation grows.
  const estTokens = useMemo(
    () => Math.ceil(messages.reduce((n, m) => n + (m.content?.length ?? 0), 0) / 4),
    [messages],
  );
  const ctxPct = ctxWindow ? Math.min(100, Math.round((estTokens / ctxWindow) * 100)) : null;

  // Summarize the older turns into one compact message: POST /api/chat/compact,
  // then rebuild [summary, ...last keepRecent]. Returns null when there's too
  // little history to bother. Shared by manual /compact and the auto-compact.
  const runCompact = useCallback(async (msgs: ChatMessage[]): Promise<ChatMessage[] | null> => {
    const res = await apiJson<{ summary: string | null; keptRecent: number }>("/api/chat/compact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: msgs.map((h) => ({ role: h.role, content: h.content, images: h.images })),
        model: requestModel, agentId: agentId ?? undefined, keepRecent: 4,
      }),
    });
    if (!res.summary) return null;
    const tail = res.keptRecent ? msgs.slice(-res.keptRecent) : [];
    return [{ role: "assistant", content: res.summary, compacted: true }, ...tail];
  }, [requestModel, agentId]);

  const doCompact = useCallback(async () => {
    if (streaming || messages.length < 3) { setNotice("Nothing to compact yet."); return; }
    setNotice("Compacting the conversation…");
    try {
      const next = await runCompact(messages);
      if (!next) { setNotice("Not enough history to compact yet."); return; }
      setMessages(next);
      if (sessionId) saveCurrent(next, sessionId);
      setNotice("Compacted earlier turns into a summary.");
    } catch { setNotice("Compaction failed — try again."); }
  }, [streaming, messages, runCompact, sessionId, saveCurrent]);

  const doClear = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setMessages([]); setSessionId(null); onSessionId(null);
    setInput(""); setError(null);
    setNotice("Started a fresh chat. The previous one is saved on the agent's Conversations tab.");
  }, [onSessionId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streaming]);

  // auto-grow the textarea
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setReadingImage(false);
  }, []);

  const addFiles = useCallback(async (files: FileList | File[] | null) => {
    if (!files?.length) return;
    const read = await Promise.all(Array.from(files).map(readFile));
    setPending((p) => [...p, ...read]);
  }, []);

  // ---- Voice: push-to-talk dictation (records → /api/voice/transcribe) ---- //
  const startRecording = useCallback(async () => {
    if (recording || transcribing) return;
    // Barge-in: starting to talk halts any reply being read aloud (FR-7).
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; setSpeakingIdx(null); }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      setError("Microphone unavailable: " + (err instanceof Error ? err.message : String(err)));
      return;
    }
    const mr = new MediaRecorder(stream);
    chunksRef.current = [];
    mr.ondataavailable = (e) => { if (e.data.size) chunksRef.current.push(e.data); };
    mr.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      setRecording(false);
      const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
      if (!blob.size) return;
      setTranscribing(true);
      try {
        const fd = new FormData();
        fd.append("file", blob, "speech.webm");
        const res = await authFetch("/api/voice/transcribe", { method: "POST", body: fd });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const t = (data.text || "").trim();
        if (t) setInput((prev) => (prev ? prev.trimEnd() + " " : "") + t);
      } catch (err) {
        setError("Transcription failed: " + (err instanceof Error ? err.message : String(err)));
      } finally {
        setTranscribing(false);
      }
    };
    mediaRef.current = mr;
    mr.start();
    setRecording(true);
  }, [recording, transcribing]);

  const toggleMic = useCallback(() => {
    if (recording) { mediaRef.current?.stop(); mediaRef.current = null; }
    else startRecording();
  }, [recording, startRecording]);

  // ---- Voice: read an assistant reply aloud (text → /api/voice/speak) ---- //
  const speak = useCallback(async (text: string, idx: number) => {
    // Barge-in: any new playback first halts the current one.
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
    if (speakingIdx === idx) { setSpeakingIdx(null); return; }  // toggle off
    const prose = text.trim();
    if (!prose) return;
    try {
      const res = await authFetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: prose }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const url = URL.createObjectURL(await res.blob());
      const audio = new Audio(url);
      audioRef.current = audio;
      setSpeakingIdx(idx);
      const clear = () => { URL.revokeObjectURL(url); if (audioRef.current === audio) audioRef.current = null; setSpeakingIdx((c) => (c === idx ? null : c)); };
      audio.onended = clear;
      audio.onerror = clear;
      await audio.play();
    } catch (err) {
      setError("Speech failed: " + (err instanceof Error ? err.message : String(err)));
      setSpeakingIdx(null);
    }
  }, [speakingIdx]);

  // `override` lets the auto-send-on-completion effect (step E) pass a combined
  // string without depending on the async `input` state. send() is also wired
  // directly as a DOM event handler (onClick/onKeyDown), which would pass a
  // MouseEvent/KeyboardEvent here — only treat it as text when it's a string.
  const send = useCallback(async (override?: unknown) => {
    const overrideText = typeof override === "string" ? override : undefined;
    const text = (overrideText ?? input).trim();
    if (streaming) {
      // A run is already in flight. Attachments still block (the user waits —
      // unchanged behavior); plain text queues up and is either auto-sent when
      // the run finishes naturally, or steered into the live run on demand.
      if (text && pending.length === 0) {
        setQueued((q) => [...q, { id: nextQueuedId(), content: text }]);
        if (overrideText === undefined) setInput("");
      }
      return;
    }
    if (!text && pending.length === 0 && forcedSkills.length === 0) return;
    // BYOK / subscription agents run on a remote provider, so they don't need a
    // local active model — only block when there's nothing to run on at all.
    if (!hasModel) { setError("No model loaded — activate one in Hub."); return; }
    setError(null);
    abortedRef.current = false;
    erroredRef.current = false;
    steeredRef.current = [];
    runIdRef.current = undefined;

    const textBlocks = pending.filter((p) => p.kind === "text")
      .map((p) => `\n\n----- ${p.name} -----\n${p.text}`).join("");
    const images = pending.filter((p) => p.kind === "image").map((p) => p.dataUrl!);
    // A skill staged via `/` with no typed text still sends a clear instruction;
    // the same names tag the turn so the UI can show which skill(s) were active.
    const turnSkillNames = forcedSkills.map((s) => skillMeta[s]?.name ?? s);
    const fallback = turnSkillNames.length ? `Use the ${turnSkillNames.join(", ")} skill.` : "(see attached)";
    const userMsg: ChatMessage = {
      role: "user",
      content: (text + textBlocks).trim() || fallback,
      images: images.length ? images : undefined,
    };

    const sid = sessionId || newSessionId();
    if (!sessionId) { setSessionId(sid); onSessionId(sid); }

    // Auto-compact: if the running history is past ~80% of the model's window,
    // fold the older turns into a summary before adding the new message, so the
    // turn fits instead of erroring out. Only when there's real history to fold.
    let baseMessages = messages;
    if (ctxWindow && messages.length >= 6 && estTokens > 0.8 * ctxWindow) {
      try {
        const compacted = await runCompact(messages);
        if (compacted) {
          baseMessages = compacted;
          setMessages(compacted);
          setNotice("Compacted earlier turns to fit the context window.");
        }
      } catch { /* couldn't compact — proceed and let the backend trim */ }
    }

    const history = [...baseMessages, userMsg];
    setMessages([...history, { role: "assistant", content: "" }]);
    setInput("");
    setPending([]);
    setForcedSkills([]);
    setShowSkillMenu(false);
    setStreaming(true);
    setNotice(null);
    setRetryStatus(null);
    setReadingImage(false);

    const controller = new AbortController();
    abortRef.current = controller;

    // Accumulate the assistant turn locally (for a deterministic save).
    let asstContent = "";
    let asstTimeline: TimelineItem[] = []; // ordered narration + tool calls before the final answer
    let asstSteps: ChatStep[] = [];
    let asstProposalIds: string[] = [];
    let asstTokens = 0;
    let asstRunId: string | undefined;
    const t0 = Date.now();
    const patchAssistant = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") next[next.length - 1] = fn(last);
        return next;
      });
    // Paces the visible answer; `asstContent` (below) stays the full source of truth.
    const typer = makeTyper((chunk) => patchAssistant((m) => ({ ...m, content: m.content + chunk })));
    const computeMeta = (): ChatMessage["meta"] => {
      const ms = Date.now() - t0;
      // Skill-loading tools are represented by the skill badge, not the tool list.
      const tools = [...new Set(asstSteps.map((s) => s.name))]
        .filter((n) => n !== "load_skill" && n !== "load_skill_resource");
      // Skills active this turn: preloaded via `/` plus any the model loaded itself.
      const loaded = asstSteps
        .filter((s) => s.name === "load_skill")
        .map((s) => {
          const a = s.args as Record<string, unknown>;
          const slug = String(a?.slug ?? a?.name ?? "");
          return skillMeta[slug]?.name ?? slug;
        })
        .filter(Boolean);
      const skills = [...new Set([...turnSkillNames, ...loaded])];
      return {
        ms,
        tokens: asstTokens || undefined,
        tokSec: asstTokens && ms > 0 ? Math.round((asstTokens / ms) * 1000 * 10) / 10 : undefined,
        tools: tools.length ? tools : undefined,
        skills: skills.length ? skills : undefined,
        runId: asstRunId,
      };
    };

    try {
      // The chat always runs as the selected agent (persona + tools + skills).
      // Search forces the web tools on top of the agent's own tools for this turn.
      const toolOverride = searchOn
        ? Array.from(new Set([...(selectedAgent?.tools ?? []), "browser_search", "browser_fetch"]))
        : undefined;
      const body: Record<string, unknown> = {
        model: requestModel, temperature,
        reasoningEffort: reasoningEffort || undefined,
        agentId: agentId ?? undefined,
        messages: history.map((h) => ({ role: h.role, content: h.content, images: h.images })),
      };
      if (system.trim()) body.system = system.trim();
      if (toolOverride) body.tools = toolOverride;
      if (forcedSkills.length) body.preloadSkills = forcedSkills;

      await streamAgent(body, sid, controller.signal, (e) => {
        if (e.type === "run") {
          runIdRef.current = e.runId;
        } else if (e.type === "vision_status") {
          if (e.state === "reading_image") setReadingImage(true);
        } else if (e.type === "text") {
          setReadingImage(false);
          setRetryStatus(null);
          asstContent += e.delta;      // full logical text (for freeze + save)
          typer.push(e.delta);         // paced reveal into the visible content
        } else if (e.type === "text_reset") {
          typer.stop();
          asstContent = "";
          patchAssistant((m) => ({ ...m, content: "" }));
        } else if (e.type === "status") {
          setRetryStatus(e.message);
        } else if (e.type === "tool_call") {
          setReadingImage(false);
          // Any text streamed since the last tool call was narration, not the
          // final answer: freeze it as a timeline text run and clear `content`
          // so only the text after the LAST tool remains as the visible answer.
          typer.stop();                // drop unrevealed narration — it's about to be folded away
          if (asstContent.trim()) asstTimeline = [...asstTimeline, { kind: "text", text: asstContent }];
          asstContent = "";
          // One shared step object lives in both `asstSteps` (flat, for meta) and
          // the timeline (ordered), so marking it done on tool_result updates both.
          const step: ChatStep = { name: e.name, args: e.args, done: false };
          asstSteps = [...asstSteps, step];
          asstTimeline = [...asstTimeline, { kind: "tool", step }];
          patchAssistant((m) => ({ ...m, timeline: [...asstTimeline], content: "", steps: [...asstSteps] }));
        } else if (e.type === "tool_result") {
          setReadingImage(false);
          for (let i = asstSteps.length - 1; i >= 0; i--) {
            if (asstSteps[i].name === e.name && !asstSteps[i].done) { asstSteps[i].done = true; asstSteps[i].result = e.result; asstSteps[i].error = isErrorResult(e.result); break; }
          }
          // A tool that filed an approval proposal returns an object carrying a
          // string proposalId — surface it as an inline card in this bubble.
          try {
            const parsed = JSON.parse(e.result) as unknown;
            if (parsed && typeof parsed === "object" && typeof (parsed as { proposalId?: unknown }).proposalId === "string") {
              const pid = (parsed as { proposalId: string }).proposalId;
              if (!asstProposalIds.includes(pid)) {
                asstProposalIds = [...asstProposalIds, pid];
                patchAssistant((m) => ({ ...m, proposalIds: [...(m.proposalIds ?? []), pid] }));
              }
            }
          } catch { /* result wasn't JSON — nothing to surface */ }
          // The step object was mutated in place above; it's shared with the
          // timeline, so fresh array copies re-render both from the same source.
          patchAssistant((m) => ({ ...m, steps: [...asstSteps], timeline: [...asstTimeline] }));
        } else if (e.type === "done") {
          setReadingImage(false);
          setRetryStatus(null);
          if (e.outputTokens) asstTokens = e.outputTokens;
          if (e.runId) asstRunId = e.runId;
        } else if (e.type === "error") {
          setReadingImage(false);
          setRetryStatus(null);
          throw new Error(e.message);
        }
      });
      await typer.drain(); // let the answer finish typing (paced) before the panel collapses + save
      // success — compute timing + token rate, persist
      const meta = computeMeta();
      patchAssistant((m) => ({ ...m, meta }));
      const finalMsgs: ChatMessage[] = [...history, ...steeredRef.current, {
        role: "assistant", content: asstContent,
        timeline: asstTimeline.length ? asstTimeline : undefined,
        steps: asstSteps.length ? asstSteps : undefined,
        proposalIds: asstProposalIds.length ? asstProposalIds : undefined,
        meta,
      }];
      saveCurrent(finalMsgs, sid);
    } catch (e) {
      setReadingImage(false);
      if ((e as Error)?.name === "AbortError") { abortedRef.current = true; /* user stopped */ }
      else {
        erroredRef.current = true; // a real error ended the run — never auto-fire the queue into it
        const msg = e instanceof Error ? e.message : String(e);
        // context window exceeded -> auto-start a fresh session, keep the old one saved
        if (/exceed.*context|context.*(size|window|length)|too many tokens/i.test(msg)) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            return last?.role === "assistant" && !last.content ? prev.slice(0, -1) : prev;
          });
          setMessages([]);
          setSessionId(null);
          onSessionId(null);
          setInput(text); // let them resend
          setNotice("Previous chat reached the model's context limit — started a new session. Your old chat is saved on the agent's Conversations tab.");
          onSaved?.();
        } else if (asstContent) {
          // The stream errored *after* delivering an answer (e.g. the model
          // server closed the connection at the end). Keep the answer + its
          // timing/tokens so the turn isn't lost, and still surface the error.
          typer.flush(); // reveal whatever was still paced out
          const meta = computeMeta();
          patchAssistant((m) => ({ ...m, meta }));
          saveCurrent([...history, ...steeredRef.current, {
            role: "assistant", content: asstContent,
            timeline: asstTimeline.length ? asstTimeline : undefined,
            steps: asstSteps.length ? asstSteps : undefined, meta,
          }], sid);
          setError(msg);
        } else {
          // no content streamed — drop the empty bubble and show the error
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            return last?.role === "assistant" && !last.content ? prev.slice(0, -1) : prev;
          });
          // A steer can have been delivered before the error hit (it landed in
          // steeredRef + the transcript already) — this branch otherwise never
          // saves, so persist it now or a delivered:true steer is lost on reload.
          if (steeredRef.current.length > 0) saveCurrent([...history, ...steeredRef.current], sid);
          setError(msg);
        }
      }
    } finally {
      typer.stop(); // cancel any pending reveal frame (e.g. on abort)
      setRetryStatus(null);
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, pending, streaming, requestModel, messages, system, temperature, reasoningEffort, searchOn, selectedAgent, agentId, sessionId, forcedSkills, skillMeta, saveCurrent, onSessionId, onSaved, ctxWindow, estTokens, runCompact]);

  // Auto-send the queue exactly once, when a run finishes NATURALLY (not via
  // Stop). Detected on the streaming true→false transition — the ref (rather
  // than a state var) means the effect doesn't need to run on every render,
  // and guards against a double-fire.
  useEffect(() => {
    const wasStreaming = prevStreamingRef.current;
    prevStreamingRef.current = streaming;
    if (!wasStreaming || streaming) return; // only a true→false transition
    if (abortedRef.current) return;         // Stop was pressed — leave the queue intact
    if (erroredRef.current) return;         // the run ended in a real error — leave the queue intact
    if (queued.length === 0) return;
    const combined = queued.map((e) => e.content).join("\n\n");
    setQueued([]);
    void send(combined);
  }, [streaming, queued, send]);

  // "Steer now" — inject one queued message into the run that's live right
  // now instead of waiting for it to finish. On success it also lands in the
  // transcript (before the in-progress assistant bubble) and is spliced into
  // the eventually-saved history (steeredRef; send() doesn't know about it).
  const steerNow = useCallback(async (entry: QueuedEntry) => {
    const runId = runIdRef.current;
    if (!runId) {
      setNotice("No active run to steer right now — it'll send once you hit enter.");
      return;
    }
    try {
      const res = await apiPost<{ delivered: boolean }>("/api/chat/steer", { runId, content: entry.content });
      if (res.delivered) {
        setQueued((q) => q.filter((e) => e.id !== entry.id));
        steeredRef.current = [...steeredRef.current, { role: "user", content: entry.content }];
        setMessages((prev) => {
          const next = [...prev];
          const asst = next.pop();
          return asst && asst.role === "assistant"
            ? [...next, { role: "user", content: entry.content }, asst]
            : [...prev, { role: "user", content: entry.content }];
        });
      } else {
        setNotice("That run already finished — kept it queued.");
      }
    } catch {
      setNotice("That run already finished — kept it queued.");
    }
  }, []);

  const removeQueued = useCallback((id: string) => {
    setQueued((q) => q.filter((e) => e.id !== id));
  }, []);

  // "Edit" — pull a queued entry back out of the queue and into the composer
  // for reworking. A non-empty draft is preserved underneath (recalled content
  // first, draft after), rather than clobbered.
  const editQueued = useCallback((id: string) => {
    setQueued((q) => {
      const entry = q.find((e) => e.id === id);
      if (!entry) return q;
      setInput((prev) => (prev.trim() ? `${entry.content}\n${prev}` : entry.content));
      return q.filter((e) => e.id !== id);
    });
    taRef.current?.focus();
  }, []);

  // `/` picker — built-in commands (/compact, /clear) plus the active agent's
  // enabled skills, filtered by what the user typed after the slash.
  const agentSkillSlugs = selectedAgent?.skills ?? [];
  const slashItems: SlashItem[] = showSkillMenu
    ? [
        ...SLASH_COMMANDS
          .filter((c) => { const q = slashQuery.toLowerCase(); return !q || c.id.includes(q); })
          .map((c) => ({ kind: "command" as const, id: c.id, name: c.name, description: c.description })),
        ...agentSkillSlugs
          .map((slug) => ({ slug, ...(skillMeta[slug] ?? { name: slug, description: "", whenToUse: "" }) }))
          .filter((s) => !forcedSkills.includes(s.slug))
          .filter((s) => {
            const q = slashQuery.toLowerCase();
            return !q || s.slug.toLowerCase().includes(q) || s.name.toLowerCase().includes(q);
          })
          .map((s) => ({ kind: "skill" as const, id: s.slug, name: s.name, description: s.description })),
      ]
    : [];

  const pickItem = (it: SlashItem) => {
    setShowSkillMenu(false); setSlashQuery(""); setSlashIdx(0);
    setInput((v) => v.replace(/^\/\S*/, "")); // drop the "/query" token, keep the rest
    if (it.kind === "command") {
      if (it.id === "clear") doClear();
      else if (it.id === "compact") void doCompact();
      return;
    }
    setForcedSkills((arr) => (arr.includes(it.id) ? arr : [...arr, it.id]));
    taRef.current?.focus();
  };

  const onInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value;
    setInput(v);
    // Slash menu while "/" is the leading token and no space has been typed. Always
    // available (built-in commands work even with no skills attached).
    const m = /^\/(\S*)$/.exec(v);
    if (m) { setShowSkillMenu(true); setSlashQuery(m[1]); setSlashIdx(0); }
    else setShowSkillMenu(false);
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSkillMenu && slashItems.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); setSlashIdx((i) => (i + 1) % slashItems.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSlashIdx((i) => (i - 1 + slashItems.length) % slashItems.length); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickItem(slashItems[Math.min(slashIdx, slashItems.length - 1)]); return; }
      if (e.key === "Escape") { e.preventDefault(); setShowSkillMenu(false); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const empty = messages.length === 0;

  // The composer is shared between the empty (centered) and active layouts.
  const composer = (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        const images = Array.from(e.dataTransfer.files).filter((file) => file.type.startsWith("image/"));
        if (images.length) void addFiles(images);
      }}
      className="rounded-[24px] border border-line bg-canvas p-2.5 shadow-[0_10px_30px_-16px_rgba(40,51,92,0.28)]"
    >
      {/* attachments */}
      {pending.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2 px-1">
          {pending.map((p, i) => (
            <span key={i} className={`relative flex items-center border border-pressed bg-softer text-xs text-body ${
              p.kind === "image" ? "overflow-hidden rounded-xl" : "gap-1.5 rounded-full px-2.5 py-1"
            }`}>
              {p.kind === "image"
                ? <button onClick={() => setPreviewImage(p.dataUrl!)} aria-label={`Preview ${p.name}`}>
                    <img src={p.dataUrl} alt={p.name} className="h-20 w-20 object-cover" />
                  </button>
                : <><Paperclip />{p.name}</>}
              <button
                onClick={() => setPending((arr) => arr.filter((_, j) => j !== i))}
                aria-label={`Remove ${p.name}`}
                className={p.kind === "image"
                  ? "absolute right-1 top-1 grid size-5 place-items-center rounded-full bg-panel/80 text-panel-ink hover:bg-panel"
                  : "text-mute hover:text-ink"}
              >✕</button>
            </span>
          ))}
        </div>
      )}

      {/* staged skills (from the `/` picker) — preloaded on the next send */}
      {forcedSkills.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2 px-1">
          {forcedSkills.map((slug) => (
            <span key={slug} title="This skill's full instructions are sent with your next message"
              className="flex items-center gap-1.5 rounded-full border border-primary-line bg-primary-soft px-2.5 py-1 text-xs font-medium text-primary-deep">
              <span className="font-mono">/</span>{skillMeta[slug]?.name ?? slug}
              <button onClick={() => setForcedSkills((arr) => arr.filter((s) => s !== slug))} className="text-primary-deep/60 hover:text-primary-deep">✕</button>
            </span>
          ))}
        </div>
      )}

      {showSystem && (
        <textarea
          value={system}
          onChange={(e) => setSystem(e.target.value)}
          placeholder="System prompt (optional)…"
          rows={2}
          className="mb-2 w-full resize-y rounded-2xl border border-pressed bg-softer px-3 py-2 font-mono text-xs text-ink outline-none placeholder:text-mute focus:border-primary-line"
        />
      )}

      {/* queued mid-run messages — sent as one turn once the reply finishes,
          or steered straight into the live run */}
      <QueuedMessages items={queued} onSteer={steerNow} onEdit={editQueued} onRemove={removeQueued} />

      <div className="relative">
        {showSkillMenu && slashItems.length > 0 && (
          <div className="absolute bottom-full left-0 z-20 mb-1 max-h-64 w-full overflow-auto rounded-2xl border border-line bg-canvas p-1.5 shadow-lg">
            <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-mute">
              Commands &amp; skills
            </div>
            {slashItems.map((s, i) => (
              <button key={`${s.kind}:${s.id}`}
                onMouseEnter={() => setSlashIdx(i)}
                onMouseDown={(e) => { e.preventDefault(); pickItem(s); }}
                className={`flex w-full flex-col items-start gap-0.5 rounded-xl px-3 py-2 text-left ${
                  i === slashIdx ? "bg-primary-soft text-primary-deep" : "text-body hover:bg-soft"}`}>
                <span className="text-sm font-medium">
                  {s.kind === "command"
                    ? <span className="font-mono">{s.name}</span>
                    : <>{s.name} <span className="font-mono text-[11px] text-mute">/{s.id}</span></>}
                </span>
                {s.description && <span className="line-clamp-1 text-xs text-mute">{s.description}</span>}
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          value={input}
          onChange={onInput}
          onKeyDown={onKey}
          onPaste={(e) => {
            const images = Array.from(e.clipboardData.items).flatMap((item) => {
              const file = item.type.startsWith("image/") ? item.getAsFile() : null;
              return file ? [file] : [];
            });
            if (images.length) { e.preventDefault(); void addFiles(images); }
          }}
          onBlur={() => setTimeout(() => setShowSkillMenu(false), 120)}
          placeholder="Ask anything  ·  / for commands & skills"
          rows={1}
          className="max-h-[200px] w-full resize-none bg-transparent px-3 py-2 text-[15px] text-body outline-none placeholder:text-mute"
        />
      </div>

      {/* footer */}
      <div className="flex items-center gap-2 px-1">
        {/* attach menu */}
        <div className="relative">
          <button
            onClick={() => { setShowAttachMenu((v) => !v); setShowSettings(false); }}
            className="grid size-8 place-items-center rounded-full border border-pressed text-body hover:bg-soft hover:text-ink"
            title="Add"
          ><Plus /></button>
          {showAttachMenu && (
            <div className="absolute bottom-10 left-0 z-10 w-52 rounded-2xl border border-line bg-canvas p-1.5 shadow-lg">
              <button
                onClick={() => { fileRef.current?.click(); setShowAttachMenu(false); }}
                className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-body hover:bg-primary-soft hover:text-primary-deep"
              ><Paperclip /> Add photos &amp; files</button>
              <button
                onClick={() => { setShowSystem((v) => !v); setShowAttachMenu(false); }}
                className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-body hover:bg-primary-soft hover:text-primary-deep"
              ><Sliders /> System prompt {system ? "●" : ""}</button>
            </div>
          )}
        </div>

        {/* web search toggle */}
        <button
          onClick={() => setSearchOn((v) => !v)}
          title="Let the model search & read the web to answer"
          className={`flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors ${
            searchOn ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft hover:text-ink"
          }`}
        ><Globe /> Search</button>

        {/* settings (temperature) */}
        <div className="relative">
          <button
            onClick={() => { setShowSettings((v) => !v); setShowAttachMenu(false); }}
            className="flex h-8 items-center gap-1.5 rounded-full border border-pressed px-3 text-xs text-body hover:bg-soft hover:text-ink"
          ><Sliders /> {reasoningEffort ? `effort ${reasoningEffort}` : `temp ${temperature.toFixed(1)}`}</button>
          {showSettings && (
            <div className="absolute bottom-10 left-0 z-10 w-60 rounded-2xl border border-line bg-canvas p-3 shadow-lg">
              <label className="flex items-center gap-2 text-xs text-body">
                Temperature
                <input type="range" min={0} max={2} step={0.1} value={temperature}
                  onChange={(e) => setTemperature(parseFloat(e.target.value))} className="flex-1 accent-primary" />
                <span className="w-6 text-right text-ink">{temperature.toFixed(1)}</span>
              </label>
              <label className="mt-3 flex items-center gap-2 text-xs text-body">
                Effort
                <select value={reasoningEffort} onChange={(e) => setReasoningEffort(e.target.value)}
                  className="flex-1 rounded-lg border border-pressed bg-canvas px-2 py-1 text-xs text-ink outline-none focus:border-primary">
                  <option value="">Model default</option>
                  <option value="off">off — no thinking</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="xhigh">xhigh</option>
                </select>
              </label>
              {reasoningEffort && (
                <p className="mt-2 text-[11px] leading-snug text-mute">
                  External reasoning models use effort instead of temperature —
                  temp is dropped from those requests. Local models ignore effort.
                </p>
              )}
            </div>
          )}
        </div>

        {/* push-to-talk mic — local STT; hidden when no backend (PRD-08) */}
        {voiceCap.stt && (
          <button
            onClick={toggleMic}
            disabled={transcribing}
            title={recording ? "Stop & transcribe" : transcribing ? "Transcribing…" : "Dictate (push to talk)"}
            aria-label={recording ? "Stop recording" : "Start voice dictation"}
            aria-pressed={recording}
            className={`grid size-8 place-items-center rounded-full border transition-colors disabled:opacity-50 ${
              recording
                ? "border-primary bg-primary text-white animate-pulse"
                : "border-pressed text-body hover:bg-soft hover:text-ink"
            }`}
          >{transcribing ? <Spinner /> : <Mic />}</button>
        )}

        {/* context usage meter + manual compact — only when we know the window */}
        {ctxWindow && messages.length > 0 && (
          <div className="ml-auto flex items-center gap-1.5">
            <span
              title={`About ${estTokens.toLocaleString()} of ${ctxWindow.toLocaleString()} tokens used (${ctxPct}%). Auto-compacts near 80%.`}
              className={`rounded-full px-2.5 py-1 font-mono text-[11px] ${
                ctxPct != null && ctxPct >= 90 ? "bg-danger-soft text-danger"
                  : ctxPct != null && ctxPct >= 70 ? "bg-warn-soft text-warn"
                  : "bg-soft text-mute"}`}
            >{fmtTokens(estTokens)}/{fmtTokens(ctxWindow)} · {ctxPct}%</span>
            <button
              onClick={doCompact}
              disabled={streaming || messages.length < 3}
              title="Summarize older turns to shrink the context (/compact)"
              className="rounded-full border border-pressed px-2.5 py-1 text-[11px] text-body hover:bg-soft hover:text-ink disabled:opacity-40"
            >Compact</button>
          </div>
        )}

        {/* model — discoverable remote providers may expose several models for this chat only */}
        {modelPickerAgent ? (
          <div className={`${ctxWindow && messages.length > 0 ? "" : "ml-auto "}flex max-w-[300px] items-center gap-1 rounded-full bg-soft px-2 py-1`}>
            <select
              value={requestModel}
              onChange={(e) => setCustomModel(e.target.value)}
              disabled={streaming || loadingCustomModels || customModelOptions.length === 0}
              title="Model for this conversation — does not change the assistant default"
              className="min-w-0 flex-1 bg-transparent px-1 font-mono text-xs text-mute outline-none disabled:opacity-50"
            >
              {!requestModel && <option value="">{loadingCustomModels ? "Loading models…" : "No models found"}</option>}
              {customModelOptions.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
            <button onClick={() => loadCustomModels(true)} disabled={streaming || loadingCustomModels}
              title="Refresh models from the configured provider"
              className="rounded-full px-1.5 py-0.5 text-xs text-body hover:bg-panel hover:text-ink disabled:opacity-40"
            >{loadingCustomModels ? <Spinner /> : "↻"}</button>
          </div>
        ) : (
          <span
            className={`${ctxWindow && messages.length > 0 ? "" : "ml-auto "}max-w-[220px] truncate rounded-full bg-soft px-3 py-1.5 font-mono text-xs text-mute`}
            title={externalAgent
              ? `This agent runs on ${providerTag} (${selectedAgent!.model || "no model set"})`
              : modelLabel ? `Active model: ${modelLabel}` : "No active model — activate one in the Models tab"}
          >{effectiveLabel || "no active model"}</span>
        )}

        {/* send / stop */}
        {streaming ? (
          <button onClick={stop} className="grid size-9 place-items-center rounded-full bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep" title="Stop"><Stop /></button>
        ) : (
          <button
            onClick={() => send()}
            disabled={!input.trim() && pending.length === 0}
            className="grid size-9 place-items-center rounded-full bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-opacity hover:bg-primary-deep disabled:opacity-30"
            title="Send"
          ><Up /></button>
        )}
      </div>

      <input ref={fileRef} type="file" multiple hidden
        onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
    </div>
  );

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      {/* top bar */}
      <div className="flex items-center gap-3 border-b border-pressed bg-softer px-6 py-3">
        <h1 className="font-display text-lg font-semibold tracking-tight text-ink">Chat</h1>
        {agents.length > 0 && (
          <label className="flex items-center gap-1.5 rounded-full border border-pressed bg-soft px-2.5 py-1 text-xs text-mute" title="Run the chat as this agent (persona + tools + skills)">
            <span>as</span>
            <select
              value={agentId ?? ""}
              onChange={(e) => setAgentId(e.target.value || null)}
              className="rounded-full bg-transparent text-xs font-semibold text-ink outline-none"
            >
              {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
        )}
        {hasModel && (
          <span className="ml-auto flex max-w-[40%] items-center gap-1.5 truncate rounded-full bg-success-soft px-3 py-1 font-mono text-xs text-success" title={effectiveLabel}>
            <span className="size-1.5 shrink-0 rounded-full bg-success" />
            <span className="truncate">{effectiveLabel}</span>
          </span>
        )}
      </div>

      {notice && (
        <div className="mx-4 mt-3 rounded-xl border border-primary-line bg-primary-soft px-3 py-2 text-xs text-primary-deep">{notice}</div>
      )}

      {empty ? (
        // Centered greeting + composer
        <div className="flex flex-1 flex-col items-center justify-center px-4">
          <div className="mb-6 text-center">
            <img src={appIcon} alt="" className="animate-float mx-auto mb-4 size-24 select-none" draggable={false} />
            <h2 className="font-display text-3xl font-semibold tracking-tight text-ink">{greeting()}</h2>
            <p className="mt-1 text-sm text-mute">
              {hasModel ? (
                <>{selectedAgent ? <><span className="text-body">{selectedAgent.name}</span> · </> : null}
                  <span className="font-mono text-body">{effectiveLabel}</span></>
              ) : "Activate a model in Models to begin"}
            </p>
          </div>
          <div className="w-full max-w-2xl">
            {error && <p className="mb-2 rounded-xl border border-danger bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
            {composer}
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              {["Summarize a repo", "Write a unit test", "Explain this error"].map((s) => (
                <button
                  key={s}
                  onClick={() => setInput(s)}
                  className="rounded-full bg-soft px-3.5 py-1.5 text-xs font-medium text-body transition-colors hover:bg-primary-soft hover:text-primary-deep"
                >{s}</button>
              ))}
            </div>
          </div>
        </div>
      ) : (
        // Active thread
        <>
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
            <div className="mx-auto max-w-2xl space-y-5">
              {messages.map((m, i) => m.compacted ? (
                <div key={i} className="my-1">
                  <details className="rounded-2xl border border-dashed border-pressed bg-softer/60 px-3 py-2">
                    <summary className="flex cursor-pointer items-center gap-2 text-xs font-medium text-mute">
                      <span aria-hidden>🗜️</span>
                      Summary of earlier conversation
                      <span className="font-normal text-mute/70">· click to expand</span>
                    </summary>
                    <div className="mt-2 text-[13px] leading-relaxed text-body">
                      <RichText text={m.content} agentId={agentId} onImagePreview={setPreviewImage} />
                    </div>
                  </details>
                </div>
              ) : m.meta?.kind === "inbox" ? (
                // Handoff event from another agent's closed task — a system notice,
                // not a user turn: no avatar, muted dashed card.
                <div key={i} className="my-1">
                  <div className="rounded-2xl border border-dashed border-pressed bg-soft px-3 py-2">
                    <p className="mb-1 text-[10px] font-extrabold uppercase tracking-wide text-mute">Handoff result</p>
                    <div className="text-[13px] leading-relaxed text-body">
                      <RichText text={m.content} agentId={agentId} onImagePreview={setPreviewImage} />
                    </div>
                  </div>
                </div>
              ) : (
                <div key={i} className={`flex gap-2.5 ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  {m.role !== "user" && (
                    <img src={appIcon} alt="" className="mt-0.5 size-[30px] shrink-0 rounded-full" draggable={false} />
                  )}
                  <div className={`max-w-[88%] text-[15px] leading-relaxed ${
                    m.role === "user"
                      ? "rounded-[18px] rounded-br-md bg-user-bubble px-4 py-2.5 text-user-bubble-ink"
                      : "text-body"
                  }`}>
                    {m.images?.length ? (
                      <div className="mb-2 flex flex-wrap gap-2">
                        {m.images.map((src, k) => (
                          <button key={k} onClick={() => setPreviewImage(src)} aria-label="Preview attached image">
                            <img src={src} alt="" className="max-h-40 rounded-lg" />
                          </button>
                        ))}
                      </div>
                    ) : null}
                    {m.role === "assistant" && m.timeline?.length ? (() => {
                      // Unified work timeline: narration + tool calls in the order they
                      // happened, folded into one "Worked for Ns" panel. The final
                      // answer lives in `m.content` (rendered below), not here.
                      const isLive = streaming && i === messages.length - 1;
                      const nTools = m.timeline!.filter((t) => t.kind === "tool").length;
                      const secs = m.meta?.ms ? Math.round(m.meta.ms / 1000) : (isLive ? elapsed : 0);
                      const label = isLive
                        ? `Working…${elapsed > 0 ? ` · ${elapsed}s` : ""}`
                        : `Worked${secs ? ` for ${secs}s` : ""}${nTools ? ` · ${nTools} tool${nTools === 1 ? "" : "s"}` : ""}`;
                      return <WorkPanel timeline={m.timeline!} label={label} live={isLive} agentId={agentId}
                                        onImagePreview={setPreviewImage} />;
                    })() : m.steps?.length ? (
                      // Fallback for sessions saved before the timeline existed.
                      <div className="mb-2 space-y-1 rounded-xl border border-pressed bg-softer p-2.5">
                        {m.steps.map((s, k) => (
                          <div key={k} className="flex items-center gap-1.5 text-xs text-body">
                            {s.done ? <span className="text-success">✓</span> : <Spinner />}
                            <span className="font-medium">{stepVerb(s.name)}</span>
                            <span className="truncate font-mono text-mute">{stepTarget(s)}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {m.content ? (
                      m.role === "user"
                        ? <span className="whitespace-pre-wrap">{m.content}</span>
                        : <RichText text={m.content} agentId={agentId} onImagePreview={setPreviewImage} />
                    ) : null}
                    {m.role === "assistant" && m.proposalIds?.length ? (
                      <div className="mt-2 space-y-2">
                        {m.proposalIds.map((pid) => <ChatProposalCard key={pid} proposalId={pid} />)}
                      </div>
                    ) : null}
                    {m.role === "assistant" && streaming && i === messages.length - 1 && (() => {
                      const running = m.steps?.find((s) => !s.done);
                      const label = retryStatus ?? (readingImage ? "Reading image…" : running ? `${stepVerb(running.name)}…` : m.content ? "Writing…" : "Working…");
                      return (
                        <div className="mt-1.5 flex items-center gap-2 text-xs font-medium text-mute">
                          <WorkingDots />
                          <span>{label}</span>
                          {elapsed > 0 && <span className="font-normal text-mute/70">· {elapsed}s</span>}
                        </div>
                      );
                    })()}
                    {m.role === "assistant" && m.content && voiceCap.tts && (
                      <button
                        onClick={() => speak(m.content, i)}
                        aria-label={speakingIdx === i ? "Stop reading aloud" : "Read this reply aloud"}
                        title={speakingIdx === i ? "Stop" : "Read aloud"}
                        className={`mt-2 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
                          speakingIdx === i
                            ? "border-primary bg-primary-soft text-primary-deep"
                            : "border-pressed text-mute hover:bg-soft hover:text-ink"
                        }`}
                      >{speakingIdx === i ? <Stop /> : <Speaker />} {speakingIdx === i ? "Stop" : "Speak"}</button>
                    )}
                    {m.role === "assistant" && m.meta && (
                      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-mute">
                        {m.meta.ms != null && <span>⏱ {(m.meta.ms / 1000).toFixed(1)}s</span>}
                        {m.meta.tokSec ? <span>· {m.meta.tokSec} tok/s</span> : null}
                        {m.meta.tokens ? <span>· {m.meta.tokens} tokens</span> : null}
                        {m.meta.skills?.length ? (
                          <span className="rounded-full border border-primary-line px-2 py-0.5 font-medium text-primary-deep" title="Skill(s) active this turn">📘 {m.meta.skills.join(", ")}</span>
                        ) : null}
                        {m.meta.tools?.length ? (
                          <span className="rounded-full bg-primary-soft px-2 py-0.5 font-medium text-primary-deep">🧩 {m.meta.tools.join(", ")}</span>
                        ) : null}
                        {sessionId && (
                          <button
                            onClick={() => onTrace(sessionId, m.meta?.runId)}
                            title={m.meta?.runId ? "Open this turn's LLM-call trace in Debug" : "Open this conversation's trace in Debug"}
                            className="rounded-full px-2 py-0.5 font-medium text-mute underline-offset-2 hover:text-primary-deep hover:underline"
                          >🔎 Trace in Debug</button>
                        )}
                      </div>
                    )}
                    {m.role === "assistant" && m.content && (i < messages.length - 1 || !streaming) && (
                      <FeedbackBar
                        sessionId={sessionId}
                        agentId={agentId}
                        model={model}
                        turnIndex={i}
                        userPrompt={messages[i - 1]?.role === "user" ? messages[i - 1].content : ""}
                        original={m.content}
                      />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="px-4 pb-4">
            <div className="mx-auto max-w-2xl">
              {error && <p className="mb-2 rounded-xl border border-danger bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
              {composer}
            </div>
          </div>
        </>
      )}
      {previewImage && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Image preview"
          onClick={() => setPreviewImage(null)}
          className="fixed inset-0 z-50 grid cursor-zoom-out place-items-center bg-black/80 p-6"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="relative cursor-default"
          >
            <img
              src={previewImage}
              alt="Full-size attachment preview"
              className="max-h-[70vh] max-w-[70vw] rounded-xl object-contain shadow-2xl"
            />
            <button
              onClick={() => setPreviewImage(null)}
              aria-label="Close image preview"
              className="absolute -right-3 -top-3 grid size-8 place-items-center rounded-full bg-panel text-panel-ink shadow-lg hover:bg-panel/90"
            >✕</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default PlaygroundPage;
