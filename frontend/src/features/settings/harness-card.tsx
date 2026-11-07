// Coding harness (Claude Code + Codex) — availability, settings, a live view of
// the sessions on this machine, and the runs started from here. Reads /api/harness/*.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost, apiPut } from "@/lib/api";

type HarnessKey = "claude" | "codex";

interface HarnessSettings {
  bin: string; permissionMode: string; model: string;
  codexBin: string; codexSandbox: string; codexModel: string;
  maxTurns: number; timeoutS: number; allowedRoots: string[]; maxResultChars: number;
}
interface Availability { label: string; available: boolean; bin: string | null; version: string | null; error: string | null }
interface Status { harnesses: Record<HarnessKey, Availability>; settings: HarnessSettings; running: number }
interface Session {
  id: string; harness: HarnessKey; title: string; project: string; live: boolean; status: string; name: string;
  pid: number | null; lastActivity: string | null; lastPrompt: string; lastAssistant: string; messages: number;
}
interface Job {
  id: string; harness: HarnessKey; prompt: string; cwd: string; sessionId: string | null; forked: boolean; status: string;
  startedAt: string; finishedAt: string | null; newSessionId: string | null; result: string; error: string | null;
  numTurns: number; toolCalls: number; costUsd: number | null; lastText: string;
}

const PERMISSION_MODES = [
  ["", "Inherit Claude Code's setting"], ["acceptEdits", "Accept edits"], ["auto", "Auto"], ["plan", "Plan only"],
  ["dontAsk", "Don't ask"], ["bypassPermissions", "Bypass permissions (dangerous)"],
] as const;
const SANDBOXES = [["read-only", "Read-only"], ["workspace-write", "Workspace write"], ["danger-full-access", "Full access (dangerous)"]] as const;

