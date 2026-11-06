// Hub — discover, download, and run models locally (HuggingFace + Ollama).
//
// Modeled on Unsloth's Hub: Discover / On-Device tabs, search + filters, a model
// list beside a detail panel, per-file GGUF download with a local-capability fit
// check and live progress. Monochrome (Uber-inspired) theme; plain React.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types — mirror backend/routes/hub.py
// --------------------------------------------------------------------------- //

interface Capability {
  os: string; arch: string;
  cpuCores: number | null; cpuBrand: string | null;
  cpuRamGb: number | null; vramGb: number | null; vramKind: string;
  diskFreeGb: number | null; diskTotalGb: number | null;
  hfTokenPresent: boolean; hfCacheDir: string;
}
interface DiscoverModel {
  id: string; author: string; likes: number; downloads: number;
  lastModified: string | null; pipelineTag: string | null; tags: string[]; gated: boolean;
  paramsB: number | null; fits: boolean | null;
}
type Fit = "fits" | "tight" | "too_large" | "unknown";
interface GgufFile {
  filename: string; sizeBytes: number | null; sizeGb: number | null;
  quant: string | null; fit: Fit; cached: boolean;
}
interface ModelDetail {
  id: string; author: string; downloads: number; likes: number; lastModified: string | null;
  pipelineTag: string | null; license: string | null; tags: string[];
  baseModel: string | null; gated: boolean; files: GgufFile[];
  mlx: boolean; repoSizeBytes: number | null; repoSizeGb: number | null;
  repoCached: boolean; repoFit: Fit;
}
interface LocalModel {
  source: "hf-cache" | "finetuned"; backend: "llamacpp" | "mlx"; repo: string; filename: string;
  path: string | null; sizeBytes: number | null; sizeGb: number | null; quant: string | null;
  multimodal?: boolean;
  maxCtx?: number | null;          // model's trained max context (from GGUF/config)
  recommendedCtx?: number | null;  // largest that likely fits this machine, ≤ maxCtx
  mtpCapable?: boolean;            // GGUF has an embedded MTP head → llama.cpp can self-speculate
}
interface DownloadJob {
  id: string; source: string; repo: string; filename: string | null;
  status: "starting" | "downloading" | "done" | "error" | "cancelled";
  error: string | null; totalBytes: number | null; receivedBytes: number;
  percent: number | null; startedAt: number;
}
interface RuntimeStatus {
  running: boolean; healthy: boolean; loading: boolean; failed?: boolean; verifying?: boolean; managed: boolean;
  backend: string | null;
  modelName: string | null; modelPath: string | null; quant: string | null;
  ctx: number | null; binAvailable: boolean; binLlamacpp: boolean; binMlx: boolean; binMlxVlm: boolean;
  mtp?: boolean; mtpNMax?: number;
  error: string | null;
}

const FORMATS = [
  { id: "gguf", label: "GGUF · llama.cpp" },
  { id: "mlx", label: "MLX · Apple" },
];

const CTX_STEPS = [8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576];

function fmtCtx(n: number): string {
  if (n >= 1_048_576 && n % 1_048_576 === 0) return `${n / 1_048_576}M`;
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
}

// Per-model context choices: the standard steps up to the model's max, plus the
// model's recommended (memory-safe) and true max, tagged for clarity.
function ctxOptionsFor(m: LocalModel): { id: string; label: string }[] {
  const max = m.maxCtx ?? null;
  const rec = m.recommendedCtx ?? null;
  const vals = new Set<number>();
  for (const s of CTX_STEPS) if (!max || s <= max) vals.add(s);
  if (rec) vals.add(rec);
  if (max) vals.add(max);
  if (!vals.size) vals.add(32768);
  return [...vals].sort((a, b) => a - b).map((v) => {
    const tags: string[] = [];
    if (rec && v === rec) tags.push("recommended");
    if (max && v === max) tags.push("max");
    return { id: String(v), label: `${fmtCtx(v)} ctx${tags.length ? ` (${tags.join(", ")})` : ""}` };
  });
}

function defaultCtxFor(m: LocalModel): number {
  return m.recommendedCtx ?? 32768;
}

type SubTab = "discover" | "device";

// --------------------------------------------------------------------------- //
// Formatting helpers
// --------------------------------------------------------------------------- //

