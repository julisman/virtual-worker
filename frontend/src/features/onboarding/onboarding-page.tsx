// First-run setup — a four-step wizard shown until something can answer a chat:
//   1. a model (download a recommended local one, use an existing one, or add a provider key)
//   2. a worker (deploy a template with a name and working folder)
//   3. a chat app (optional: Telegram / Discord token, or go to Channels for WhatsApp)
//   4. done → open Chat with the new worker
// Reads /api/onboarding/status for recommendations; every action uses the same
// APIs the full screens use, so nothing here is a special path.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

interface Rec { label: string; q: string; fmt: "mlx" | "gguf"; why: string; kind: "repo" | "file"; quant?: string }
interface Status {
  needed: boolean; dismissed: boolean; modelActive: boolean; modelLoading: boolean; localModels: number;
  providersConfigured: number; hasModel: boolean; agents: number; channels: number;
  platform: { os: string; appleSilicon: boolean; mlx: boolean; memGb: number | null };
  recommendations: Rec[];
}
interface DiscoverModel { id: string; fits: boolean | null; paramsB: number | null; downloads: number }
interface DetailFile { filename: string; sizeBytes?: number; quant: string | null; fit: string }
interface LocalModel { repo: string; path: string; backend: string; filename: string; sizeBytes?: number }
interface Job { id: string; repo: string; filename: string | null; status: string; error: string | null; percent: number | null; receivedBytes: number; totalBytes: number }
interface Template { id: string; name: string; description?: string; builtin?: boolean; category?: string; variables?: { key: string; label: string; type: string; default?: string; required?: boolean }[] }
interface Provider { id: string; label: string; byok: boolean; models: string[]; hasKey?: boolean; configured?: boolean }

const inputCls = "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";
const btnPrimary = "rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-40";
const btnGhost = "rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-40";

