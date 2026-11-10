// Train — local LoRA fine-tuning (MLX · Apple Silicon). Build a dataset from Chat
// feedback or upload JSONL, train, watch loss live, then serve the result as the
// on-device model. Backend: /api/finetune (runtime/finetune_manager.py).

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";

interface Capability {
  appleSilicon: boolean; mlxInstalled: boolean; mlxVlmInstalled: boolean;
  setup: { status: string; output?: string };
}
interface BaseModel {
  repo: string; name: string; path: string; quant?: string | null; sizeGb?: number;
  trainable: boolean; reason?: string | null; trainer?: string | null;
}
interface Dataset { id: string; name: string; examples: number; format?: string; source?: string; createdAt: number }
interface Metric { iter: number; trainLoss?: number; valLoss?: number }
interface Job {
  id: string; name: string; status: string; examples?: number; createdAt: number; finishedAt?: number | null;
  config: { baseModel?: string; iters?: number; datasetName?: string; trainer?: string };
  metrics: Metric[]; logTail?: string[]; progress?: { iter: number; total: number };
  outputPath?: string | null; error?: string | null;
}
interface FeedbackCounts { up: number; down: number; edit: number; total: number; trainable: number }

type SubTab = "configure" | "run" | "history";

const CFG_KEY = "vw:train-config";
const RUNNING = ["queued", "preparing", "running", "fusing", "cancelling"];
const FINISHED = ["done", "error", "cancelled", "interrupted"];

const DEFAULTS = {
  localModel: "", hfModel: "", datasetId: "", name: "",
  useEpochs: false, iters: 300, epochs: 3,
  maxSeqLength: 2048, learningRate: 0.0002, batchSize: 1,
  numLayers: 8, loraRank: 8, loraAlpha: 16, loraDropout: 0.0,
};
type Cfg = typeof DEFAULTS;

function loadCfg(): Cfg {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(CFG_KEY) || "{}") }; }
  catch { return { ...DEFAULTS }; }
}

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary focus:ring-2 focus:ring-primary-soft";

// --------------------------------------------------------------------------- //
// Atoms
// --------------------------------------------------------------------------- //

function Card({ title, sub, children, className = "" }: { title: string; sub?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl border border-line bg-canvas p-5 ${className}`}>
      <h3 className="font-display text-sm font-semibold text-ink">{title}</h3>
      {sub && <p className="mt-0.5 text-xs text-body">{sub}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <span className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
      {children}{hint && <span className="cursor-help text-mute" title={hint}>ⓘ</span>}
    </span>
  );
}

function Btn({ children, onClick, disabled, variant = "ghost", className = "", title }: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean;
  variant?: "primary" | "ghost" | "danger"; className?: string; title?: string;
}) {
  const v = variant === "primary" ? "bg-primary text-white hover:bg-primary-deep disabled:opacity-40"
    : variant === "danger" ? "bg-danger text-white hover:opacity-90 disabled:opacity-40"
    : "border border-pressed bg-canvas text-body hover:bg-soft hover:text-ink disabled:opacity-40";
  return (
    <button type="button" title={title} onClick={onClick} disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed ${v} ${className}`}>
      {children}
    </button>
  );
}

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden />;
}

function StatusPill({ status }: { status: string }) {
  const running = RUNNING.includes(status);
  const cls = status === "done" ? "bg-success-soft text-success"
    : status === "error" || status === "interrupted" ? "bg-danger-soft text-danger"
    : running ? "bg-primary-soft text-primary-deep" : "bg-soft text-body";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {running && <span className="inline-block size-1.5 animate-pulse rounded-full bg-current" />}{status}
    </span>
  );
}