function fmtCount(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}
function fmtAgo(iso: string | null): string {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  if (d < 86400 * 30) return `${Math.round(d / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
function fmtBytes(b: number | null): string {
  if (!b) return "—";
  const gb = b / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(2)} GB` : `${(b / 1024 ** 2).toFixed(0)} MB`;
}

const FIT_BADGE: Record<Fit, { label: string; cls: string }> = {
  fits: { label: "Fits", cls: "border-success-soft bg-success-soft text-success" },
  tight: { label: "Tight", cls: "border-warn-soft bg-warn-soft text-warn" },
  too_large: { label: "Too large", cls: "border-danger-soft bg-danger-soft text-danger line-through" },
  unknown: { label: "—", cls: "border-pressed bg-soft text-mute" },
};

// --------------------------------------------------------------------------- //
// Icons
// --------------------------------------------------------------------------- //

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}
const DownloadIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
  </svg>
);
const RefreshIcon = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M23 4v6h-6M1 20v-6h6" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
  </svg>
);
function Avatar({ name }: { name: string }) {
  return (
    <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-soft font-mono text-sm font-bold text-body">
      {name.slice(0, 1).toUpperCase()}
    </div>
  );
}
function Chip({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full bg-soft px-2.5 py-0.5 text-[11px] text-body">{children}</span>;
}

// --------------------------------------------------------------------------- //
// Filters
// --------------------------------------------------------------------------- //

const SORTS = [
  { id: "newest", label: "Newest" },
  { id: "downloads", label: "Most downloads" },
  { id: "likes", label: "Most likes" },
  { id: "trending", label: "Trending" },
];
const CAPS = [
  { id: "", label: "All capabilities" },
  { id: "text-generation", label: "Text Generation" },
  { id: "text2text-generation", label: "Text2Text" },
  { id: "feature-extraction", label: "Embeddings" },
  { id: "automatic-speech-recognition", label: "Speech" },
  { id: "text-to-image", label: "Image" },
];

function Select<T extends string>(props: {
  value: T; onChange: (v: T) => void; options: { id: T; label: string }[];
}) {
  return (
    <select
      value={props.value}
      onChange={(e) => props.onChange(e.target.value as T)}
      className="rounded-full border border-pressed bg-canvas px-3.5 py-1.5 text-sm text-body outline-none hover:bg-soft focus:border-primary"
    >
      {props.options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
    </select>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function HubPage() {
  const [cap, setCap] = useState<Capability | null>(null);
  const [sub, setSub] = useState<SubTab>("discover");

  // discover state
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("downloads");
  const [capFilter, setCapFilter] = useState("");
  const [format, setFormat] = useState("gguf");
  const [models, setModels] = useState<DiscoverModel[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // on-device state
  const [local, setLocal] = useState<LocalModel[]>([]);
  const [localLoading, setLocalLoading] = useState(false);
  // Trainability verdict per repo, from the Train tab's source of truth.
  const [trainMap, setTrainMap] = useState<Record<string, { trainable: boolean; reason?: string | null }>>({});

  // downloads
  const [jobs, setJobs] = useState<DownloadJob[]>([]);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // active model (managed llama runtime)
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [activating, setActivating] = useState<string | null>(null); // path being started
  // Per-model context choice; falls back to the model's recommended (memory-safe) value.
  const [ctxByPath, setCtxByPath] = useState<Record<string, number>>({});
  // Per-model MTP (multi-token-prediction) speculative decoding — off by default.
  const [mtpByPath, setMtpByPath] = useState<Record<string, boolean>>({});
  const rtPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ---- capability ---- //
  const loadCap = useCallback(() => {
    apiJson<Capability>("/api/hub/capability").then(setCap).catch(() => setCap(null));
  }, []);
  useEffect(() => { loadCap(); }, [loadCap]);

  // ---- discover ---- //
  const loadModels = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      // fit_only defaults true on the backend — we only ever show models that fit.
      const params = new URLSearchParams({ q: query, sort, capability: capFilter, fmt: format });
      const data = await apiJson<{ models: DiscoverModel[] }>(`/api/hub/discover?${params}`);
      setModels(data.models);
      if (!selected && data.models[0]) setSelected(data.models[0].id);
    } catch (e) {
      setModels([]);
      setListError(e instanceof Error ? e.message : String(e));
    } finally {
      setListLoading(false);
    }
  }, [query, sort, capFilter, format]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (sub !== "discover") return;
    const t = setTimeout(loadModels, query ? 350 : 0); // debounce typing
    return () => clearTimeout(t);
  }, [loadModels, sub, query]);

  // ---- detail ---- //
  const selectedRef = useRef<string | null>(null);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  const loadDetail = useCallback((id: string | null, silent = false) => {
    if (!id) { setDetail(null); return; }
    if (!silent) setDetailLoading(true);
    apiJson<ModelDetail>(`/api/hub/model/${id}`)
      .then(setDetail)
      .catch(() => { if (!silent) setDetail(null); })
      .finally(() => { if (!silent) setDetailLoading(false); });
  }, []);
  useEffect(() => { loadDetail(selected); }, [selected, loadDetail]);

  // ---- on-device ---- //
  const loadLocal = useCallback(async () => {
    setLocalLoading(true);
    try {
      const data = await apiJson<{ models: LocalModel[] }>("/api/hub/local");
      setLocal(data.models);
      try {
        const bm = await apiJson<{ models: { repo: string; trainable: boolean; reason?: string | null }[] }>("/api/finetune/base-models");
        const map: Record<string, { trainable: boolean; reason?: string | null }> = {};
        for (const b of bm.models) map[b.repo] = { trainable: b.trainable, reason: b.reason };
        setTrainMap(map);
      } catch { /* finetune route optional */ }
    } catch { setLocal([]); } finally { setLocalLoading(false); }
  }, []);

  const deleteLocal = useCallback(async (m: LocalModel) => {
    if (!m.path) return;
    if (!confirm(`Delete ${m.filename}? This frees ~${fmtBytes(m.sizeBytes)} of disk.`)) return;
    try {
      await apiPost("/api/hub/delete", { path: m.path });
      loadLocal();
      loadCap(); // disk-free badge updates
    } catch (e) { alert(e instanceof Error ? e.message : String(e)); }
  }, [loadLocal, loadCap]);
  useEffect(() => { if (sub === "device") loadLocal(); }, [sub, loadLocal]);

  // ---- downloads polling ---- //
  const wasActiveRef = useRef(false);
  const refreshJobs = useCallback(async () => {
    try {
      const data = await apiJson<{ downloads: DownloadJob[] }>("/api/hub/downloads");
      setJobs(data.downloads);
      const active = data.downloads.some((j) => j.status === "downloading" || j.status === "starting");
      // A download just finished → refresh cached/on-device state in place so the
      // button flips to "On device ✓" without a manual page refresh.
      if (wasActiveRef.current && !active) {
        loadLocal();
        loadDetail(selectedRef.current, true);
      }
      wasActiveRef.current = active;
      if (!active && pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    } catch { /* ignore transient */ }
  }, [loadLocal, loadDetail]);
  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    refreshJobs();
    pollRef.current = setInterval(refreshJobs, 1000);
  }, [refreshJobs]);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  // On load, re-attach to any download already running in the backend (e.g. one
  // that kept going while we were on another page, or survived a UI reload).
  useEffect(() => {
    apiJson<{ downloads: DownloadJob[] }>("/api/hub/downloads").then((d) => {
      setJobs(d.downloads);
      if (d.downloads.some((j) => j.status === "downloading" || j.status === "starting")) startPolling();
    }).catch(() => {});
  }, [startPolling]);

  const download = useCallback(async (body: { source: string; repo: string; filename?: string; kind?: string }) => {
    try {
      await apiPost("/api/hub/download", body);
      startPolling();
    } catch (e) { alert(e instanceof Error ? e.message : String(e)); }
  }, [startPolling]);

  const cancelJob = useCallback(async (id: string) => {
    await apiPost(`/api/hub/downloads/${id}/cancel`, {}).catch(() => {});
    refreshJobs();
  }, [refreshJobs]);

  const activeJobs = jobs.filter((j) => j.status === "downloading" || j.status === "starting");
  // Only surface an error if it's the MOST RECENT attempt for that model — a
  // later successful re-download supersedes an old failure. (jobs are newest-first)
  const latestForTarget = new Map<string, string>();
  for (const j of jobs) {
    const k = `${j.repo}|${j.filename ?? ""}`;
    if (!latestForTarget.has(k)) latestForTarget.set(k, j.id);
  }
  const failedJobs = jobs.filter((j) =>
    j.status === "error" && !dismissed.includes(j.id) &&
    latestForTarget.get(`${j.repo}|${j.filename ?? ""}`) === j.id);

  // ---- active model runtime ---- //
  const refreshRuntime = useCallback(async () => {
    try {
      const st = await apiJson<RuntimeStatus>("/api/runtime/status");
      setRuntime(st);
      if (!st.loading && rtPollRef.current) { clearInterval(rtPollRef.current); rtPollRef.current = null; }
      if (!st.loading) setActivating(null);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { refreshRuntime(); }, [refreshRuntime]);
  useEffect(() => () => { if (rtPollRef.current) clearInterval(rtPollRef.current); }, []);
  // The model is usable as soon as it's reachable, but a background probe is still
  // confirming it can actually generate — keep polling so a late failure (e.g. an
  // unsupported MoE) flips the chip to "Load failed" without a manual refresh.
  useEffect(() => {
    if (!runtime?.verifying) return;
    const t = setInterval(refreshRuntime, 2500);
    return () => clearInterval(t);
  }, [runtime?.verifying, refreshRuntime]);

  const activate = useCallback(async (m: LocalModel, force = false) => {
    const path = m.path!;
    const ctx = ctxByPath[path] ?? defaultCtxFor(m);
    const mtp = !!(m.mtpCapable && mtpByPath[path]);
    setActivating(path);
    try {
      await apiPost("/api/runtime/start", { path, force, ctx, mtp });
      // Poll until the model is actually healthy (MLX can take a while to load),
      // updating the UI each tick so it flips to Active without a manual refresh.
      for (let i = 0; i < 240; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        let st: RuntimeStatus | null = null;
        try { st = await apiJson<RuntimeStatus>("/api/runtime/status"); } catch { continue; }
        setRuntime(st);
        if (st.failed || st.error) {
          alert("This model failed to load and can't run — see the details below the model list.\n\n" +
                (st.error ?? "").slice(0, 400));
          break;
        }
        if (st.healthy && st.modelPath === path) break;     // loaded ✓
        if (!st.running && !st.loading) break;               // died/stopped
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!force && /already in use/i.test(msg) &&
          confirm("Another model server is already running on the port. Stop it and switch to this model?")) {
        return activate(m, true);
      }
      alert(msg);
    } finally {
      setActivating(null);
    }
  }, [ctxByPath, mtpByPath]);

  const stopRuntime = useCallback(async () => {
    await apiPost("/api/runtime/stop", {}).catch(() => {});
    refreshRuntime();
  }, [refreshRuntime]);

  function isActive(m: LocalModel): boolean {
    if (!runtime?.healthy) return false;
    if (m.path && runtime.modelPath) return m.path === runtime.modelPath;
    return !!runtime.modelName && m.filename.replace(/\.gguf$/, "") === runtime.modelName;
  }

  // ------------------------------------------------------------------------- //
  // Render
  // ------------------------------------------------------------------------- //

  return (
    <div className="flex h-full flex-col bg-base">
      {/* Header */}
      <div className="flex flex-wrap items-start gap-4 bg-softer border-b border-pressed px-6 py-3.5">
        <div>
          <h1 className="font-display text-2xl font-bold tracking-tight text-ink">Models</h1>
          <p className="mt-0.5 text-sm text-body">Discover, download, and run inference models locally.</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {runtime?.healthy && runtime.modelName ? (
            <span className="flex items-center gap-1.5 rounded-full bg-success-soft px-3 py-1 text-xs text-success">
              <span className="size-2 rounded-full bg-success" />
              Active: <span className="font-mono font-medium">{runtime.modelName}</span>
              {runtime.ctx ? <span className="text-success/70">{Math.round(runtime.ctx / 1024)}K ctx</span> : null}
              {runtime.mtp ? <span className="text-success/70" title={`Multi-token prediction on (draft depth ${runtime.mtpNMax ?? 3})`}>· MTP</span> : null}
              {runtime.verifying ? <span className="text-success/70" title="Confirming the model can generate…">· verifying…</span> : null}
              <button onClick={stopRuntime} title="Stop" className="ml-1 text-success/70 hover:text-success">✕</button>
            </span>
          ) : runtime?.loading ? (
            <span className="flex items-center gap-1.5 rounded-full bg-soft px-3 py-1 text-xs text-body"><Spinner /> Loading model…</span>
          ) : runtime?.failed ? (
            <span className="flex items-center gap-1.5 rounded-full bg-danger-soft px-3 py-1 text-xs text-danger" title={runtime.error ?? undefined}>
              <span className="size-2 rounded-full bg-danger" /> Load failed{runtime.modelName ? <>: <span className="font-mono">{runtime.modelName}</span></> : null}
            </span>
          ) : (
            <span className="rounded-full bg-soft px-3 py-1 text-xs text-mute">No active model</span>
          )}
          <CapBadge label="Local" value={cap ? undefined : "…"} count />
          {/* Unified memory (Apple Silicon): VRAM and RAM are one pool — show once. */}
          {cap?.vramKind === "unified" && cap?.vramGb != null ? (
            <CapBadge label={`${cap.vramGb} GB`} sub="VRAM / RAM" />
          ) : (
            <>
              {cap?.vramGb != null && <CapBadge label={`${cap.vramGb} GB VRAM`} sub={cap.vramKind} />}
              {cap?.cpuRamGb != null && <CapBadge label={`${cap.cpuRamGb} GB RAM`} />}
            </>
          )}
          {cap?.cpuCores != null && <CapBadge label={`${cap.cpuCores}-core CPU`} sub={cap.cpuBrand ?? undefined} />}
          {cap?.diskFreeGb != null && <CapBadge label={`${cap.diskFreeGb} GB free`} sub="disk" />}
        </div>
      </div>

      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2 px-6 py-3">
        <div className="flex rounded-full bg-soft p-0.5">
          {(["discover", "device"] as SubTab[]).map((t) => (
            <button
              key={t}
              onClick={() => setSub(t)}
              className={`rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${
                sub === t ? "bg-ink text-white shadow-sm" : "text-body hover:text-ink"
              }`}
            >{t === "discover" ? "Discover" : "On Device"}</button>
          ))}
        </div>

        {sub === "discover" ? (
          <>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search HuggingFace — Qwen, Llama, Mistral, Unsloth…"
              className="min-w-[180px] flex-1 rounded-xl border border-pressed bg-canvas px-4 py-1.5 text-sm text-ink outline-none placeholder:text-mute focus:border-primary"
            />
            <Select value={format} onChange={(v) => { setFormat(v); setSelected(null); }} options={FORMATS} />
            <Select value={capFilter} onChange={setCapFilter} options={CAPS} />
            <Select value={sort} onChange={setSort} options={SORTS} />
            <button onClick={loadModels} className="rounded-full border border-pressed p-2 text-body hover:bg-soft hover:text-ink" title="Refresh">
              {listLoading ? <Spinner /> : <RefreshIcon />}
            </button>
          </>
        ) : (
          <>
            <span className="ml-1 text-sm text-body">{local.length} model{local.length === 1 ? "" : "s"} on device</span>
            <span className="ml-auto text-xs text-mute" title="Each model defaults to the largest context that likely fits this machine, up to its trained max. Adjust per model before activating.">
              Context defaults to each model's memory-safe max
            </span>
            <button onClick={loadLocal} className="rounded-full border border-pressed bg-canvas px-3.5 py-1.5 text-sm text-body hover:bg-soft hover:text-ink">
              {localLoading ? <Spinner /> : "Refresh"}
            </button>
          </>
        )}
      </div>

      {/* Active downloads strip */}
      {activeJobs.length > 0 && (
        <div className="mx-6 mb-1 space-y-2 rounded-2xl border border-line bg-canvas p-3">
          {activeJobs.map((j) => (
            <div key={j.id} className="flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-xs">
                  <span className="truncate font-mono text-ink">{j.filename || j.repo}</span>
                  <span className="ml-auto shrink-0 font-mono text-mute">
                    {fmtBytes(j.receivedBytes)}{j.totalBytes ? ` / ${fmtBytes(j.totalBytes)}` : ""}
                    {j.percent != null ? ` · ${j.percent}%` : ""}
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-soft">
                  <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${j.percent ?? 5}%` }} />
                </div>
              </div>
              <button onClick={() => cancelJob(j.id)} className="rounded-full border border-pressed px-3 py-1 text-xs text-body hover:bg-soft hover:text-ink">Cancel</button>
            </div>
          ))}
        </div>
      )}

      {/* Failed downloads — so a failure is never silent */}
      {failedJobs.length > 0 && (
        <div className="mx-6 mb-1 space-y-2">
          {failedJobs.map((j) => (
            <div key={j.id} className="flex items-start gap-3 rounded-2xl border border-danger-soft bg-danger-soft p-3">
              <div className="min-w-0 flex-1 text-xs">
                <div className="font-medium text-danger">Download failed · <span className="font-mono">{j.filename || j.repo}</span></div>
                {j.error && <div className="mt-1 break-all font-mono text-[11px] text-body">{j.error}</div>}
              </div>
              <button onClick={() => setDismissed((d) => [...d, j.id])} className="shrink-0 rounded-full border border-pressed bg-canvas px-3 py-1 text-xs text-body hover:bg-soft hover:text-ink">Dismiss</button>
            </div>
          ))}
        </div>
      )}

      {/* Body */}
      {sub === "discover" ? (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-0 lg:grid-cols-2">
          {/* List */}
          <div className="min-h-0 overflow-y-auto border-r border-pressed px-6 py-4">
            <h2 className="mb-3 text-sm font-semibold text-body">Models</h2>
            {listError && (
              <div className="rounded-xl border border-danger-soft bg-danger-soft p-3 font-mono text-[11px] text-danger">{listError}</div>
            )}
            {!listError && listLoading && models.length === 0 && (
              <p className="py-10 text-center text-sm text-mute">Searching…</p>
            )}
            {!listLoading && !listError && models.length === 0 && (
              <p className="py-10 text-center text-sm text-mute">No models found.</p>
            )}
            <div className="space-y-1.5">
              {models.map((m) => (
                <button
                  key={m.id}
                  onClick={() => setSelected(m.id)}
                  className={`flex w-full items-center gap-3 rounded-2xl border p-2.5 text-left transition-colors ${
                    selected === m.id ? "border-primary-line bg-primary-soft" : "border-line bg-canvas hover:bg-soft"
                  }`}
                >
                  <Avatar name={m.id.split("/")[1] || m.author} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-ink">{m.id.split("/")[1] || m.id}</div>
                    <div className="truncate text-xs text-mute">{m.author}{m.gated ? " · gated" : ""}</div>
                  </div>
                  <div className="shrink-0 text-right text-[11px] text-mute">
                    <div>♥ {fmtCount(m.likes)} · ↓ {fmtCount(m.downloads)}</div>
                    <div>{fmtAgo(m.lastModified)}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Detail */}
          <div className="min-h-0 overflow-y-auto px-6 py-4">
            {!selected && <p className="py-10 text-center text-sm text-mute">Select a model to see details.</p>}
            {selected && detailLoading && !detail && <p className="py-10 text-center text-sm text-mute"><Spinner /></p>}
            {detail && (
              <DetailPanel
                detail={detail}
                onDownload={(f) => download({ source: "hf", repo: detail.id, filename: f.filename })}
                onDownloadRepo={() => download({ source: "hf", repo: detail.id, kind: "repo" })}
              />
            )}
          </div>
        </div>
      ) : (
        // On Device
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {localLoading && local.length === 0 && <p className="py-10 text-center text-sm text-mute"><Spinner /></p>}
          {!localLoading && local.length === 0 && <p className="py-10 text-center text-sm text-mute">No models on device yet. Download some from Discover.</p>}
          <div className="space-y-1.5">
            {local.map((m, i) => (
              <div key={`${m.repo}-${m.filename}-${i}`} className="flex items-center gap-3 rounded-2xl border border-line bg-canvas p-3">
                <Avatar name={m.repo.split("/").pop() || m.repo} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-ink">{m.filename}</div>
                  <div className="truncate text-xs text-mute">{m.repo}</div>
                </div>
                {m.quant && <Chip>{m.quant}</Chip>}
                <span className="rounded-full bg-soft px-2 py-0.5 text-[10px] text-body">
                  {m.backend === "mlx" ? "MLX" : "GGUF"}
                </span>
                {m.source === "finetuned" && (
                  <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary" title="Fused output of a local fine-tune (Train tab)">
                    fine-tuned
                  </span>
                )}
                {m.multimodal && <span className="rounded-full bg-info/10 px-2 py-0.5 text-[10px] text-info">vision</span>}
                {(() => {
                  const v = m.backend !== "mlx"
                    ? { trainable: false, reason: "GGUF — inference only" }
                    : (trainMap[m.repo] ?? { trainable: !m.multimodal, reason: m.multimodal ? "multimodal" : null });
                  return (
                    <span
                      title={v.trainable ? "Can be fine-tuned in the Train tab" : `Not trainable — ${v.reason ?? "unsupported"}`}
                      className={`rounded-full px-2 py-0.5 text-[10px] ${v.trainable ? "bg-success-soft text-success" : "bg-soft text-mute"}`}
                    >{v.trainable ? "trainable" : "inference-only"}</span>
                  );
                })()}
                <span className="font-mono text-xs text-body">{fmtBytes(m.sizeBytes)}</span>
                {/* Activate in the matching engine (llama.cpp / mlx / mlx-vlm) */}
                {m.path && (() => {
                  const binOk = m.backend !== "mlx" ? runtime?.binLlamacpp
                    : m.multimodal ? runtime?.binMlxVlm : runtime?.binMlx;
                  const missingMsg = m.backend !== "mlx" ? "llama-server not installed"
                    : m.multimodal ? "mlx-vlm not installed — pip install mlx-vlm"
                    : "mlx-lm not installed — pip install mlx-lm";
                  if (isActive(m)) return <span className="flex items-center gap-1 rounded-full bg-success-soft px-3 py-1 text-[11px] font-semibold text-success">● Active</span>;
                  if (activating === m.path) return <span className="rounded-full border border-pressed px-3 py-1 text-[11px] text-body"><Spinner /></span>;
                  return (
                    <div className="flex items-center gap-1.5">
                      {/* MTP self-speculative decoding — only offered for GGUFs with an
                          embedded MTP head; faster with identical output. */}
                      {m.backend !== "mlx" && m.mtpCapable && (
                        <label
                          title="Multi-token prediction: self-speculative decoding off this model's built-in MTP head — faster, identical output"
                          className="flex cursor-pointer items-center gap-1 rounded-lg border border-pressed px-2 py-1 text-[11px] text-body select-none"
                        >
                          <input
                            type="checkbox"
                            checked={!!mtpByPath[m.path!]}
                            onChange={(e) => setMtpByPath((p) => ({ ...p, [m.path!]: e.target.checked }))}
                          />
                          MTP
                        </label>
                      )}
                      {/* Context window for this model — defaults to its memory-safe max */}
                      {m.backend !== "mlx" && (
                        <Select
                          value={String(ctxByPath[m.path!] ?? defaultCtxFor(m))}
                          onChange={(v) => setCtxByPath((p) => ({ ...p, [m.path!]: Number(v) }))}
                          options={ctxOptionsFor(m)}
                        />
                      )}
                      <button
                        onClick={() => activate(m)}
                        disabled={!binOk || !!activating}
                        title={binOk ? "Load this model" : missingMsg}
                        className="rounded-xl bg-primary px-4 py-1.5 text-[11px] font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep disabled:opacity-40 disabled:shadow-none"
                      >Activate</button>
                    </div>
                  );
                })()}
                {/* Delete to free disk (not while active) */}
                {m.path && !isActive(m) && (
                  <button
                    onClick={() => deleteLocal(m)}
                    title="Delete to free disk"
                    className="rounded-full border border-pressed px-2.5 py-1 text-[11px] text-mute hover:border-danger hover:bg-danger-soft hover:text-danger"
                  >Delete</button>
                )}
              </div>
            ))}
          </div>
          {runtime?.error && (
            <pre className="mt-3 max-h-40 overflow-auto rounded-xl border border-danger-soft bg-danger-soft p-3 font-mono text-[10px] text-danger whitespace-pre-wrap">{runtime.error}</pre>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Detail panel
// --------------------------------------------------------------------------- //

function DetailPanel({ detail, onDownload, onDownloadRepo }: {
  detail: ModelDetail; onDownload: (f: GgufFile) => void; onDownloadRepo: () => void;
}) {
  const [fileIdx, setFileIdx] = useState(0);
  useEffect(() => { setFileIdx(0); }, [detail.id]);
  const file = detail.files[fileIdx];
  const name = detail.id.split("/")[1] || detail.id;

  return (
    <div>
      <div className="flex items-start gap-3">
        <Avatar name={name} />
        <div className="min-w-0">
          <h2 className="font-display break-words text-xl font-bold tracking-tight text-ink">{name}</h2>
          <a href={`https://huggingface.co/${detail.id}`} target="_blank" rel="noreferrer"
            className="text-sm text-body underline-offset-2 hover:underline">{detail.author} ↗</a>
        </div>
      </div>

      {/* Capability tags */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {detail.pipelineTag && <Chip>{detail.pipelineTag}</Chip>}
        {detail.tags.slice(0, 6).map((t) => <Chip key={t}>{t}</Chip>)}
      </div>
      {detail.baseModel && <p className="mt-2 text-xs text-mute">Base: <span className="font-mono text-body">{detail.baseModel}</span></p>}

      {/* MLX: whole-repo download (no per-file quants) */}
      {detail.mlx ? (
        <div className="mt-5 rounded-2xl border border-line bg-canvas p-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <span className="font-mono text-sm text-body">{fmtBytes(detail.repoSizeBytes)}</span>
              <span className={`ml-2 rounded-full border px-2.5 py-0.5 text-[11px] ${FIT_BADGE[detail.repoFit].cls}`}>{FIT_BADGE[detail.repoFit].label}</span>
            </div>
            {detail.repoCached ? (
              <span className="rounded-full bg-success-soft px-4 py-2 text-sm font-semibold text-success">On device ✓</span>
            ) : (
              <button onClick={onDownloadRepo} className="flex items-center gap-1.5 rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep">
                <DownloadIcon /> Download model
              </button>
            )}
          </div>
          <p className="mt-2 text-[11px] text-mute">MLX downloads the whole repo (weights + tokenizer). Runs via mlx_lm.server on Apple Silicon.</p>
        </div>
      ) : (
      /* GGUF: file picker + download */
      <div className="mt-5 rounded-2xl border border-line bg-canvas p-3">
        {detail.files.length === 0 ? (
          <p className="text-sm text-mute">No GGUF files in this repo.</p>
        ) : (
          <>
            <div className="flex items-center gap-2">
              <select
                value={fileIdx}
                onChange={(e) => setFileIdx(Number(e.target.value))}
                className="min-w-0 flex-1 rounded-lg border border-pressed bg-soft px-2.5 py-1.5 font-mono text-xs text-ink outline-none focus:border-primary"
              >
                {detail.files.map((f, i) => (
                  <option key={f.filename} value={i}>
                    {f.quant ? `${f.quant} · ` : ""}{fmtBytes(f.sizeBytes)} · {f.filename}
                  </option>
                ))}
              </select>
              {file && (
                <span className={`shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] ${FIT_BADGE[file.fit].cls}`}>
                  {FIT_BADGE[file.fit].label}
                </span>
              )}
            </div>
            {file && (
              <div className="mt-3 flex items-center justify-between gap-3">
                <span className="font-mono text-sm text-body">{fmtBytes(file.sizeBytes)}</span>
                {file.cached ? (
                  <span className="rounded-full bg-success-soft px-4 py-2 text-sm font-semibold text-success">On device ✓</span>
                ) : (
                  <button
                    onClick={() => onDownload(file)}
                    className="flex items-center gap-1.5 rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep"
                  ><DownloadIcon /> Download</button>
                )}
              </div>
            )}
            {file?.fit === "too_large" && (
              <p className="mt-2 text-[11px] text-mute">This file is larger than your available memory — pick a smaller quant.</p>
            )}
          </>
        )}
      </div>
      )}

      {/* Meta */}
      <div className="mt-4 flex flex-wrap gap-1.5">
        <Chip>↓ {fmtCount(detail.downloads)}</Chip>
        <Chip>♥ {fmtCount(detail.likes)}</Chip>
        {detail.lastModified && <Chip>{fmtAgo(detail.lastModified)}</Chip>}
        {detail.license && <Chip>{detail.license}</Chip>}
        {detail.gated && <Chip>gated</Chip>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Capability badge (header)
// --------------------------------------------------------------------------- //

function CapBadge({ label, value, sub, dim, count }: {
  label: string; value?: string; sub?: string; dim?: boolean; count?: boolean;
}) {
  return (
    <span className={`flex items-center gap-1.5 rounded-full bg-soft px-3 py-1 text-xs ${dim ? "text-mute" : "text-body"}`}>
      {count && <span className="size-1.5 rounded-full bg-primary" />}
      <span className="font-medium">{value ?? label}</span>
      {sub && <span className="text-mute">{sub}</span>}
    </span>
  );
}

export default HubPage;