function fmtBytes(n?: number | null) {
  if (!n) return "";
  return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`;
}

type Step = 1 | 2 | 3 | 4;

export function OnboardingPage({ onDone, onSkip }: { onDone: (agentId: string | null) => void; onSkip: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [step, setStep] = useState<Step>(1);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [providerChoice, setProviderChoice] = useState<{ id: string; model: string } | null>(null);

  const load = useCallback(async () => {
    try { setStatus(await apiJson<Status>("/api/onboarding/status")); } catch { /* backend not ready yet */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const finish = async () => {
    await apiPost("/api/onboarding/dismiss", {}).catch(() => {});
    onDone(agentId);
  };
  const skip = async () => {
    await apiPost("/api/onboarding/dismiss", {}).catch(() => {});
    onSkip();
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6 flex items-center gap-3">
        <h1 className="font-display text-[28px] font-semibold text-ink">Welcome to Virtual Worker</h1>
        <button onClick={skip} className="ml-auto text-xs text-mute hover:text-ink">Skip setup</button>
      </div>
      <ol className="mb-8 flex gap-2 text-xs">
        {(["Model", "Worker", "Chat app", "Done"] as const).map((label, i) => {
          const n = (i + 1) as Step;
          const on = n === step, past = n < step;
          return (
            <li key={label} className={`flex items-center gap-2 rounded-full px-3 py-1 ${on ? "bg-primary text-white" : past ? "bg-primary-soft text-primary-deep" : "bg-soft text-mute"}`}>
              <span className="font-bold">{past ? "✓" : n}</span>{label}
            </li>
          );
        })}
      </ol>

      {!status && <p className="text-sm text-mute">Checking your machine…</p>}
      {status && step === 1 && (
        <ModelStep status={status} onReady={(choice) => { setProviderChoice(choice); setStep(2); }} />
      )}
      {status && step === 2 && (
        <WorkerStep providerChoice={providerChoice} onDeployed={(id) => { setAgentId(id); setStep(3); }} />
      )}
      {status && step === 3 && (
        <ChannelStep agentId={agentId} onNext={() => setStep(4)} />
      )}
      {status && step === 4 && (
        <section className="rounded-2xl border border-line bg-canvas p-6">
          <h2 className="font-display text-lg font-semibold text-ink">You're set</h2>
          <p className="mt-2 text-sm text-body">
            Your worker is ready. Chat with it, give it tasks on its board, schedule recurring jobs, or
            message it from a connected chat app. Everything stays on this machine unless you connected
            something.
          </p>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-body">
            <li><b>Agents</b> — tune the persona, tools, skills and guardrails.</li>
            <li><b>Channels</b> — connect WhatsApp, Telegram, Discord or Slack.</li>
            <li><b>Docs</b> — everything else, in-app.</li>
          </ul>
          <div className="mt-5 flex gap-2">
            <button onClick={finish} className={btnPrimary}>Open chat</button>
          </div>
        </section>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 1 — a model
// --------------------------------------------------------------------------- //

function ModelStep({ status, onReady }: { status: Status; onReady: (provider: { id: string; model: string } | null) => void }) {
  const [mode, setMode] = useState<"download" | "existing" | "provider">(status.modelActive ? "existing" : status.localModels > 0 ? "existing" : "download");
  const [pick, setPick] = useState<{ rec: Rec; model: DiscoverModel; file?: DetailFile } | null>(null);
  const [resolving, setResolving] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [activating, setActivating] = useState(false);
  const [local, setLocal] = useState<LocalModel[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provId, setProvId] = useState("openai");
  const [provKey, setProvKey] = useState("");
  const [provModel, setProvModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  // Resolve the first recommendation that the hub can actually find and that fits.
  const [skipped, setSkipped] = useState<string[]>([]);
  useEffect(() => {
    if (mode !== "download" || pick) return;
    let alive = true;
    (async () => {
      setResolving(true); setNote(null);
      // "Try another" moves past the recommendations already shown; when every
      // preset has been skipped, start over from the first.
      const candidates = status.recommendations.filter((r) => !skipped.includes(r.label));
      const order = candidates.length ? candidates : status.recommendations;
      for (const rec of order) {
        try {
          const d = await apiJson<{ models: DiscoverModel[] }>(`/api/hub/discover?q=${encodeURIComponent(rec.q)}&fmt=${rec.fmt}&fit_only=true&limit=8`);
          const m = d.models.find((x) => x.id.toLowerCase().includes(rec.q.toLowerCase()) && x.fits !== false) ?? d.models.find((x) => x.fits !== false);
          if (!m) continue;
          let file: DetailFile | undefined;
          if (rec.kind === "file") {
            const [owner, name] = m.id.split("/");
            const det = await apiJson<{ files: DetailFile[] }>(`/api/hub/model/${owner}/${name}`);
            file = det.files.find((f) => f.quant === rec.quant && f.fit !== "too_large") ?? det.files.find((f) => f.fit === "fits");
            if (!file) continue;
          }
          if (alive) { setPick({ rec, model: m, file }); }
          break;
        } catch { /* try the next preset */ }
      }
      if (alive) { setResolving(false); }
    })();
    return () => { alive = false; };
  }, [mode, status.recommendations, pick, skipped]);

  useEffect(() => {
    if (mode !== "existing") return;
    // Hide projector files, embedding models and other things that can't answer a chat.
    const chatty = (m: LocalModel) => !/mmproj|embed|bge-|e5-|minilm|whisper|parakeet|tts/i.test(`${m.repo}/${m.filename}`);
    apiJson<{ models: LocalModel[] }>("/api/hub/local")
      .then((d) => setLocal(d.models.filter(chatty).sort((a, b) => (a.backend === "mlx" ? -1 : 1) - (b.backend === "mlx" ? -1 : 1))))
      .catch(() => setLocal([]));
  }, [mode]);
  useEffect(() => {
    if (mode !== "provider") return;
    apiJson<{ providers: Provider[] }>("/api/providers").then((d) => {
      const list = d.providers.filter((p) => p.byok && p.id !== "openai-compatible" && p.id !== "opencode-go");
      setProviders(list);
      if (list[0]) { setProvId(list[0].id); setProvModel(list[0].models[0] ?? ""); }
    }).catch((e) => setError(String(e)));
  }, [mode]);

  const pollJobs = useCallback(async (repo: string) => {
    try {
      const d = await apiJson<{ downloads: Job[] }>("/api/hub/downloads");
      const j = d.downloads.find((x) => x.repo === repo);
      if (j) setJob(j);
      return j;
    } catch { return null; }
  }, []);
  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  const activate = useCallback(async (path: string) => {
    setActivating(true); setError(null);
    try {
      await apiPost("/api/runtime/start", { path, force: true });
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const s = await apiJson<{ healthy: boolean; failed?: boolean; error?: string | null }>("/api/runtime/status");
        if (s.healthy) { onReady(null); return; }
        if (s.failed) throw new Error(s.error || "the model server failed to start");
      }
      throw new Error("the model is taking too long to load — check Monitor");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setActivating(false); }
  }, [onReady]);

  const download = async () => {
    if (!pick) return;
    setBusy(true); setError(null);
    try {
      const body = pick.rec.kind === "file"
        ? { source: "hf", repo: pick.model.id, filename: pick.file!.filename, kind: "file" }
        : { source: "hf", repo: pick.model.id, kind: "repo" };
      await apiPost("/api/hub/download", body);
      timer.current = window.setInterval(async () => {
        const j = await pollJobs(pick.model.id);
        if (j && (j.status === "done" || j.status === "error" || j.status === "cancelled")) {
          if (timer.current) window.clearInterval(timer.current);
          setBusy(false);
          if (j.status === "done") {
            const d = await apiJson<{ models: LocalModel[] }>("/api/hub/local");
            const m = d.models.find((x) => x.repo === pick.model.id && (!pick.file || x.filename === pick.file.filename));
            if (m) await activate(m.path); else setError("downloaded, but the model was not found on disk — open Models to activate it");
          } else setError(j.error || `download ${j.status}`);
        }
      }, 1500);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };

  const saveProvider = async () => {
    setBusy(true); setError(null);
    try {
      await apiJson(`/api/providers/${provId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ apiKey: provKey }) });
      onReady({ id: provId, model: provModel });
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const tab = (id: typeof mode, label: string) => (
    <button onClick={() => setMode(id)} className={`rounded-full border px-3 py-1 text-xs font-medium ${mode === id ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}>{label}</button>
  );

  return (
    <section className="rounded-2xl border border-line bg-canvas p-6">
      <h2 className="font-display text-lg font-semibold text-ink">Pick a model</h2>
      <p className="mt-1 text-sm text-body">
        Workers think with a model that runs on this machine by default. {status.platform.memGb ? `This machine has ${status.platform.memGb} GB of memory` : ""}
        {status.platform.appleSilicon ? " and Apple Silicon, so MLX models run fastest." : "."}
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        {tab("download", "Download a recommended model")}
        {tab("existing", status.modelActive ? "A model is already running" : "Use a model already on disk")}
        {tab("provider", "Use a provider key instead")}
      </div>

      {mode === "download" && (
        <div className="mt-4">
          {resolving && <p className="text-sm text-mute">Finding the best model for this machine…</p>}
          {!resolving && !pick && <p className="text-sm text-warn">No recommended model could be found right now (is the machine online?). You can use the Models page later, or add a provider key.</p>}
          {pick && (
            <div className="rounded-xl border border-primary-line bg-softer p-4">
              <p className="text-sm font-semibold text-ink">{pick.rec.label}</p>
              <p className="mt-0.5 font-mono text-xs text-mute">{pick.model.id}{pick.file ? ` · ${pick.file.filename}` : ""}{pick.file?.sizeBytes ? ` · ${fmtBytes(pick.file.sizeBytes)}` : ""}</p>
              <p className="mt-1 text-xs text-body">{pick.rec.why}</p>
              {job && job.status !== "done" && (
                <div className="mt-3">
                  <div className="h-2 w-full overflow-hidden rounded-full bg-soft"><div className="h-2 rounded-full bg-primary transition-all" style={{ width: `${job.percent ?? 0}%` }} /></div>
                  <p className="mt-1 text-xs text-mute">{job.status} · {fmtBytes(job.receivedBytes)} / {fmtBytes(job.totalBytes)}</p>
                </div>
              )}
              {activating && <p className="mt-3 text-xs text-body">Loading the model… this takes a minute the first time.</p>}
              <div className="mt-3 flex gap-2">
                <button onClick={download} disabled={busy || activating} className={btnPrimary}>{busy ? "Downloading…" : activating ? "Loading…" : "Download and activate"}</button>
                <button onClick={() => { setSkipped((s) => s.length >= status.recommendations.length - 1 ? [] : [...s, pick.rec.label]); setPick(null); }} disabled={busy || activating} className={btnGhost}>Try another</button>
              </div>
            </div>
          )}
          {note && <p className="mt-2 text-xs text-mute">{note}</p>}
        </div>
      )}

      {mode === "existing" && (
        <div className="mt-4 space-y-2">
          {status.modelActive && (
            <div className="flex items-center gap-3 rounded-xl border border-success-soft bg-success-soft p-3 text-sm text-success">
              A model is running. <button onClick={() => onReady(null)} className={`${btnPrimary} ml-auto`}>Continue</button>
            </div>
          )}
          {local.length === 0 && !status.modelActive && <p className="text-sm text-mute">No models on disk yet.</p>}
          {local.map((m) => (
            <div key={m.path} className="flex items-center gap-3 rounded-xl border border-line p-3 text-sm">
              <div className="min-w-0 flex-1"><p className="truncate font-mono text-xs text-ink">{m.repo}</p><p className="text-[11px] text-mute">{m.backend} · {m.filename} {fmtBytes(m.sizeBytes)}</p></div>
              <button onClick={() => activate(m.path)} disabled={activating} className={btnGhost}>{activating ? "Loading…" : "Activate"}</button>
            </div>
          ))}
        </div>
      )}

      {mode === "provider" && (
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Provider</label>
            <select className={inputCls} value={provId} onChange={(e) => { setProvId(e.target.value); setProvModel(providers.find((p) => p.id === e.target.value)?.models[0] ?? ""); }}>
              {providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Model</label>
            <select className={inputCls} value={provModel} onChange={(e) => setProvModel(e.target.value)}>
              {(providers.find((p) => p.id === provId)?.models ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="mb-1 block text-xs font-medium text-body">API key <span className="text-mute">(stored once, never shown again)</span></label>
            <input className={inputCls} type="password" autoComplete="off" value={provKey} onChange={(e) => setProvKey(e.target.value)} />
          </div>
          <p className="md:col-span-2 text-[11px] text-mute">Using a provider sends your prompts to that company. Local models never leave this machine.</p>
          <div className="md:col-span-2"><button onClick={saveProvider} disabled={busy || !provKey.trim()} className={btnPrimary}>Save and continue</button></div>
        </div>
      )}
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Step 2 — a worker
// --------------------------------------------------------------------------- //

const STARTERS = ["personal-assistant", "customer-support-rep", "research-analyst", "code-reviewer", "intern"];

function WorkerStep({ providerChoice, onDeployed }: { providerChoice: { id: string; model: string } | null; onDeployed: (id: string) => void }) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [tplId, setTplId] = useState("personal-assistant");
  const [ownerName, setOwnerName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiJson<{ templates: Template[] }>("/api/templates").then((d) => {
      const list = d.templates.filter((t) => STARTERS.includes(t.id));
      list.sort((a, b) => STARTERS.indexOf(a.id) - STARTERS.indexOf(b.id));
      setTemplates(list);
    }).catch((e) => setError(String(e)));
  }, []);

  const deploy = async () => {
    setBusy(true); setError(null);
    try {
      const tpl = templates.find((t) => t.id === tplId);
      const vars: Record<string, string> = {};
      for (const v of tpl?.variables ?? []) {
        if (v.key === "owner_name" && ownerName.trim()) vars[v.key] = ownerName.trim();
        else if (v.type === "folder" && workingDir.trim()) vars[v.key] = workingDir.trim();
        else if (v.default) vars[v.key] = v.default;
      }
      const agent = await apiPost<{ id: string }>(`/api/templates/${tplId}/deploy`, { variables: vars });
      if (providerChoice) {
        await apiJson(`/api/agents/${agent.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ modelSource: "byok", provider: providerChoice.id, model: providerChoice.model }) });
      }
      onDeployed(agent.id);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <section className="rounded-2xl border border-line bg-canvas p-6">
      <h2 className="font-display text-lg font-semibold text-ink">Hire your first worker</h2>
      <p className="mt-1 text-sm text-body">A template bundles a persona, tools, skills and guardrails. You can change everything later under Agents.</p>
      <div className="mt-4 grid gap-2 md:grid-cols-2">
        {templates.map((t) => (
          <button key={t.id} onClick={() => setTplId(t.id)}
            className={`rounded-xl border p-3 text-left transition-colors ${tplId === t.id ? "border-primary bg-primary-soft" : "border-pressed hover:bg-soft"}`}>
            <p className="text-sm font-semibold text-ink">{t.name}</p>
            <p className="mt-0.5 line-clamp-2 text-[11px] text-mute">{t.description}</p>
          </button>
        ))}
        {templates.length === 0 && !error && <p className="text-sm text-mute">Loading templates…</p>}
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Your name <span className="text-mute">(optional)</span></label>
          <input className={inputCls} value={ownerName} onChange={(e) => setOwnerName(e.target.value)} placeholder="so the worker knows who it works for" />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Working folder <span className="text-mute">(optional)</span></label>
          <input className={inputCls} value={workingDir} onChange={(e) => setWorkingDir(e.target.value)} placeholder="/Users/you/VirtualWorker — files the worker may read and write" />
        </div>
      </div>
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}
      <div className="mt-4"><button onClick={deploy} disabled={busy || !tplId} className={btnPrimary}>{busy ? "Deploying…" : "Deploy worker"}</button></div>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Step 3 — a chat app (optional)
