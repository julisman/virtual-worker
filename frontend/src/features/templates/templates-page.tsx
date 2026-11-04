// Templates — a gallery of one-click deployable Virtual Employees (PRD-05).
// Browse built-in + imported templates, Deploy with variable substitution,
// Export an existing agent as a template, and Import a template JSON. Local-first.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

interface TemplateVariable {
  key: string;
  label: string;
  type: "text" | "folder" | "model" | "skill" | "secret" | "enum";
  default: string;
  required: boolean;
  help: string;
}

interface Template {
  id: string;
  name: string;
  description: string;
  category: string;
  role: string;
  persona: string;
  model: string | null;
  temperature: number;
  topP: number | null;
  topK: number | null;
  tools: string[];
  skills: string[];
  guardrails: { shell: string; verify: boolean; maxIterations: number; dailyCap: number };
  variables: TemplateVariable[];
  starterTasks: { title: string; description: string }[];
  source: "builtin" | "user" | "imported";
}

interface AgentLite { id: string; name: string; role: string }

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";

// A stable brand accent per template, keyed off its category.
const ACCENTS: Record<string, string> = {
  Support: "bg-info", Research: "bg-success", Engineering: "bg-primary",
  Productivity: "bg-warn", General: "bg-primary",
};
function accent(t: Template): string {
  return ACCENTS[t.category] ?? "bg-primary";
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function TemplatesPage({ onDeployed }: { onDeployed?: (agentId: string) => void }) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "builtin" | "mine">("all");
  const [query, setQuery] = useState("");
  const [deploy, setDeploy] = useState<Template | null>(null);
  const [inspect, setInspect] = useState<Template | null>(null);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);

  const load = useCallback(async () => {
    try { setTemplates((await apiJson<{ templates: Template[] }>("/api/templates")).templates); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return templates.filter((t) => {
      if (filter === "builtin" && t.source !== "builtin") return false;
      if (filter === "mine" && t.source === "builtin") return false;
      if (!q) return true;
      return (t.name + t.description + t.role + t.category).toLowerCase().includes(q);
    });
  }, [templates, filter, query]);

  const remove = async (t: Template) => {
    if (!confirm(`Remove template "${t.name}"?`)) return;
    try { await apiJson(`/api/templates/${encodeURIComponent(t.id)}`, { method: "DELETE" }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-base">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-8 py-7">
          {/* header */}
          <div className="flex flex-wrap items-end gap-4">
            <div className="min-w-0 flex-1">
              <h2 className="font-display text-[22px] font-bold tracking-tight text-ink">Templates</h2>
              <p className="mt-0.5 text-sm text-body">
                One-click Virtual Employees — deploy a ready-made agent, or capture your own to reuse and share.
              </p>
            </div>
            <div className="flex gap-2">
              <button onClick={() => setExporting(true)}
                className="rounded-xl border border-pressed px-4 py-2 text-sm font-semibold text-body transition-colors hover:border-ink hover:text-ink">
                Export agent…
              </button>
              <button onClick={() => setImporting(true)}
                className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">
                + Import
              </button>
            </div>
          </div>

          {/* filter + search */}
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
              {(["all", "builtin", "mine"] as const).map((f) => (
                <button key={f} onClick={() => setFilter(f)}
                  className={`rounded-lg px-3 py-1 text-xs font-semibold capitalize transition-colors ${
                    filter === f ? "bg-primary text-white shadow-[0_6px_14px_-8px_rgba(43,60,130,0.7)]" : "text-body hover:text-ink"
                  }`}>{f === "builtin" ? "Built-in" : f}</button>
              ))}
            </div>
            <input className={`${inputCls} max-w-xs flex-1`} value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search templates…" />
          </div>

          {error && <p className="mt-4 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

          {/* gallery */}
          <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {shown.length === 0 && (
              <p className="col-span-full py-10 text-center text-sm text-mute">No templates match.</p>
            )}
            {shown.map((t) => (
              <div key={t.id}
                className="flex flex-col rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
                <div className="flex items-start gap-3">
                  <div className={`font-display flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-lg font-bold text-white shadow-[0_8px_18px_-10px_rgba(43,60,130,0.6)] ${accent(t)}`}>
                    {(t.name || "?").trim().charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-[10px] font-semibold uppercase tracking-wide text-mute">{t.category}</p>
                    <h3 className="font-display truncate text-[15px] font-bold text-ink">{t.name}</h3>
                  </div>
                  {t.source === "builtin" ? (
                    <span className="shrink-0 rounded-full bg-primary-soft px-2 py-0.5 text-[10px] font-bold text-primary-badge">Built-in</span>
                  ) : (
                    <span className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[10px] font-semibold text-mute">Mine</span>
                  )}
                </div>
                <p className="mt-3 line-clamp-3 min-h-[3rem] text-xs text-body">{t.description}</p>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {t.tools.length > 0 && <Badge>{t.tools.length} tool{t.tools.length > 1 ? "s" : ""}</Badge>}
                  {t.skills.length > 0 && <Badge>{t.skills.length} skill{t.skills.length > 1 ? "s" : ""}</Badge>}
                  {t.starterTasks.length > 0 && <Badge>{t.starterTasks.length} task{t.starterTasks.length > 1 ? "s" : ""}</Badge>}
                  <Badge>shell: {t.guardrails.shell}</Badge>
                </div>
                <div className="mt-4 flex items-center gap-2 pt-1">
                  <button onClick={() => setDeploy(t)}
                    className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep active:scale-[.98]">
                    Deploy
                  </button>
                  <button onClick={() => setInspect(t)}
                    className="rounded-xl border border-pressed px-3 py-2 text-sm text-body hover:border-ink hover:text-ink">Inspect</button>
                  {t.source !== "builtin" && (
                    <button onClick={() => remove(t)} title="Remove"
                      className="ml-auto rounded-lg border border-pressed px-2.5 py-2 text-xs text-body hover:border-danger hover:text-danger">✕</button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {inspect && <InspectDialog template={inspect} onClose={() => setInspect(null)}
        onDeploy={() => { setDeploy(inspect); setInspect(null); }} />}
      {deploy && <DeployDialog template={deploy} onClose={() => setDeploy(null)}
        onDeployed={(id) => { setDeploy(null); onDeployed?.(id); }} />}
      {exporting && <ExportDialog onClose={() => setExporting(false)} onSaved={load} />}
      {importing && <ImportDialog onClose={() => setImporting(false)} onImported={load} />}
    </div>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{children}</span>;
}

// --------------------------------------------------------------------------- //
// Modal shell
// --------------------------------------------------------------------------- //

function Modal({ title, onClose, children, wide }: {
  title: string; onClose: () => void; children: React.ReactNode; wide?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-ink/30 p-6" onClick={onClose}>
      <div className={`max-h-[85vh] w-full ${wide ? "max-w-2xl" : "max-w-lg"} overflow-y-auto rounded-2xl border border-line bg-canvas p-6 shadow-[0_24px_60px_-30px_rgba(40,51,92,0.55)]`}
        onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center gap-2">
          <h3 className="font-display text-base font-bold text-ink">{title}</h3>
          <button onClick={onClose} className="ml-auto text-mute hover:text-ink">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function subst(text: string, ctx: Record<string, string>): string {
  return (text || "").replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (m, k) => (k in ctx && ctx[k] ? ctx[k] : m));
}

// --------------------------------------------------------------------------- //
// Inspect — read-only manifest before deploy
// --------------------------------------------------------------------------- //

function InspectDialog({ template, onClose, onDeploy }: {
  template: Template; onClose: () => void; onDeploy: () => void;
}) {
  return (
    <Modal title={template.name} onClose={onClose} wide>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-mute">
        {template.category} · {template.source === "builtin" ? "Built-in" : "Imported"}
      </p>
      <p className="mt-2 text-sm text-body">{template.description}</p>

      <Section label="Persona">
        <pre className="whitespace-pre-wrap rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink">{template.persona}</pre>
      </Section>

      <div className="mt-4 grid grid-cols-2 gap-4 text-xs">
        <Meta label="Role" value={template.role || "—"} />
        <Meta label="Model" value={template.model || "active model"} />
        <Meta label="Tools" value={template.tools.join(", ") || "none"} />
        <Meta label="Skills" value={template.skills.join(", ") || "none"} />
        <Meta label="Shell guardrail" value={template.guardrails.shell} />
        <Meta label="Temperature" value={String(template.temperature)} />
      </div>

      {template.starterTasks.length > 0 && (
        <Section label="Will create starter tasks">
          <ul className="list-disc pl-5 text-xs text-body">
            {template.starterTasks.map((t, i) => <li key={i}>{t.title}</li>)}
          </ul>
        </Section>
      )}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Close</button>
        <button onClick={onDeploy}
          className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">Deploy</button>
      </div>
    </Modal>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-4">
      <p className="mb-1.5 text-[11px] font-extrabold uppercase tracking-wide text-mute">{label}</p>
      {children}
    </div>
  );
}
function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-mute">{label}</p>
      <p className="mt-0.5 break-words text-body">{value}</p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Deploy — guided form from the variable schema + live persona preview
// --------------------------------------------------------------------------- //

function DeployDialog({ template, onClose, onDeployed }: {
  template: Template; onClose: () => void; onDeployed: (agentId: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(template.variables.map((v) => [v.key, v.default ?? ""])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (k: string, v: string) => setValues((s) => ({ ...s, [k]: v }));
  const preview = subst(template.persona, values);

  const go = async () => {
    for (const v of template.variables) {
      if (v.required && !(values[v.key] || "").trim()) { setError(`"${v.label}" is required.`); return; }
    }
    setBusy(true); setError(null);
    try {
      const agent = await apiPost<{ id: string }>(`/api/templates/${encodeURIComponent(template.id)}/deploy`, { variables: values });
      onDeployed(agent.id);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={`Deploy — ${template.name}`} onClose={onClose} wide>
      <p className="text-sm text-body">{template.description}</p>

      {template.variables.length === 0 ? (
        <p className="mt-4 text-xs text-mute">This template has no variables — deploy creates the agent as-is.</p>
      ) : (
        <div className="mt-4 space-y-3">
          {template.variables.map((v) => (
            <div key={v.key}>
              <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
                {v.label}{v.required && <span className="text-danger">*</span>}
                {v.help && <span className="text-mute" title={v.help}>ⓘ</span>}
              </label>
              <input className={inputCls} type={v.type === "secret" ? "password" : "text"}
                value={values[v.key] ?? ""} onChange={(e) => set(v.key, e.target.value)}
                placeholder={v.type === "folder" ? "/Users/you/projects/app" : v.default} />
            </div>
          ))}
        </div>
      )}

      <Section label="Persona preview">
        <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink">{preview}</pre>
      </Section>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
        <button onClick={go} disabled={busy}
          className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Deploying…" : "Deploy employee"}
        </button>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Export — capture an existing agent as a template (secrets scrubbed server-side)
// --------------------------------------------------------------------------- //

function ExportDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [includeStarters, setIncludeStarters] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const list = (await apiJson<{ agents: AgentLite[] }>("/api/agents")).agents;
        setAgents(list); setAgentId(list[0]?.id ?? "");
      } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    })();
  }, []);

  const fetchTemplate = useCallback(async () => {
    const q = new URLSearchParams();
    if (name.trim()) q.set("name", name.trim());
    if (includeStarters) q.set("includeStarters", "true");
    return apiJson<Template>(`/api/templates/from-agent/${encodeURIComponent(agentId)}?${q.toString()}`);
  }, [agentId, name, includeStarters]);

  const saveToGallery = async () => {
    if (!agentId) return;
    setBusy(true); setError(null);
    try {
      const tpl = await fetchTemplate();
      await apiPost("/api/templates/import", tpl);
      onSaved(); setDone("Saved to your gallery.");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const download = async () => {
    if (!agentId) return;
    setBusy(true); setError(null);
    try {
      const tpl = await fetchTemplate();
      const blob = new Blob([JSON.stringify(tpl, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${tpl.id || "template"}.json`; a.click();
      URL.revokeObjectURL(url);
      setDone("Downloaded template JSON.");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Modal title="Export an agent as a template" onClose={onClose}>
      <p className="text-xs text-mute">
        Captures the agent's persona, model, tools, skills, and guardrails. Memory, sessions, and any secrets are scrubbed automatically.
      </p>
      <div className="mt-4 space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Agent</label>
          <select className={inputCls} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.role ? ` — ${a.role}` : ""}</option>)}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Template name (optional)</label>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Defaults to the agent's name" />
        </div>
        <label className="flex items-center gap-2 text-xs text-body">
          <input type="checkbox" checked={includeStarters} onChange={(e) => setIncludeStarters(e.target.checked)} className="accent-primary" />
          Include the agent's open tasks as starter tasks
        </label>
      </div>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
      {done && <p className="mt-3 rounded-xl border border-primary-line bg-primary-soft px-3 py-2 text-xs text-primary-deep">{done}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={download} disabled={busy || !agentId}
          className="rounded-xl border border-pressed px-4 py-2 text-sm font-semibold text-body hover:border-ink hover:text-ink disabled:opacity-40">
          Download JSON
        </button>
        <button onClick={saveToGallery} disabled={busy || !agentId}
          className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Working…" : "Save to gallery"}
        </button>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Import — paste or upload a template JSON
// --------------------------------------------------------------------------- //

function ImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const onFile = async (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    setText(await f.text());
  };

  const go = async () => {
    let parsed: unknown;
    try { parsed = JSON.parse(text); }
    catch { setError("That isn't valid JSON."); return; }
    setBusy(true); setError(null);
    try {
      await apiPost("/api/templates/import", parsed);
      onImported(); onClose();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Modal title="Import a template" onClose={onClose} wide>
      <p className="text-xs text-mute">Paste a template JSON, or load a <span className="font-mono">.json</span> file. It's validated and secrets are stripped before it joins your gallery.</p>
      <div className="mt-3">
        <button onClick={() => fileRef.current?.click()}
          className="rounded-xl border border-pressed px-4 py-2 text-sm font-semibold text-body hover:border-ink hover:text-ink">Choose file…</button>
        <input ref={fileRef} type="file" accept=".json,application/json" hidden
          onChange={(e) => { onFile(e.target.files); e.target.value = ""; }} />
      </div>
      <textarea className={`${inputCls} mt-3 min-h-[200px] resize-y font-mono text-[11px] leading-relaxed`}
        value={text} onChange={(e) => setText(e.target.value)}
        placeholder='{ "name": "My Employee", "persona": "You are…", "variables": [] }' />

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
        <button onClick={go} disabled={busy || !text.trim()}
          className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Importing…" : "Import template"}
        </button>
      </div>
    </Modal>
  );
}

export default TemplatesPage;
