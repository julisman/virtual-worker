// Monitoring — live memory / CPU / disk / runtime health + recent errors and
// the inference-server log. Running models locally makes resource pressure the
// thing most likely to wedge or kill a model, so this surfaces it at a glance.
// Reads /api/monitor/*. Monochrome (Uber-inspired) theme.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types (mirror routes/monitor.py)
// --------------------------------------------------------------------------- //

interface Overview {
  memory: { totalBytes: number | null; usedBytes: number | null; availableBytes: number | null; percent: number | null; unified: boolean };
  vram: { totalBytes: number | null; usedBytes: number | null; percent: number | null; kind: string };
  cpu: { cores: number | null; brand: string | null; loadPercent: number | null };
  disk: { freeBytes: number | null; usedBytes: number | null; totalBytes: number | null; percent: number | null; path: string };
  process: { pid: number | null; rssBytes: number | null; uptimeSec: number | null } | null;
  runtime: {
    running: boolean; healthy: boolean; loading: boolean; failed: boolean; managed: boolean;
    backend: string | null; modelName: string | null; quant: string | null; ctx: number | null;
    port: number | null; error: string | null;
  };
  searxng: { managed: boolean; installed: boolean; running: boolean; port: number | null };
  api: { runs: number; errors: number; sessions: number; tokens: number; cost: number };
  host: { os: string; arch: string; python: string; upstreamUrl: string };
  now: string;
}
interface ApiError {
  runId: string; session: string; time: string; endpoint: string; provider: string;
  model: string; statusCode: number | null; error: string; durationMs: number | null;
}

// --------------------------------------------------------------------------- //
// Formatting helpers
// --------------------------------------------------------------------------- //