function ago(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const inputCls = "w-full rounded-lg border border-line bg-canvas px-2.5 py-1.5 text-sm text-ink focus:border-primary focus:outline-none";

export function HarnessCard() {
  const [status, setStatus] = useState<Status | null>(null);
  const [cfg, setCfg] = useState<HarnessSettings | null>(null);
  const [harness, setHarness] = useState<HarnessKey>("claude");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [target, setTarget] = useState("");
  const [starting, setStarting] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const st = await apiJson<Status>("/api/harness/status");
      setStatus(st); setCfg((c) => c ?? st.settings);
      const [s, j] = await Promise.all([
        st.harnesses[harness]?.available
          ? apiJson<{ sessions: Session[] }>(`/api/harness/sessions?harness=${harness}&limit=12`)
          : Promise.resolve({ sessions: [] as Session[] }),
        apiJson<{ jobs: Job[] }>("/api/harness/jobs?limit=10"),
      ]);
      setSessions(s.sessions); setJobs(j.jobs); setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, [harness]);
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, [load]);

  const save = async (patch: Partial<HarnessSettings>) => {
    if (!cfg) return;
    const next = { ...cfg, ...patch };
    setCfg(next); setSaving(true); setError(null);
    try { setCfg(await apiPut<HarnessSettings>("/api/harness/settings", next)); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSaving(false); }
  };

  const run = async () => {
    if (!prompt.trim()) return;
    setStarting(true); setError(null);
    try {
      const body: Record<string, string> = { prompt: prompt.trim(), harness };
      if (target.startsWith("/") || target.startsWith("~")) body.project = target; else if (target) body.session = target;
      await apiPost("/api/harness/run", body);
      setPrompt(""); await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setStarting(false); }
  };

  const stop = async (id: string) => {
    try { await apiPost(`/api/harness/jobs/${id}/stop`, {}); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const avail = status?.harnesses[harness];

  return (
    <div className="mt-8">
      <div className="mb-4">
        <h2 className="font-display text-base font-semibold text-ink">Coding harness (Claude Code, Codex)</h2>
        <p className="mt-0.5 text-sm text-body">
          See the coding-agent sessions on this machine and send prompts into them, from here, from a
          worker's <span className="font-mono text-xs">code_*</span> tools, or with <span className="font-mono text-xs">/code</span> and{" "}
          <span className="font-mono text-xs">/codex</span> on a chat channel. Off for every worker until you enable its <em>Coding harness</em> guardrail.
        </p>
      </div>

      {error && <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="rounded-2xl border border-line bg-canvas p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex overflow-hidden rounded-full border border-pressed">
            {(["claude", "codex"] as const).map((k) => (
              <button key={k} onClick={() => setHarness(k)}
                className={`px-3 py-1.5 text-xs font-medium ${harness === k ? "bg-primary text-white" : "text-body hover:bg-soft hover:text-ink"}`}>
                {k === "claude" ? "Claude Code" : "Codex"}
              </button>
            ))}
          </div>
          <div className="min-w-0">
            <div className="font-display text-sm font-semibold text-ink">
              {avail ? (avail.available ? `${avail.label} ${avail.version ?? ""}` : `${avail.label} not found`) : "checking…"}
            </div>
            <p className="mt-0.5 truncate font-mono text-[11px] text-mute">{avail?.bin ?? avail?.error ?? ""}</p>
          </div>
          {status && <span className="ml-auto rounded-full bg-soft px-2.5 py-0.5 text-[11px] font-semibold text-body">{status.running} running</span>}
        </div>
        {cfg && harness === "claude" && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-xs text-body">Binary path (blank = find on PATH)
              <input className={`${inputCls} mt-1 font-mono text-xs`} value={cfg.bin} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, bin: e.target.value })} onBlur={() => save({})} placeholder="/usr/local/bin/claude" />
            </label>
            <label className="text-xs text-body">Permission mode for runs
              <select className={`${inputCls} mt-1`} value={cfg.permissionMode} disabled={saving}
                onChange={(e) => save({ permissionMode: e.target.value })}>
                {PERMISSION_MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="text-xs text-body">Model (blank = Claude Code default)
              <input className={`${inputCls} mt-1 font-mono text-xs`} value={cfg.model} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, model: e.target.value })} onBlur={() => save({})} placeholder="e.g. opus" />
            </label>
            <label className="text-xs text-body">Max turns per prompt (0 = Claude Code default)
              <input className={`${inputCls} mt-1`} type="number" min={0} max={500} value={cfg.maxTurns} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, maxTurns: Number(e.target.value) || 0 })} onBlur={() => save({})} />
            </label>
          </div>
        )}
        {cfg && harness === "codex" && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-xs text-body">Binary path (blank = find on PATH)
              <input className={`${inputCls} mt-1 font-mono text-xs`} value={cfg.codexBin} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, codexBin: e.target.value })} onBlur={() => save({})} placeholder="/usr/local/bin/codex" />
            </label>
            <label className="text-xs text-body">Sandbox for runs
              <select className={`${inputCls} mt-1`} value={cfg.codexSandbox} disabled={saving}
                onChange={(e) => save({ codexSandbox: e.target.value })}>
                {SANDBOXES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="text-xs text-body">Model (blank = Codex default)
              <input className={`${inputCls} mt-1 font-mono text-xs`} value={cfg.codexModel} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, codexModel: e.target.value })} onBlur={() => save({})} placeholder="e.g. gpt-5.5" />
            </label>
          </div>
        )}
        {cfg && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-xs text-body">Timeout (seconds)
              <input className={`${inputCls} mt-1`} type="number" min={30} max={21600} value={cfg.timeoutS} disabled={saving}
                onChange={(e) => setCfg({ ...cfg, timeoutS: Number(e.target.value) || 1800 })} onBlur={() => save({})} />
            </label>
            <label className="text-xs text-body">Allowed roots for new sessions (one per line)
              <textarea className={`${inputCls} mt-1 font-mono text-xs`} rows={2} disabled={saving}
                value={cfg.allowedRoots.join("\n")}
                onChange={(e) => setCfg({ ...cfg, allowedRoots: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })}
                onBlur={() => save({})} placeholder="~/Documents" />
            </label>
          </div>
        )}
        <p className="mt-2 text-[11px] text-mute">
          Existing sessions can always be continued. A new session can only start inside an allowed root, so a
          prompt from a chat app can never point a coding agent at an arbitrary folder. A Claude Code session open
          in a terminal is forked rather than written to.
        </p>
      </div>

      {avail?.available && (
        <>
          <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
            <div className="font-display text-sm font-semibold text-ink">Send a prompt to {avail.label}</div>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input className={`${inputCls} sm:w-64 font-mono text-xs`} value={target} onChange={(e) => setTarget(e.target.value)}
                placeholder="session name / id prefix, or /path for a new one" />
              <input className={inputCls} value={prompt} onChange={(e) => setPrompt(e.target.value)}
                placeholder={`What should ${avail.label} do?`} onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
              <button onClick={run} disabled={starting || !prompt.trim() || !target.trim()}
                className="shrink-0 rounded-xl bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-deep disabled:opacity-40">
                {starting ? "Starting…" : "Run"}
              </button>
            </div>
          </div>

          <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
            <div className="font-display text-sm font-semibold text-ink">{avail.label} sessions</div>
            {sessions.length === 0 ? <p className="mt-2 text-xs text-mute">No sessions found.</p> : (
              <ul className="mt-2 divide-y divide-line">
                {sessions.map((s) => (
                  <li key={s.id} className="flex cursor-pointer items-center gap-2 py-2" onClick={() => setTarget(s.name || s.id.slice(0, 8))}>
                    <span className={`size-2 shrink-0 rounded-full ${s.live ? (s.status === "busy" ? "bg-warn" : "bg-success") : "bg-pressed"}`} title={s.status} />
                    <span className="w-36 shrink-0 truncate font-mono text-[11px] text-body">{s.name || s.id.slice(0, 8)}</span>
                    <span className="min-w-0 flex-1 truncate text-xs text-ink">{s.title}</span>
                    <span className="hidden w-32 shrink-0 truncate text-[11px] text-mute sm:block">{s.project.split("/").pop()}</span>
                    <span className="w-14 shrink-0 text-right text-[11px] text-mute">{ago(s.lastActivity)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      {jobs.length > 0 && (
        <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
          <div className="font-display text-sm font-semibold text-ink">Runs</div>
          <ul className="mt-2 divide-y divide-line">
            {jobs.map((j) => (
              <li key={j.id} className="py-2">
                <div className="flex cursor-pointer items-center gap-2" onClick={() => setOpen(open === j.id ? null : j.id)}>
                  <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                    j.status === "done" ? "bg-success-soft text-success" : j.status === "running" ? "bg-warn-soft text-warn" : "bg-danger-soft text-danger"}`}>{j.status}</span>
                  <span className="font-mono text-[11px] text-mute">{j.id}</span>
                  <span className="rounded-full bg-soft px-2 py-0.5 text-[10px] text-body">{j.harness === "codex" ? "Codex" : "Claude Code"}</span>
                  <span className="min-w-0 flex-1 truncate text-xs text-ink">{j.prompt}</span>
                  <span className="shrink-0 text-[11px] text-mute">{j.toolCalls} tools{j.costUsd != null ? ` · $${j.costUsd.toFixed(2)}` : ""}</span>
                  {j.status === "running" && (
                    <button onClick={(e) => { e.stopPropagation(); stop(j.id); }} className="text-[11px] text-danger hover:underline">stop</button>
                  )}
                </div>
                {open === j.id && (
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-xl bg-softer p-3 font-mono text-[11px] text-body">
                    {j.status === "running" ? (j.lastText || "working…") : (j.error || j.result || "(no output)")}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default HarnessCard;