// --------------------------------------------------------------------------- //

function ChannelStep({ agentId, onNext }: { agentId: string | null; onNext: () => void }) {
  const [kind, setKind] = useState<"telegram" | "discord" | "none">("telegram");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const connect = async () => {
    setBusy(true); setError(null);
    try {
      const acct = await apiPost<{ id: string; status: { state: string; error?: string } }>("/api/channels/accounts", {
        kind, name: kind === "telegram" ? "Telegram bot" : "Discord bot", enabled: true, defaultAgentId: agentId,
        config: { dmPolicy: "pairing" }, secrets: { botToken: token.trim() },
      });
      // give the adapter a moment to connect, then read status
      await new Promise((r) => setTimeout(r, 2500));
      const s = await apiJson<{ state: string; error?: string }>(`/api/channels/accounts/${acct.id}/status`);
      if (s.state === "error") throw new Error(s.error || "could not connect");
      setDone(`${kind === "telegram" ? "Telegram" : "Discord"} connected (${s.state}). Unknown senders will receive a pairing code you approve under Channels.`);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <section className="rounded-2xl border border-line bg-canvas p-6">
      <h2 className="font-display text-lg font-semibold text-ink">Message it from your phone <span className="text-sm font-normal text-mute">(optional)</span></h2>
      <p className="mt-1 text-sm text-body">Connect a chat app now, or later under Channels. WhatsApp pairs by QR code from the Channels page.</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {([["telegram", "Telegram"], ["discord", "Discord"], ["none", "Later"]] as const).map(([id, label]) => (
          <button key={id} onClick={() => setKind(id)} className={`rounded-full border px-3 py-1 text-xs font-medium ${kind === id ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft"}`}>{label}</button>
        ))}
      </div>
      {kind !== "none" && !done && (
        <div className="mt-4">
          <p className="text-xs text-body">
            {kind === "telegram"
              ? <>Message <b>@BotFather</b> in Telegram, send <code className="font-mono">/newbot</code>, and paste the token here.</>
              : <>In the Discord Developer Portal create an application, add a bot, enable the <b>Message Content</b> intent, invite it to your server, and paste the bot token.</>}
          </p>
          <div className="mt-2 flex gap-2">
            <input className={`${inputCls} flex-1`} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="bot token" />
            <button onClick={connect} disabled={busy || !token.trim()} className={btnPrimary}>{busy ? "Connecting…" : "Connect"}</button>
          </div>
        </div>
      )}
      {done && <p className="mt-3 rounded-xl bg-success-soft px-3 py-2 text-xs text-success">{done}</p>}
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}
      <div className="mt-5 flex gap-2">
        <button onClick={onNext} className={done || kind === "none" ? btnPrimary : btnGhost}>{done || kind === "none" ? "Continue" : "Skip for now"}</button>
      </div>
    </section>
  );
}