function gb(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  const v = bytes / 1024 ** 3;
  return v >= 100 ? `${Math.round(v)} GB` : `${v.toFixed(1)} GB`;
}
function mb(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  return `${Math.round(bytes / 1024 ** 2)} MB`;
}
function uptime(sec: number | null | undefined): string {
  if (sec == null) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (isNaN(t)) return iso;
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

// --------------------------------------------------------------------------- //
// Presentational
// --------------------------------------------------------------------------- //

/** A labeled usage bar. Turns near-black as it approaches full (pressure cue). */
function Gauge({ label, percent, primary, secondary }: {
  label: string; percent: number | null; primary: string; secondary?: string;
}) {
  const p = percent == null ? null : Math.max(0, Math.min(100, percent));
  const hot = p != null && p >= 90;
  return (
    <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-mute">{label}</span>
        <span className={`font-mono text-xs ${hot ? "text-danger" : "text-body"}`}>{p == null ? "—" : `${p}%`}</span>
      </div>
      <div className={`font-display mt-2 text-2xl font-semibold tracking-tight ${hot ? "text-danger" : "text-ink"}`}>{primary}</div>
      {secondary && <div className="text-xs text-body">{secondary}</div>}
      <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-soft">
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${hot ? "bg-danger" : "bg-primary"}`}
          style={{ width: `${p ?? 0}%` }}
        />
      </div>
    </div>
  );
}

function Stat({ label, value, alert }: { label: string; value: React.ReactNode; alert?: boolean }) {
  return (
    <div className={`rounded-xl border px-3 py-2.5 ${alert ? "border-danger bg-danger-soft" : "border-line bg-softer"}`}>
      <div className={`text-[10px] font-semibold uppercase tracking-wide ${alert ? "text-danger" : "text-mute"}`}>{label}</div>
      <div className={`font-display mt-0.5 text-lg font-semibold tracking-tight ${alert ? "text-danger" : "text-ink"}`}>{value}</div>
    </div>
  );
}

function Dot({ tone }: { tone: "live" | "warn" | "off" }) {
  const cls = tone === "live" ? "bg-success" : tone === "warn" ? "bg-warn" : "bg-mute";
  return <span className={`inline-block h-2 w-2 rounded-full ${cls} ${tone === "live" ? "animate-pulse" : ""}`} />;
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function MonitoringPage() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [errors, setErrors] = useState<ApiError[]>([]);
  const [log, setLog] = useState<{ text: string; exists: boolean; path: string } | null>(null);
  const [auto, setAuto] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

  const load = useCallback(async () => {
    try {
      const [o, e, l] = await Promise.all([
        apiJson<Overview>("/api/monitor/overview"),
        apiJson<{ errors: ApiError[] }>("/api/monitor/errors?limit=25"),
        apiJson<{ text: string; exists: boolean; path: string }>("/api/monitor/logs?lines=200"),
      ]);
      setOv(o); setErrors(e.errors); setLog(l); setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load monitoring data");
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [auto, load]);

  // Keep the log scrolled to the newest line when it updates.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log?.text]);

  const rt = ov?.runtime;
  const rtTone: "live" | "warn" | "off" =
    rt?.failed ? "warn" : rt?.healthy ? "live" : rt?.loading ? "warn" : "off";
  const rtLabel =
    rt?.failed ? "Load failed" : rt?.healthy ? "Healthy" : rt?.loading ? "Loading…" : rt?.running ? "Running" : "Stopped";

  return (
    <div className="mx-auto max-w-5xl px-5 py-6 sm:px-8">
      {/* Header */}
      <div className="mb-5 -mx-5 flex items-center gap-3 border-b border-pressed bg-softer px-5 py-4 sm:-mx-8 sm:px-8">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Monitoring</h1>
          <p className="mt-0.5 text-sm text-body">Live resource usage, model health, errors, and the inference log.</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {rtLabel && (
            <span className="hidden items-center gap-1.5 rounded-full bg-success-soft px-3 py-1.5 text-xs font-medium text-success sm:inline-flex">
              <span className="inline-block h-2 w-2 rounded-full bg-success" />
              {rt?.modelName ?? "no model"} · {rt?.backend ?? "engine"}
            </span>
          )}
          {ov?.searxng?.installed && (
            <span title={`Managed SearXNG (web search)${ov.searxng.port ? ` · :${ov.searxng.port}` : ""}`}
              className={`hidden items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium sm:inline-flex ${
                ov.searxng.running ? "bg-success-soft text-success" : "bg-soft text-mute"
              }`}>
              <span className={`inline-block h-2 w-2 rounded-full ${ov.searxng.running ? "bg-success" : "bg-mute"}`} />
              Search {ov.searxng.running ? "up" : "down"}
            </span>
          )}
          <button
            onClick={() => setAuto((a) => !a)}
            className={`rounded-full border px-3 py-1.5 text-xs font-medium ${
              auto ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft hover:text-ink"
            }`}
          >{auto ? "● Auto 4s" : "Paused"}</button>
          <button
            onClick={load}
            className="rounded-full border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink"
          >Refresh</button>
        </div>
      </div>

      {err && (
        <div className="mb-5 rounded-xl border border-danger bg-danger-soft px-4 py-3 text-sm text-danger">
          {err} — is the backend running on :8888?
        </div>
      )}

      {/* Resource gauges */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Gauge
          label={ov?.memory.unified ? "Memory (unified)" : "Memory"}
          percent={ov?.memory.percent ?? null}
          primary={`${gb(ov?.memory.usedBytes)} / ${gb(ov?.memory.totalBytes)}`}
          secondary={`${gb(ov?.memory.availableBytes)} available`}
        />
        {ov?.vram.kind === "nvidia" ? (
          <Gauge label="GPU memory" percent={ov?.vram.percent ?? null}
            primary={`${gb(ov?.vram.usedBytes)} / ${gb(ov?.vram.totalBytes)}`} secondary="VRAM" />
        ) : (
          <Gauge label="CPU load" percent={ov?.cpu.loadPercent ?? null}
            primary={ov?.cpu.loadPercent == null ? "—" : `${ov.cpu.loadPercent}%`}
            secondary={ov?.cpu.brand ? `${ov.cpu.cores ?? "?"} cores · ${ov.cpu.brand}` : `${ov?.cpu.cores ?? "?"} cores`} />
        )}
        <Gauge label="Disk (model cache)" percent={ov?.disk.percent ?? null}
          primary={`${gb(ov?.disk.usedBytes)} / ${gb(ov?.disk.totalBytes)}`}
          secondary={`${gb(ov?.disk.freeBytes)} free`} />
        <Gauge label="Model process" percent={
          ov?.process?.rssBytes && ov?.memory.totalBytes ? Math.round(ov.process.rssBytes / ov.memory.totalBytes * 100) : null
        }
          primary={ov?.process ? mb(ov.process.rssBytes) : "—"}
          secondary={ov?.process ? `up ${uptime(ov.process.uptimeSec)} · pid ${ov.process.pid}` : "not running"} />
      </div>

      {/* Runtime + API stats */}
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
        {/* Runtime */}
        <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)] lg:col-span-2">
          <div className="mb-3 flex items-center gap-2">
            <Dot tone={rtTone} />
            <h2 className="text-sm font-semibold text-ink">Inference server</h2>
            <span className={`ml-auto rounded-full px-2.5 py-0.5 text-xs font-medium ${
              rtTone === "live" ? "bg-success-soft text-success" : rtTone === "warn" ? "bg-warn-soft text-warn" : "border border-pressed text-body"
            }`}>{rtLabel}</span>
          </div>
          {rt?.running || rt?.failed ? (
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Model</div><div className="truncate font-medium text-ink" title={rt?.modelName ?? ""}>{rt?.modelName ?? "—"}</div></div>
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Engine</div><div className="font-medium text-ink">{rt?.backend ?? "—"}{rt?.quant ? ` · ${rt.quant}` : ""}</div></div>
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Context</div><div className="font-medium text-ink">{rt?.ctx ? `${(rt.ctx / 1024).toFixed(0)}K` : "—"}</div></div>
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Port</div><div className="font-mono text-ink">{rt?.port ?? "—"}</div></div>
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Managed</div><div className="font-medium text-ink">{rt?.managed ? "yes" : "no"}</div></div>
              <div><div className="text-[11px] uppercase tracking-wide text-mute">Upstream</div><div className="truncate font-mono text-xs text-body" title={ov?.host.upstreamUrl}>{ov?.host.upstreamUrl?.replace(/^https?:\/\//, "")}</div></div>
            </div>
          ) : (
            <p className="text-sm text-body">No model is active. Activate one from <span className="font-semibold text-ink">Models</span> to start the inference server.</p>
          )}
          {rt?.error && (
            <pre className="mt-3 max-h-28 overflow-auto whitespace-pre-wrap rounded-xl border border-danger bg-danger-soft p-2.5 font-mono text-[11px] text-danger">{rt.error}</pre>
          )}
        </div>

        {/* API stats */}
        <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
          <h2 className="mb-3 text-sm font-semibold text-ink">API traffic</h2>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Calls" value={ov?.api.runs ?? "—"} />
            <Stat label="Errors" value={ov?.api.errors ?? "—"} alert={!!ov && ov.api.errors > 0} />
            <Stat label="Sessions" value={ov?.api.sessions ?? "—"} />
            <Stat label="Tokens" value={ov ? ov.api.tokens.toLocaleString() : "—"} />
          </div>
          <p className="mt-2.5 text-[11px] text-mute">Totals captured in Debug. Local models cost $0{ov && ov.api.cost > 0 ? ` · $${ov.api.cost.toFixed(4)} external` : ""}.</p>
        </div>
      </div>

      {/* Recent errors */}
      <div className="mt-3 rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h2 className="mb-3 text-sm font-semibold text-ink">Recent errors</h2>
        {errors.length === 0 ? (
          <p className="text-sm text-body">No errors recorded. 🎉</p>
        ) : (
          <div className="space-y-1.5">
            {errors.map((e) => {
              const open = expanded === e.runId;
              return (
                <div key={e.runId} className="rounded-xl border border-line">
                  <button
                    onClick={() => setExpanded(open ? null : e.runId)}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-soft"
                  >
                    <span className="rounded-md bg-danger px-1.5 py-0.5 font-mono text-[10px] font-semibold text-white">{e.statusCode ?? "ERR"}</span>
                    <span className="truncate font-medium text-ink">{e.model || e.endpoint}</span>
                    <span className="hidden shrink-0 text-xs text-mute sm:inline">{e.endpoint}</span>
                    <span className="ml-auto shrink-0 font-mono text-[11px] text-mute">{relTime(e.time)}</span>
                  </button>
                  {open && (
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap border-t border-line bg-danger-soft p-2.5 font-mono text-[11px] text-danger">{e.error || "(no message)"}</pre>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Inference log */}
      <div className="mt-3 rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-sm font-semibold text-ink">Inference server log</h2>
          {log && <span className="ml-auto truncate font-mono text-[11px] text-mute" title={log.path}>{log.path}</span>}
        </div>
        {log?.exists ? (
          <pre ref={logRef} className="max-h-80 overflow-auto whitespace-pre-wrap rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink">{log.text || "(log is empty)"}</pre>
        ) : (
          <p className="text-sm text-body">No log yet — it appears once a managed model server has started.</p>
        )}
      </div>

      <p className="mt-4 text-center text-[11px] text-mute">{ov ? `Updated ${ov.now.replace("T", " ")} · ${ov.host.os}/${ov.host.arch} · Python ${ov.host.python}` : "Loading…"}</p>
    </div>
  );
}

export default MonitoringPage;