function LossChart({ metrics, height = 160 }: { metrics: Metric[]; height?: number }) {
  const train = metrics.filter((m) => typeof m.trainLoss === "number");
  const val = metrics.filter((m) => typeof m.valLoss === "number");
  if (train.length < 2) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-line bg-softer text-center" style={{ height }}>
        <p className="text-sm text-body">No loss data yet</p>
        <p className="mt-0.5 text-xs text-mute">The curve appears once training reports a few steps</p>
      </div>
    );
  }
  const W = 600, H = height, pad = 10;
  const all = [...train.map((m) => m.trainLoss as number), ...val.map((m) => m.valLoss as number)];
  const xs = [...train, ...val].map((m) => m.iter);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...all), maxY = Math.max(...all);
  const sx = (x: number) => pad + ((x - minX) / Math.max(1, maxX - minX)) * (W - 2 * pad);
  const sy = (y: number) => pad + (1 - (y - minY) / Math.max(1e-9, maxY - minY)) * (H - 2 * pad);
  const path = (pts: Metric[], key: "trainLoss" | "valLoss") =>
    pts.map((m, i) => `${i ? "L" : "M"}${sx(m.iter).toFixed(1)},${sy(m[key] as number).toFixed(1)}`).join(" ");
  return (
    <div>
      <div className="rounded-xl border border-line bg-softer p-2" style={{ height }}>
        <svg viewBox={`0 0 ${W} ${H}`} className="h-full w-full" preserveAspectRatio="none" role="img"
          aria-label={`Training loss from ${train[0].trainLoss?.toFixed(3)} to ${train[train.length - 1].trainLoss?.toFixed(3)}`}>
          {[0, 0.25, 0.5, 0.75, 1].map((g) => {
            const y = pad + g * (H - 2 * pad);
            return <line key={g} x1={pad} y1={y} x2={W - pad} y2={y} stroke="var(--color-line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />;
          })}
          <path d={path(train, "trainLoss")} fill="none" stroke="var(--color-primary)" strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
          {val.length > 1 && <path d={path(val, "valLoss")} fill="none" stroke="var(--color-warn)" strokeWidth="2" strokeDasharray="5 4" vectorEffect="non-scaling-stroke" />}
        </svg>
      </div>
      <div className="mt-1.5 flex gap-4 text-[11px] text-mute">
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-primary" />train {minY.toFixed(3)}–{maxY.toFixed(3)}</span>
        {val.length > 0 && <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-warn" />validation</span>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function TrainPage({ onOpenChat }: { onOpenChat?: () => void }) {
  const [sub, setSub] = useState<SubTab>("configure");
  const [cap, setCap] = useState<Capability | null>(null);
  const [models, setModels] = useState<BaseModel[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [fb, setFb] = useState<FeedbackCounts | null>(null);
  const [cfg, setCfg] = useState<Cfg>(loadCfg);
  const set = <K extends keyof Cfg>(k: K, v: Cfg[K]) => setCfg((c) => ({ ...c, [k]: v }));

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState<"" | "upload" | "build" | "start" | "setup">("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [run, setRun] = useState<Job | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const baseModel = cfg.hfModel.trim() || cfg.localModel;
  const dataset = datasets.find((d) => d.id === cfg.datasetId);
  const activeJob = jobs.find((j) => RUNNING.includes(j.status));

  const loadCap = useCallback(async () => { try { setCap(await apiJson<Capability>("/api/finetune/capability")); } catch { /* shown via actions */ } }, []);
  const loadModels = useCallback(async () => { try { setModels((await apiJson<{ models: BaseModel[] }>("/api/finetune/base-models")).models); } catch { /* */ } }, []);
  const loadDatasets = useCallback(async () => { try { setDatasets((await apiJson<{ datasets: Dataset[] }>("/api/finetune/datasets")).datasets); } catch { /* */ } }, []);
  const loadJobs = useCallback(async () => { try { setJobs((await apiJson<{ jobs: Job[] }>("/api/finetune/jobs")).jobs); } catch { /* */ } }, []);
  const loadFb = useCallback(async () => { try { setFb(await apiJson<FeedbackCounts>("/api/feedback/counts")); } catch { /* */ } }, []);

  useEffect(() => { loadCap(); loadModels(); loadDatasets(); loadJobs(); loadFb(); }, [loadCap, loadModels, loadDatasets, loadJobs, loadFb]);
  useEffect(() => { try { localStorage.setItem(CFG_KEY, JSON.stringify(cfg)); } catch { /* private mode */ } }, [cfg]);
  useEffect(() => () => { esRef.current?.close(); if (pollRef.current) clearInterval(pollRef.current); }, []);

  const attach = useCallback((jobId: string) => {
    esRef.current?.close();
    const es = new EventSource(`/api/finetune/jobs/${encodeURIComponent(jobId)}/stream`);
    esRef.current = es;
    es.onmessage = (e) => {
      let evt: Record<string, unknown>;
      try { evt = JSON.parse(e.data); } catch { return; }
      setRun((prev) => {
        if (!prev || prev.id !== jobId) return prev;
        const next = { ...prev };
        if (evt.type === "snapshot") {
          next.metrics = (evt.metrics as Metric[]) ?? next.metrics;
          next.logTail = (evt.logTail as string[]) ?? next.logTail;
          next.status = (evt.status as string) ?? next.status;
          next.progress = (evt.progress as Job["progress"]) ?? next.progress;
          next.outputPath = (evt.outputPath as string) ?? next.outputPath;
          next.error = (evt.error as string) ?? next.error;
        } else if (evt.type === "metric") {
          const m = evt as unknown as Metric;
          next.metrics = [...next.metrics, { iter: m.iter, trainLoss: m.trainLoss, valLoss: m.valLoss }];
          if (m.trainLoss != null) next.progress = { iter: m.iter, total: next.progress?.total ?? 0 };
        } else if (evt.type === "progress") {
          next.progress = { iter: evt.iter as number, total: evt.total as number };
        } else if (evt.type === "log") {
          next.logTail = [...(next.logTail || []), evt.line as string].slice(-400);
        } else if (evt.type === "status") {
          next.status = evt.status as string;
          if (evt.outputPath) next.outputPath = evt.outputPath as string;
          if (evt.error) next.error = evt.error as string;
        } else if (evt.type === "done") {
          next.status = "done"; next.outputPath = evt.outputPath as string;
        }
        return next;
      });
      const st = evt.type === "done" ? "done" : (evt.type === "status" || evt.type === "snapshot") ? (evt.status as string) : "";
      if (FINISHED.includes(st)) { es.close(); loadJobs(); loadModels(); }
    };
    es.onerror = () => { /* the server ends the stream when the job finishes */ };
  }, [loadJobs, loadModels]);

  // Coming back to the page (or reloading) while a job trains: reattach to it.
  const reattached = useRef(false);
  useEffect(() => {
    if (reattached.current || run || !activeJob) return;
    reattached.current = true;
    setRun(activeJob);
    attach(activeJob.id);
  }, [activeJob, run, attach]);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  const setupMlx = async () => {
    setBusy("setup"); setError(null);
    try {
      await apiPost("/api/finetune/setup", {});
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        const c = await apiJson<Capability>("/api/finetune/capability").catch(() => null);
        if (c) setCap(c);
        if (!c || c.setup.status !== "running") {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null; setBusy("");
        }
      }, 3000);
    } catch (e) { fail(e); setBusy(""); }
  };

  const upload = async (file: File) => {
    setBusy("upload"); setError(null); setNotice(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await apiFetch("/api/finetune/datasets", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `upload failed (HTTP ${res.status})`);
      await loadDatasets();
      set("datasetId", data.id);
    } catch (e) { fail(e); } finally { setBusy(""); }
  };

  const buildFromFeedback = async () => {
    setBusy("build"); setError(null); setNotice(null);
    try {
      const r = await apiPost<Dataset & { positives: number; corrections: number }>("/api/finetune/datasets/feedback", {});
      await loadDatasets();
      set("datasetId", r.id);
      setNotice(`Built “${r.name}”: ${r.examples} examples (${r.positives} rated up, ${r.corrections} corrected). Pick a base model and start training.`);
    } catch (e) { fail(e); } finally { setBusy(""); loadFb(); }
  };

  const removeDataset = async (d: Dataset) => {
    if (!confirm(`Delete dataset “${d.name}”?`)) return;
    try {
      await apiJson(`/api/finetune/datasets/${d.id}`, { method: "DELETE" });
      if (cfg.datasetId === d.id) set("datasetId", "");
      loadDatasets();
    } catch (e) { fail(e); }
  };

  const start = async () => {
    if (!baseModel || !dataset) return;
    setBusy("start"); setError(null); setNotice(null);
    try {
      const job = await apiPost<Job>("/api/finetune/jobs", {
        baseModel, datasetId: dataset.id, name: cfg.name.trim() || undefined,
        trainer: models.find((m) => m.path === cfg.localModel && !cfg.hfModel.trim())?.trainer ?? undefined,
        useEpochs: cfg.useEpochs, iters: cfg.iters, epochs: cfg.epochs, maxSeqLength: cfg.maxSeqLength,
        learningRate: cfg.learningRate, batchSize: cfg.batchSize, numLayers: cfg.numLayers,
        loraRank: cfg.loraRank, loraAlpha: cfg.loraAlpha, loraDropout: cfg.loraDropout,
      });
      setRun({ ...job, metrics: [], logTail: [] });
      setSub("run");
      attach(job.id);
      loadJobs();
    } catch (e) { fail(e); } finally { setBusy(""); }
  };

  const stop = async () => { if (run) await apiPost(`/api/finetune/jobs/${run.id}/cancel`, {}).catch(fail); };
  const activate = async (job: Job) => {
    setError(null);
    try { await apiPost(`/api/finetune/jobs/${job.id}/activate`, {}); setNotice(`Loading “${job.name}” as the on-device model…`); return true; }
    catch (e) { fail(e); return false; }
  };
  const openRun = (job: Job) => { setRun(job); setSub("run"); if (RUNNING.includes(job.status)) attach(job.id); };
  const removeJob = async (job: Job) => {
    if (!confirm(`Delete “${job.name}” and its trained model? This frees disk and can't be undone.`)) return;
    try {
      await apiJson(`/api/finetune/jobs/${job.id}`, { method: "DELETE" });
      if (run?.id === job.id) setRun(null);
      loadJobs(); loadModels();
    } catch (e) { fail(e); }
  };

  const trainable = models.filter((m) => m.trainable);
  const installing = busy === "setup" || cap?.setup.status === "running";

  return (
    <div className="mx-auto max-w-[1200px] px-4 py-6 sm:px-6">
      <div className="mb-5">
        <h1 className="font-display text-2xl font-bold tracking-tight text-ink">Train</h1>
        <p className="mt-0.5 text-sm text-body">
          Fine-tune a local model on your own examples with LoRA (MLX, Apple Silicon). Everything stays on this machine.
        </p>
      </div>

      <div className="mb-5 inline-flex gap-1 rounded-full border border-line bg-canvas p-1" role="tablist">
        {(["configure", "run", "history"] as SubTab[]).map((t) => (
          <button key={t} role="tab" aria-selected={sub === t} onClick={() => { setSub(t); if (t === "history") loadJobs(); }}
            className={`rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${sub === t ? "bg-primary text-white" : "text-body hover:bg-soft hover:text-ink"}`}>
            {t === "configure" ? "Configure" : t === "run" ? "Current run" : "History"}
          </button>
        ))}
      </div>

      {cap && !cap.mlxInstalled && (
        <div className="mb-5 rounded-2xl border border-warn/30 bg-warn-soft p-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-warn">MLX is not installed</p>
              <p className="mt-0.5 text-xs text-body">
                {cap.appleSilicon
                  ? "Training needs mlx-lm. Install downloads it from PyPI into Virtual Worker's MLX engine."
                  : "Fine-tuning here needs Apple Silicon (MLX). This machine isn't an arm64 Mac."}
              </p>
            </div>
            {cap.appleSilicon && (
              <Btn variant="primary" onClick={setupMlx} disabled={installing}>
                {installing ? <><Spinner /> Installing…</> : "Install MLX"}
              </Btn>
            )}
          </div>
          {cap.setup.status === "error" && cap.setup.output && (
            <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-xl bg-panel p-2 font-mono text-[11px] text-panel-ink">{cap.setup.output}</pre>
          )}
        </div>
      )}

      {error && <div role="alert" className="mb-4 rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger">{error}</div>}
      {notice && <div className="mb-4 rounded-xl border border-success/30 bg-success-soft px-3 py-2 text-xs text-success">{notice}</div>}

      {sub === "configure" && (
        <div className="space-y-5">
          <Card title="From feedback"
            sub="Replies you rated up or corrected in Chat become training examples. Rated-down replies are left out."
            className="border-primary-line bg-primary-soft/40">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="rounded-full bg-success-soft px-2.5 py-0.5 font-semibold text-success">{fb?.up ?? 0} up</span>
                <span className="rounded-full bg-primary-soft px-2.5 py-0.5 font-semibold text-primary-deep">{fb?.edit ?? 0} corrected</span>
                <span className="rounded-full bg-soft px-2.5 py-0.5 text-mute">{fb?.down ?? 0} down, excluded</span>
              </div>
              <Btn variant="primary" className="sm:ml-auto" onClick={buildFromFeedback}
                disabled={busy === "build" || !fb || fb.trainable === 0}>
                {busy === "build" ? <><Spinner /> Building…</> : `Build dataset (${fb?.trainable ?? 0})`}
              </Btn>
            </div>
            {fb && fb.trainable === 0 && (
              <p className="mt-3 text-xs text-body">No usable feedback yet. In Chat, use the thumbs or the pencil under a reply, then come back here.</p>
            )}
          </Card>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            <Card title="Base model" sub="The model the LoRA adapter is trained on top of">
              <label className="block">
                <Label hint="Downloaded models with safetensors weights. GGUF-only, speech and encoder models can't be trained and are disabled.">On this machine</Label>
                <select value={cfg.localModel} onChange={(e) => { set("localModel", e.target.value); if (e.target.value) set("hfModel", ""); }} className={inputCls}>
                  <option value="">Choose a model…</option>
                  {models.map((m) => (
                    <option key={m.path} value={m.path} disabled={!m.trainable}>
                      {m.repo}{m.quant ? ` · ${m.quant}` : ""}{m.sizeGb ? ` · ${m.sizeGb} GB` : ""}
                      {m.trainable ? (m.trainer === "mlx-vlm" ? " · text side only" : "") : ` (${m.reason ?? "not trainable"})`}
                    </option>
                  ))}
                </select>
              </label>
              <p className="mt-1 flex items-center gap-2 text-[11px] text-mute">
                {trainable.length} trainable of {models.length}
                <button type="button" onClick={loadModels} className="text-primary hover:underline">Refresh</button>
              </p>
              {models.length > 0 && trainable.length === 0 && (
                <p className="mt-2 text-xs text-body">Download an MLX model (for example from the mlx-community org) on the Models page to train on it.</p>
              )}
              <label className="mt-4 block">
                <Label hint="Downloads the model from Hugging Face when training starts.">Or a Hugging Face id</Label>
                <input value={cfg.hfModel} onChange={(e) => set("hfModel", e.target.value)} placeholder="mlx-community/Qwen2.5-3B-Instruct-4bit" className={inputCls} />
              </label>
              {baseModel && <p className="mt-2 break-all font-mono text-[11px] text-mute">base: {baseModel}</p>}
            </Card>

            <Card title="Dataset" sub="One JSON object per line, chat format recommended">
              {datasets.length > 0 && (
                <div className="mb-3 flex gap-2">
                  <select value={cfg.datasetId} onChange={(e) => set("datasetId", e.target.value)} className={inputCls} aria-label="Dataset">
                    <option value="">Choose a dataset…</option>
                    {datasets.map((d) => (
                      <option key={d.id} value={d.id}>{d.name} · {d.examples} examples{d.source === "feedback" ? " · from feedback" : ""}</option>
                    ))}
                  </select>
                  {dataset && <Btn onClick={() => removeDataset(dataset)} title="Delete this dataset">Delete</Btn>}
                </div>
              )}
              <label className="flex cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed border-pressed bg-softer px-4 py-6 text-center transition-colors hover:border-primary-line hover:bg-soft">
                <input type="file" accept=".jsonl,.json" className="sr-only"
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.currentTarget.value = ""; }} />
                {busy === "upload" ? <Spinner /> : <>
                  <span className="text-sm font-medium text-ink">Upload a JSONL file</span>
                  <span className="mt-1 break-all font-mono text-[11px] text-mute">{'{"messages":[{"role":"user","content":"…"},{"role":"assistant","content":"…"}]}'}</span>
                </>}
              </label>
              {dataset && <p className="mt-2 text-[11px] text-success">Selected: {dataset.name}, {dataset.examples} examples ({dataset.format})</p>}
              <p className="mt-2 text-[11px] text-mute">A handful of examples only proves the loop works. A real change in behaviour needs hundreds.</p>
            </Card>
          </div>

          <Card title="Training" sub="Defaults suit a first run on a small model">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <label className="block">
                <Label hint="Also names the trained model (local/<name> on the Models page).">Name (optional)</Label>
                <input value={cfg.name} onChange={(e) => set("name", e.target.value)} placeholder="rani-v1" className={inputCls} />
              </label>
              <label className="block">
                <span className="mb-1 flex items-center justify-between text-xs font-medium text-body">
                  {cfg.useEpochs ? "Epochs" : "Steps"}
                  <button type="button" onClick={() => set("useEpochs", !cfg.useEpochs)} className="font-medium text-primary hover:underline">
                    {cfg.useEpochs ? "Use steps" : "Use epochs"}
                  </button>
                </span>
                <input type="number" min={1} value={cfg.useEpochs ? cfg.epochs : cfg.iters}
                  onChange={(e) => set(cfg.useEpochs ? "epochs" : "iters", Math.max(1, parseInt(e.target.value) || 1))} className={inputCls} />
              </label>
              <label className="block">
                <Label hint="Longer examples are truncated to this many tokens.">Context length</Label>
                <select value={cfg.maxSeqLength} onChange={(e) => set("maxSeqLength", parseInt(e.target.value))} className={inputCls}>
                  {[512, 1024, 2048, 4096, 8192].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <label className="block">
                <Label hint="2e-4 is a good LoRA default.">Learning rate</Label>
                <input type="number" step="0.0001" min={0} value={cfg.learningRate}
                  onChange={(e) => set("learningRate", parseFloat(e.target.value) || 0.0002)} className={inputCls} />
              </label>
            </div>

            <button type="button" onClick={() => setShowAdvanced((s) => !s)} aria-expanded={showAdvanced}
              className="mt-4 flex items-center gap-1 text-xs font-medium text-body hover:text-ink">
              <span className={`transition-transform ${showAdvanced ? "rotate-90" : ""}`}>›</span> LoRA and batch settings
            </button>
            {showAdvanced && (
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
                {([
                  ["loraRank", "Rank", 1, 1], ["loraAlpha", "Alpha", 1, 1], ["loraDropout", "Dropout", 0.05, 0],
                  ["numLayers", "Layers", 1, 1], ["batchSize", "Batch size", 1, 1],
                ] as const).map(([k, label, step, min]) => (
                  <label key={k} className="block">
                    <Label>{label}</Label>
                    <input type="number" step={step} min={min} value={cfg[k]}
                      onChange={(e) => set(k, Math.max(min, Number(e.target.value) || min))} className={inputCls} />
                  </label>
                ))}
              </div>
            )}

            <div className="mt-5 flex flex-wrap items-center gap-3">
              <Btn variant="primary" onClick={start}
                disabled={!baseModel || !dataset || busy === "start" || !!activeJob || !cap?.mlxInstalled}>
                {busy === "start" ? <><Spinner /> Starting…</> : "Start training"}
              </Btn>
              <Btn onClick={() => setCfg({ ...DEFAULTS })}>Reset settings</Btn>
              <span className="text-xs text-mute">
                {!baseModel ? "Choose a base model." : !dataset ? "Choose or build a dataset." : ""}
                {activeJob && <>A job is running. <button type="button" onClick={() => openRun(activeJob)} className="text-primary hover:underline">Watch it</button></>}
              </span>
            </div>
          </Card>
        </div>
      )}

      {sub === "run" && <RunView run={run} onStop={stop} onActivate={activate} onOpenChat={onOpenChat} />}

      {sub === "history" && (
        <div className="space-y-2">
          {jobs.length === 0 && <p className="rounded-2xl border border-line bg-canvas p-8 text-center text-sm text-body">No training runs yet.</p>}
          {jobs.map((j) => (
            <div key={j.id} className="flex flex-wrap items-center gap-3 rounded-2xl border border-line bg-canvas p-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-ink">{j.name}</span>
                  <StatusPill status={j.status} />
                </div>
                <p className="mt-0.5 truncate font-mono text-[11px] text-mute">
                  {j.config.baseModel} · {j.examples ?? "?"} examples · {j.config.iters ?? "?"} steps · {new Date(j.createdAt * 1000).toLocaleString()}
                </p>
              </div>
              <div className="flex gap-2">
                <Btn onClick={() => openRun(j)}>{RUNNING.includes(j.status) ? "Watch" : "View"}</Btn>
                {j.status === "done" && <Btn variant="primary" onClick={() => activate(j)}>Use on device</Btn>}
                {!RUNNING.includes(j.status) && <Btn onClick={() => removeJob(j)}>Delete</Btn>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function RunView({ run, onStop, onActivate, onOpenChat }: {
  run: Job | null; onStop: () => void; onActivate: (j: Job) => Promise<boolean>; onOpenChat?: () => void;
}) {
  const logRef = useRef<HTMLDivElement | null>(null);
  const [activated, setActivated] = useState(false);
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight }); }, [run?.logTail]);
  useEffect(() => { setActivated(false); }, [run?.id]);

  if (!run) {
    return <p className="rounded-2xl border border-line bg-canvas p-8 text-center text-sm text-body">No run open. Start one from Configure, or open one from History.</p>;
  }
  const running = RUNNING.includes(run.status);
  const pct = run.progress?.total ? Math.min(100, Math.round((run.progress.iter / run.progress.total) * 100)) : 0;
  const lastTrain = [...run.metrics].reverse().find((m) => m.trainLoss != null)?.trainLoss;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-display text-base font-semibold text-ink">{run.name}</span>
        <StatusPill status={run.status} />
        {lastTrain != null && <span className="font-mono text-xs text-body">loss {lastTrain.toFixed(4)}</span>}
        <div className="ml-auto flex gap-2">
          {running && <Btn variant="danger" onClick={onStop} disabled={run.status === "cancelling"}>Stop</Btn>}
          {run.status === "done" && <Btn variant="primary" onClick={async () => setActivated(await onActivate(run))}>Use on device</Btn>}
          {run.status === "done" && activated && onOpenChat && <Btn onClick={onOpenChat}>Open Chat</Btn>}
        </div>
      </div>

      {!!run.progress?.total && (
        <div>
          <div className="mb-1 flex justify-between font-mono text-[11px] text-mute">
            <span>step {run.progress.iter} / {run.progress.total}{run.status === "fusing" ? " · fusing the adapter" : ""}</span><span>{pct}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-soft" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div className="h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {run.error && <div className="rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 font-mono text-xs text-danger">{run.error}</div>}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card title="Loss" sub="Lower is better. Validation rising while training falls means it is memorising.">
          <LossChart metrics={run.metrics} height={220} />
        </Card>
        <Card title="Log" sub="Trainer output">
          <div ref={logRef} className="h-[240px] overflow-y-auto rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink">
            {(run.logTail || []).length === 0
              ? <span className="opacity-60">Waiting for output…</span>
              : (run.logTail || []).map((l, i) => <div key={i} className="whitespace-pre-wrap break-all">{l}</div>)}
          </div>
        </Card>
      </div>

      {run.status === "done" && run.outputPath && (
        <div className="rounded-2xl border border-success/30 bg-success-soft p-4">
          <p className="text-sm font-medium text-ink">Trained model ready</p>
          <p className="mt-0.5 break-all font-mono text-[11px] text-mute">{run.outputPath}</p>
          <p className="mt-2 text-xs text-body">
            Use on device loads it as the active local model, so it answers in Chat, channels and the gateway. It is also listed on the Models page.
            Compare it with the base model on the Evaluate page before relying on it.
          </p>
        </div>
      )}
    </div>
  );
}

export default TrainPage;
