// Templates — a gallery of one-click deployable virtual workers (PRD-05).
// Browse built-in + imported templates, Deploy with variable substitution,
// Export an existing agent as a template, and Import a template JSON. Local-first.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { compareSemver } from "@/lib/semver";

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

// PRD-29 M2: a template's structural resources — present when schemaVersion===2.
// Loosely typed (key/name plus whatever else the manifest carries) since only
// the identity fields are used client-side.
export interface TemplateCustomToolRef { key: string; name: string; [k: string]: unknown }
export interface TemplateConnectorRef { name: string; preset: string; [k: string]: unknown }
// PRD-29 M4: the three new structural resource kinds a manifest may carry —
// never take credentials (secretKeys/retainedKeys are always empty for them).
export interface TemplateWikiRef { name: string; repoUrl: string; wikiRoot: string; pathVariable?: string }
export interface TemplateKnowledgeBaseRef { name: string; description?: string; embeddingModel?: string; sourceFolderVariable?: string }
export interface TemplateMcpServerRef { name: string; transport?: string; hint?: string; mcpToolAllow?: string[] }

export interface Template {
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
  source: "builtin" | "user" | "imported" | "registry";
  // PRD-29 M3: the registry slug this template was synced from — present only
  // when source==="registry" (registry templates are read-only; edits arrive
  // as version bumps on the next Sync, never a local delete/import).
  registry?: string;
  // PRD-29 M1: registry lineage — stable identity across re-imports (sourceId +
  // templateId), the manifest schema version, and the content digest + semver
  // this installed copy is pinned to (what an agent's templateRef compares against).
  sourceId: string;
  templateId: string;
  schemaVersion: number;
  digest: string;
  version: string;
  // PRD-29 M2: structural resources this template deploys (schemaVersion===2
  // only — absent/empty on older manifests).
  customTools?: TemplateCustomToolRef[];
  connectors?: TemplateConnectorRef[];
  // PRD-29 M4: same schemaVersion===2 manifest, three more resource kinds.
  wikis?: TemplateWikiRef[];
  knowledgeBases?: TemplateKnowledgeBaseRef[];
  mcpServers?: TemplateMcpServerRef[];
  // PRD-22 / PRD-31: optional behavior configs seeded onto the deployed agent
  // (instance-local parts — reviewer, exclusions, escalation targets — are
  // scrubbed on export and configured locally after deploy).
  cases?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
}

// --------------------------------------------------------------------------- //
// Needs-attention checklist (PRD-29 M2 §7.4) — shared between the post-deploy
// and post-update flows (templates-page.tsx and agents-page.tsx).
// --------------------------------------------------------------------------- //

export interface NeedsAttentionItem {
  kind: "connector-untested" | "binding-reentry" | "skill-unresolved" | "cli-tool-missing"
    | "kb-empty" | "mcp-server-unmatched" | "wiki-path-missing";
  id: string;
  message: string;
}

export interface SetupSummary {
  customTools: { created: number; adopted: number };
  connectors: { created: number; attached: number };
  skills: { installed: number; attached: number };
}

const NEEDS_ATTENTION_META: Record<NeedsAttentionItem["kind"], { icon: string; label: string }> = {
  "connector-untested": { icon: "🔌", label: "Connector untested" },
  "binding-reentry": { icon: "🔒", label: "Needs re-entry" },
  "skill-unresolved": { icon: "🧩", label: "Skill unresolved" },
  "cli-tool-missing": { icon: "🛠", label: "CLI tool missing" },
  "kb-empty": { icon: "📚", label: "Knowledge base empty" },
  "mcp-server-unmatched": { icon: "🔗", label: "MCP server not configured" },
  "wiki-path-missing": { icon: "📁", label: "Wiki path missing" },
};

function summarizeSetup(s: SetupSummary): string {
  const parts: string[] = [];
  const ct = s.customTools.created + s.customTools.adopted;
  if (ct > 0) parts.push(`${ct} custom tool${ct === 1 ? "" : "s"}`);
  const cn = s.connectors.created + s.connectors.attached;
  if (cn > 0) parts.push(`${cn} connector${cn === 1 ? "" : "s"}`);
  const sk = s.skills.installed + s.skills.attached;
  if (sk > 0) parts.push(`${sk} skill${sk === 1 ? "" : "s"}`);
  return parts.length > 0 ? `${parts.join(", ")} set up.` : "Nothing extra to set up.";
}

export function NeedsAttentionPanel({ needsAttention, setupSummary }: {
  needsAttention: NeedsAttentionItem[];
  setupSummary?: SetupSummary;
}) {
  return (
    <div className="space-y-3">
      {setupSummary && (
        <p className="text-xs text-body">
          <span className="font-semibold text-ink">Ready:</span> {summarizeSetup(setupSummary)}
        </p>
      )}
      {needsAttention.length > 0 ? (
        <div>
          <p className="mb-1.5 text-xs font-semibold text-warn">⚠ Needs attention ({needsAttention.length}):</p>
          <ul className="space-y-1.5">
            {needsAttention.map((item, i) => {
              const meta = NEEDS_ATTENTION_META[item.kind] ?? { icon: "⚠", label: item.kind };
              return (
                <li key={`${item.kind}-${item.id}-${i}`}
                  className="flex items-start gap-2 rounded-lg border border-warn-soft bg-warn-soft px-3 py-2 text-xs text-warn">
                  <span aria-hidden className="shrink-0">{meta.icon}</span>
                  <span><span className="font-medium">{meta.label}</span> — {item.message}</span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : (
        <p className="text-xs text-mute">Nothing needs attention.</p>
      )}
    </div>
  );
}

interface AgentLite {
  id: string;
  name: string;
  role: string;
  // PRD-29 M1.5: lineage back to the template this agent was deployed from —
  // used to pre-select "Update existing" in the export dialog, and (M3) to
  // compute the deployed/behind counts shown on registry + imported cards.
  templateRef?: { sourceId: string; templateId: string; version: string } | null;
}

// PRD-29 M3: a synced template registry (git URL or local folder) — the
// backend owns the clone/pull; this is read-only status + management.
export interface Registry {
  slug: string;
  url: string;
  kind: "url" | "local";
  branch: string | null;
  // Optional content subdirectory (templates/ + skills/ live under it). When
  // unset the backend auto-detects vw-registry/ and falls back to the root.
  subdir?: string | null;
  lastSyncedAt: string | null;
  lastError: string | null;
  templateCount: number;
  skillPackCount: number;
  path: string;
}

// PRD-29 M3: response shape from POST /api/templates/{id}/update-all.
interface UpdateAllResultItem {
  agentId: string;
  name: string;
  status: "updated" | "skipped" | "failed";
  code?: string;
  message?: string;
  reason?: string;
}
interface UpdateAllResponse {
  templateId: string;
  toVersion: string;
  results: UpdateAllResultItem[];
  stopped: boolean;
}

// PRD-29 deploy parity: POST /api/templates/{id}/deploy-preview — the same
// unified per-resource rows the update dry-run uses, plus which secret
// variables still need entry once reuse/adoption is accounted for.
interface DeployRow {
  type: "customTool" | "connector" | "wiki" | "knowledgeBase" | "mcpServer";
  name: string;
  resourceKey: string;
  status: "create" | "update" | "reuse" | "adopt";
  adopted: boolean;
  affectedAgents: string[];
  secretKeys: string[];
  retainedKeys: string[];
}
interface DeployPreview {
  resources: DeployRow[];
  missingSecrets: string[];
  retainedSecrets: string[];
  // Folder variables that only exist to create a wiki/KB — covered (not
  // asked) when the resource is reused instead.
  coveredVariables?: string[];
  error?: string;
}
// Adoption confirmations posted with deploy/deploy-preview (same shape the
// update modal sends with apply-template).
interface Adoption { type: DeployRow["type"]; resourceKey: string }

const DEPLOY_KIND_LABELS: Record<DeployRow["type"], string> = {
  customTool: "tool", connector: "connector", wiki: "wiki",
  knowledgeBase: "knowledge base", mcpServer: "mcp server",
};

// Template ids are slugs (folder + registry path + lineage half) — mirror the
// backend's _slugify so the id preview matches what the server would derive.
function slugify(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

// Bump the patch component for the suggested next version (1.0.3 → 1.0.4).
function nextPatch(version: string): string {
  const [core] = String(version || "0.0.0").split("-");
  const [maj = "0", min = "0", pat = "0"] = core.split(".");
  const n = parseInt(pat, 10);
  return `${parseInt(maj, 10) || 0}.${parseInt(min, 10) || 0}.${Number.isFinite(n) ? n + 1 : 1}`;
}

// Compact relative time for "last synced" — mirrors the `ago()` helper used
// elsewhere in the app (agents/work-tabs.tsx, agents/cases-tab.tsx).
function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

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

// A template's deployed lineage — every agent whose templateRef points at this
// template's (sourceId, templateId), split into up-to-date vs behind (PRD-29
// M3 §7.2 "N deployed · ⚠ M need update").
function templateLineage(t: Template, agents: AgentLite[]) {
  const deployed = agents.filter(
    (a) => a.templateRef && a.templateRef.sourceId === t.sourceId && a.templateRef.templateId === t.templateId);
  const behind = deployed.filter((a) => compareSemver(t.version, a.templateRef!.version) > 0);
  return { deployed, behind };
}

export function TemplatesPage({ onDeployed, onUpdated }: {
  onDeployed?: (agentId: string) => void;
  // PRD-29 M4: called after an in-page Update-all finishes, so the caller (the
  // sidebar's "update available" dot in App.tsx) can refresh — mirrors onDeployed.
  onUpdated?: () => void;
}) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "builtin" | "registry" | "mine">("all");
  const [query, setQuery] = useState("");
  const [deploy, setDeploy] = useState<Template | null>(null);
  const [inspect, setInspect] = useState<Template | null>(null);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [updating, setUpdating] = useState<Template | null>(null);
  const [rollingOut, setRollingOut] = useState<Template | null>(null);

  const load = useCallback(async () => {
    try { setTemplates((await apiJson<{ templates: Template[] }>("/api/templates")).templates); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  // GET-only — for the "N deployed · ⚠ M need update" badges (PRD-29 M3 §7.2).
  const loadAgents = useCallback(async () => {
    try { setAgents((await apiJson<{ agents: AgentLite[] }>("/api/agents")).agents); }
    catch { /* badges are best-effort */ }
  }, []);
  useEffect(() => { load(); loadAgents(); }, [load, loadAgents]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return templates.filter((t) => {
      if (filter === "builtin" && t.source !== "builtin") return false;
      if (filter === "registry" && t.source !== "registry") return false;
      if (filter === "mine" && t.source !== "user" && t.source !== "imported") return false;
      if (!q) return true;
      return (t.name + t.description + t.role + t.category).toLowerCase().includes(q);
    });
  }, [templates, filter, query]);

  const builtins = useMemo(() => shown.filter((t) => t.source === "builtin"), [shown]);
  const importedGroup = useMemo(() => shown.filter((t) => t.source === "user" || t.source === "imported"), [shown]);
  // One group per registry slug, sorted alphabetically for a stable order.
  const registryGroups = useMemo(() => {
    const bySlug = new Map<string, Template[]>();
    for (const t of shown) {
      if (t.source !== "registry") continue;
      const slug = t.registry || "unknown";
      if (!bySlug.has(slug)) bySlug.set(slug, []);
      bySlug.get(slug)!.push(t);
    }
    return [...bySlug.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([slug, items]) => ({ slug, items }));
  }, [shown]);

  const remove = async (t: Template) => {
    if (!confirm(`Remove template "${t.name}"?`)) return;
    try { await apiJson(`/api/templates/${encodeURIComponent(t.id)}`, { method: "DELETE" }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  // Download the stored template.json verbatim (PRD-29 M1.5, FR-15c) — the
  // publish artifact to commit to a registry (built-ins download lineage-free).
  const download = async (t: Template) => {
    try {
      const res = await fetch(`/api/templates/${encodeURIComponent(t.id)}/download`);
      if (!res.ok) throw new Error(`download failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${t.id}-v${t.version}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const cardProps = {
    agents, onDeploy: setDeploy, onInspect: setInspect, onDownload: download, onRemove: remove,
    onUpdateAll: setUpdating, onRollout: setRollingOut,
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
                One-click virtual workers — deploy a ready-made agent, or capture your own to reuse and share.
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

          {/* PRD-29 M3 §7.1 — shared template sources (git/local), sync on demand only */}
          <RegistriesPanel onChanged={load} />

          {/* filter + search */}
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
              {(["all", "builtin", "registry", "mine"] as const).map((f) => (
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

          {/* gallery — grouped Built-in / one section per registry / Imported (PRD-29 M3 §7.2) */}
          <div className="mt-6">
            {shown.length === 0 && (
              <p className="py-10 text-center text-sm text-mute">No templates match.</p>
            )}

            {builtins.length > 0 && (
              <GallerySection title="Built-in">
                {builtins.map((t) => <TemplateCard key={t.id} t={t} {...cardProps} />)}
              </GallerySection>
            )}

            {registryGroups.map(({ slug, items }) => (
              <div key={slug} className="mt-8 first:mt-0">
                <div className="mb-3 flex items-center gap-2">
                  <p className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Registry · {slug}</p>
                  <span className="rounded-full border border-pressed px-2 py-0.5 text-[10px] font-semibold text-mute">read-only</span>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {items.map((t) => <TemplateCard key={t.id} t={t} {...cardProps} />)}
                </div>
              </div>
            ))}

            {importedGroup.length > 0 && (
              <GallerySection title="Imported">
                {importedGroup.map((t) => <TemplateCard key={t.id} t={t} {...cardProps} />)}
              </GallerySection>
            )}
          </div>
        </div>
      </div>

      {inspect && <InspectDialog template={inspect} onClose={() => setInspect(null)}
        onDeploy={() => { setDeploy(inspect); setInspect(null); }} />}
      {deploy && <DeployDialog template={deploy} onClose={() => setDeploy(null)}
        onDeployed={(id) => { setDeploy(null); onDeployed?.(id); }} />}
      {exporting && <ExportDialog templates={templates} onClose={() => setExporting(false)} onSaved={load} />}
      {importing && <ImportDialog onClose={() => setImporting(false)} onImported={load} />}
      {updating && <UpdateAllDialog template={updating} onClose={() => setUpdating(null)}
        onDone={() => { loadAgents(); onUpdated?.(); }} />}
      {rollingOut && <RolloutDialog template={rollingOut} onClose={() => setRollingOut(null)} />}
    </div>
  );
}

// A gallery group header (BUILT-IN / IMPORTED) — the per-registry group below
// renders its own header inline since it also carries the "read-only" chip.
function GallerySection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-8 first:mt-0">
      <p className="mb-3 text-[11px] font-extrabold uppercase tracking-wide text-mute">{title}</p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </div>
  );
}

// One gallery card — shared by every group so built-in/registry/imported stay
// pixel-identical (PRD-29 M3 §7.2: "reuse the existing card markup").
function TemplateCard({ t, agents, onDeploy, onInspect, onDownload, onRemove, onUpdateAll, onRollout }: {
  t: Template;
  agents: AgentLite[];
  onDeploy: (t: Template) => void;
  onInspect: (t: Template) => void;
  onDownload: (t: Template) => void;
  onRemove: (t: Template) => void;
  onUpdateAll: (t: Template) => void;
  onRollout: (t: Template) => void;
}) {
  const { deployed, behind } = templateLineage(t, agents);
  return (
    <div className="flex flex-col rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
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
        ) : t.source === "registry" ? (
          <span className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[10px] font-semibold text-mute">Registry</span>
        ) : (
          <span className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[10px] font-semibold text-mute">Mine</span>
        )}
      </div>
      <p className="mt-3 line-clamp-3 min-h-[3rem] text-xs text-body">{t.description}</p>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {t.source !== "builtin" && t.version && <Badge>v{t.version}</Badge>}
        {t.source === "registry" && t.registry && <Badge>from {t.registry}</Badge>}
        {t.tools.length > 0 && <Badge>{t.tools.length} tool{t.tools.length > 1 ? "s" : ""}</Badge>}
        {(t.customTools?.length ?? 0) > 0 && <Badge>{t.customTools!.length} custom tool{t.customTools!.length > 1 ? "s" : ""}</Badge>}
        {(t.connectors?.length ?? 0) > 0 && <Badge>{t.connectors!.length} connector{t.connectors!.length > 1 ? "s" : ""}</Badge>}
        {(t.wikis?.length ?? 0) > 0 && <Badge>{t.wikis!.length} wiki{t.wikis!.length > 1 ? "s" : ""}</Badge>}
        {(t.knowledgeBases?.length ?? 0) > 0 && <Badge>{t.knowledgeBases!.length} KB{t.knowledgeBases!.length > 1 ? "s" : ""}</Badge>}
        {(t.mcpServers?.length ?? 0) > 0 && <Badge>{t.mcpServers!.length} MCP</Badge>}
        {t.skills.length > 0 && <Badge>{t.skills.length} skill{t.skills.length > 1 ? "s" : ""}</Badge>}
        {t.starterTasks.length > 0 && <Badge>{t.starterTasks.length} task{t.starterTasks.length > 1 ? "s" : ""}</Badge>}
        {t.cases && Object.keys(t.cases).length > 0 && <Badge>cases</Badge>}
        {t.metrics && Object.keys(t.metrics).length > 0 && <Badge>metrics</Badge>}
        <Badge>shell: {t.guardrails.shell}</Badge>
      </div>
      {deployed.length > 0 && (
        <p className="mt-2.5 text-[11px] text-mute">
          {deployed.length} deployed
          {behind.length > 0 && <span className="text-warn"> · ⚠ {behind.length} need update</span>}
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2 pt-1">
        <button onClick={() => onDeploy(t)}
          className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep active:scale-[.98]">
          Deploy
        </button>
        <button onClick={() => onInspect(t)}
          className="rounded-xl border border-pressed px-3 py-2 text-sm text-body hover:border-ink hover:text-ink">Inspect</button>
        {behind.length > 0 && (
          <button onClick={() => onUpdateAll(t)}
            className="rounded-xl border border-warn-soft bg-warn-soft px-3 py-2 text-sm font-semibold text-warn transition-colors hover:opacity-90">
            Update all
          </button>
        )}
        <OverflowMenu items={[
          { label: "Rollout…", onClick: () => onRollout(t) },
          { label: "Download JSON", onClick: () => onDownload(t) },
          ...(t.source !== "builtin" && t.source !== "registry"
            ? [{ label: "Delete", onClick: () => onRemove(t), danger: true }]
            : []),
        ]} />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Template Registries — shared, git/local-synced template sources (PRD-29 M3
// §7.1). Read-only: templates from a registry arrive as version bumps on the
// next Sync, never edited locally. FR-14: the list load below is a plain GET —
// syncing (a git pull) only ever happens from the explicit [Sync] click.
// --------------------------------------------------------------------------- //

function RegistriesPanel({ onChanged }: { onChanged: () => void }) {
  // Compact by default: a one-line bar with a summary. The registry list opens
  // on click; the add form only appears behind the [+ Add registry] button.
  const [open, setOpen] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [registries, setRegistries] = useState<Registry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [syncingSlug, setSyncingSlug] = useState<string | null>(null);
  const [addUrl, setAddUrl] = useState("");
  const [addBranch, setAddBranch] = useState("");
  const [addSubdir, setAddSubdir] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setRegistries((await apiJson<{ registries: Registry[] }>("/api/registries")).registries); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const sync = async (slug: string) => {
    setSyncingSlug(slug); setError(null);
    try { await apiPost<Registry>(`/api/registries/${encodeURIComponent(slug)}/sync`, {}); await load(); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSyncingSlug(null); }
  };

  const remove = async (r: Registry) => {
    if (!confirm(`Remove registry "${r.slug}"? Its templates leave your gallery.`)) return;
    setError(null);
    try { await apiJson(`/api/registries/${encodeURIComponent(r.slug)}`, { method: "DELETE" }); await load(); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const add = async () => {
    if (!addUrl.trim()) return;
    setAddBusy(true); setAddError(null);
    try {
      const body: { url: string; branch?: string; subdir?: string } = { url: addUrl.trim() };
      if (addBranch.trim()) body.branch = addBranch.trim();
      if (addSubdir.trim()) body.subdir = addSubdir.trim();
      await apiPost<Registry>("/api/registries", body);
      setAddUrl(""); setAddBranch(""); setAddSubdir("");
      setShowAdd(false); setOpen(true);
      await load(); onChanged();
    } catch (e) { setAddError(e instanceof Error ? e.message : String(e)); }
    finally { setAddBusy(false); }
  };

  const lastSynced = registries.reduce<string | null>(
    (latest, r) => (r.lastSyncedAt && (!latest || r.lastSyncedAt > latest) ? r.lastSyncedAt : latest), null);
  const summary = registries.length === 0
    ? "none connected"
    : `${registries.length} ${registries.length === 1 ? "registry" : "registries"} · ${
        registries.reduce((n, r) => n + r.templateCount, 0)} templates · synced ${ago(lastSynced)}`;

  return (
    <div className="mt-5 rounded-2xl border border-line bg-canvas px-5 py-3 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      <div className="flex items-center gap-3">
        <button onClick={() => registries.length > 0 && setOpen((o) => !o)} aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          disabled={registries.length === 0}>
          <h3 className="font-display shrink-0 text-sm font-bold text-ink">Template Registries</h3>
          <span className="truncate text-xs text-mute">{summary}</span>
          {registries.length > 0 && (
            <span aria-hidden className={`shrink-0 text-[10px] text-mute transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
          )}
        </button>
        <button onClick={() => { setShowAdd((s) => !s); if (!showAdd) setOpen(true); }}
          className="shrink-0 rounded-lg border border-pressed px-3 py-1.5 text-xs font-semibold text-body transition-colors hover:border-ink hover:text-ink">
          {showAdd ? "Cancel" : "+ Add registry"}
        </button>
      </div>

      {(open || showAdd) && (
        <div className="mt-3 space-y-3">
          {error && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

          {open && registries.length > 0 && (
            <div className="space-y-2">
              {registries.map((r) => (
                <div key={r.slug} className="rounded-xl border border-line bg-softer px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-display text-sm font-bold text-ink">{r.slug}</span>
                    {r.branch && <Badge>{r.branch}</Badge>}
                    {r.subdir && <Badge>/{r.subdir}</Badge>}
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-mute" title={r.url}>{r.url}</span>
                    <div className="ml-auto flex shrink-0 items-center gap-2">
                      <button onClick={() => sync(r.slug)} disabled={syncingSlug === r.slug}
                        className="rounded-lg border border-pressed px-3 py-1 text-xs font-semibold text-body transition-colors hover:border-ink hover:text-ink disabled:opacity-40">
                        {syncingSlug === r.slug ? "Syncing…" : "Sync"}
                      </button>
                      <button onClick={() => remove(r)} title="Remove registry" aria-label={`Remove ${r.slug}`}
                        className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-mute transition-colors hover:border-danger hover:text-danger">✕</button>
                    </div>
                  </div>
                  <p className="mt-1.5 text-[11px] text-mute">
                    {r.templateCount} template{r.templateCount === 1 ? "" : "s"} · {r.skillPackCount} skill pack{r.skillPackCount === 1 ? "" : "s"} · synced {ago(r.lastSyncedAt)}
                  </p>
                  {r.lastError && (
                    <p className="mt-1.5 rounded-lg border border-danger-soft bg-danger-soft px-2 py-1 font-mono text-[11px] text-danger">{r.lastError}</p>
                  )}
                </div>
              ))}
            </div>
          )}

          {showAdd && (
          <div className="rounded-xl border border-dashed border-pressed p-3">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-mute">Add registry</p>
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-[14rem] flex-1">
                <label className="mb-1 block text-[11px] font-medium text-body">Git URL or local folder path</label>
                <input className={inputCls} value={addUrl} onChange={(e) => setAddUrl(e.target.value)}
                  placeholder="https://github.com/org/templates.git or /path/to/folder" />
              </div>
              <div className="w-32">
                <label className="mb-1 block text-[11px] font-medium text-body">Branch</label>
                <input className={inputCls} value={addBranch} onChange={(e) => setAddBranch(e.target.value)} placeholder="main" />
              </div>
              <div className="w-44">
                <label className="mb-1 block text-[11px] font-medium text-body">Subdirectory <span className="font-normal text-mute">(optional)</span></label>
                <input className={inputCls} value={addSubdir} onChange={(e) => setAddSubdir(e.target.value)} placeholder="vw-registry" />
              </div>
              <button onClick={add} disabled={addBusy || !addUrl.trim()}
                className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
                {addBusy ? "Adding…" : "Add"}
              </button>
            </div>
            <p className="mt-2 text-[11px] text-mute">Cloned with your own git credentials. Virtual Worker stores nothing.</p>
            {addError && <p className="mt-2 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{addError}</p>}
          </div>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Update all — bulk-apply a newer template version to every deployed agent
// (PRD-29 M3 §7.2's [Update all]). Runs sequentially server-side; a duty-active
// agent stops the batch until the operator confirms.
// --------------------------------------------------------------------------- //

function UpdateAllDialog({ template, onClose, onDone }: {
  template: Template; onClose: () => void; onDone: () => void;
}) {
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UpdateAllResponse | null>(null);

  const run = useCallback(async (confirmDuty: boolean) => {
    setBusy(true); setError(null);
    try {
      const res = await apiPost<UpdateAllResponse>(
        `/api/templates/${encodeURIComponent(template.id)}/update-all`, { confirmDuty });
      setResult(res);
      onDone();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }, [template.id, onDone]);

  useEffect(() => { run(false); }, [run]);

  const needsDutyConfirm = !!result?.stopped
    && result.results.some((r) => r.status === "failed" && r.code === "confirm_duty_required");

  const statusCls = (s: UpdateAllResultItem["status"]) =>
    s === "updated" ? "text-success" : s === "skipped" ? "text-mute" : "text-danger";

  return (
    <Modal title={`Update all — ${template.name}`} onClose={onClose}>
      <p className="text-xs text-mute">Updates every agent deployed from this template to v{template.version}.</p>

      {busy && <p className="mt-4 text-sm text-body">Updating…</p>}

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      {result && (
        <div className="mt-4 space-y-1.5">
          {result.results.length === 0 && <p className="text-xs text-mute">No agents are deployed from this template.</p>}
          {result.results.map((r) => (
            <div key={r.agentId} className="flex items-start justify-between gap-2 rounded-lg border border-line bg-softer px-3 py-2 text-xs">
              <div className="min-w-0">
                <p className="truncate font-medium text-ink">{r.name}</p>
                {r.message && <p className="mt-0.5 text-[11px] text-mute">{r.message}</p>}
                {!r.message && r.reason && <p className="mt-0.5 text-[11px] text-mute">{r.reason.replace(/_/g, " ")}</p>}
              </div>
              <span className={`shrink-0 font-semibold capitalize ${statusCls(r.status)}`}>{r.status}</span>
            </div>
          ))}
          {result.stopped && (
            <p className="mt-2 text-[11px] text-warn">
              ⚠ Stopped early{needsDutyConfirm ? " — an agent is on duty now" : ""}.
            </p>
          )}
        </div>
      )}

      <div className="mt-6 flex justify-end gap-2">
        {needsDutyConfirm && (
          <button onClick={() => run(true)} disabled={busy}
            className="rounded-xl border border-warn-soft bg-warn-soft px-4 py-2 text-sm font-semibold text-warn transition-colors hover:opacity-90 disabled:opacity-40">
            Confirm duty and continue
          </button>
        )}
        <button onClick={onClose}
          className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">
          Done
        </button>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Rollout — read-only lockstep view: which agents deployed from this template
// are on which version/commit/digest (PRD-29 M4). GET-only, no mutations.
// --------------------------------------------------------------------------- //

interface RolloutAgentItem {
  agentId: string; name: string; version: string; commit: string; digest: string;
  status: "behind" | "up-to-date"; onDuty: boolean;
}
interface RolloutResponse {
  template: { templateId: string; sourceId: string; version: string; digest: string; name: string };
  agents: RolloutAgentItem[];
}

function short(s: string | null | undefined, n: number): string {
  return s ? s.slice(0, n) : "—";
}

function RolloutStatusPill({ status }: { status: RolloutAgentItem["status"] }) {
  return status === "up-to-date" ? (
    <span className="shrink-0 rounded-full border border-success-soft bg-success-soft px-2 py-0.5 text-[10px] font-semibold text-success">up to date</span>
  ) : (
    <span className="shrink-0 rounded-full border border-warn-soft bg-warn-soft px-2 py-0.5 text-[10px] font-semibold text-warn">behind</span>
  );
}

function RolloutDialog({ template, onClose }: { template: Template; onClose: () => void }) {
  const [data, setData] = useState<RolloutResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true); setError(null);
      try {
        const r = await apiJson<RolloutResponse>(`/api/templates/${encodeURIComponent(template.id)}/rollout`);
        if (!cancelled) setData(r);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [template.id]);

  return (
    <Modal title={`Rollout — ${template.name}`} onClose={onClose} wide>
      {loading && <p className="text-sm text-mute">Loading rollout…</p>}
      {error && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      {data && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-softer px-3 py-2">
            <span className="font-display text-sm font-bold text-ink">{data.template.name}</span>
            <Badge>v{data.template.version}</Badge>
            <span className="font-mono text-[11px] text-mute">digest {short(data.template.digest, 8)}</span>
          </div>

          {data.agents.length === 0 ? (
            <p className="text-xs text-mute">No agents are deployed from this template.</p>
          ) : (
            <div className="space-y-1.5">
              {data.agents.map((a) => (
                <div key={a.agentId}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-softer px-3 py-2 text-xs">
                  <div className="min-w-0">
                    <p className="truncate font-medium text-ink">
                      {a.name}{a.onDuty && <span className="ml-1.5 text-[10px] font-normal text-mute">on duty</span>}
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] text-mute">
                      v{a.version || "—"} · commit {short(a.commit, 7)} · digest {short(a.digest, 8)}
                    </p>
                  </div>
                  <RolloutStatusPill status={a.status} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-6 flex justify-end">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Close</button>
      </div>
    </Modal>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{children}</span>;
}

// Compact "⋯" overflow menu for the gallery card actions (PRD-29 §7.7) — keeps
// the row from overflowing at narrow card widths. Closes on outside click.
function OverflowMenu({ items }: {
  items: { label: string; onClick: () => void; danger?: boolean }[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div ref={ref} className="relative ml-auto">
      <button onClick={() => setOpen((v) => !v)} title="More actions"
        aria-haspopup="menu" aria-expanded={open}
        className="rounded-lg border border-pressed px-2.5 py-2 text-sm leading-none text-body hover:border-ink hover:text-ink">⋯</button>
      {open && (
        <div role="menu"
          className="absolute right-0 z-10 mt-1 min-w-[9rem] overflow-hidden rounded-xl border border-line bg-canvas py-1 shadow-[0_16px_40px_-20px_rgba(40,51,92,0.5)]">
          {items.map((it) => (
            <button key={it.label} role="menuitem"
              onClick={() => { setOpen(false); it.onClick(); }}
              className={`block w-full px-3 py-2 text-left text-sm transition-colors ${
                it.danger ? "text-danger hover:bg-danger-soft" : "text-body hover:bg-soft hover:text-ink"
              }`}>{it.label}</button>
          ))}
        </div>
      )}
    </div>
  );
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

// A custom tool draws on a credential when a header value references one of the
// template's secret-typed variables ({{key}} — the manifest's only legal secret
// spot); a connector does when it declares any secretBindings.
function toolUsesSecret(tool: TemplateCustomToolRef, secretKeys: Set<string>): boolean {
  const headers = tool.headers;
  if (!headers || typeof headers !== "object") return false;
  return Object.values(headers as Record<string, unknown>).some((v) =>
    typeof v === "string" &&
    [...v.matchAll(/\{\{\s*([\w.-]+)\s*\}\}/g)].some((m) => secretKeys.has(m[1])));
}

function InspectDialog({ template, onClose, onDeploy }: {
  template: Template; onClose: () => void; onDeploy: () => void;
}) {
  const customTools = template.customTools ?? [];
  const connectors = template.connectors ?? [];
  const wikis = template.wikis ?? [];
  const knowledgeBases = template.knowledgeBases ?? [];
  const mcpServers = template.mcpServers ?? [];
  const setsUp = customTools.length > 0 || connectors.length > 0
    || wikis.length > 0 || knowledgeBases.length > 0 || mcpServers.length > 0;
  const secretKeys = new Set(
    template.variables.filter((v) => v.type === "secret").map((v) => v.key));
  return (
    <Modal title={template.name} onClose={onClose} wide>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-mute">
        {template.category} · {template.source === "builtin" ? "Built-in"
          : template.source === "registry" ? `Registry · ${template.registry ?? ""}` : "Imported"}
      </p>
      {template.source !== "builtin" && (
        <p className="mt-1 font-mono text-[11px] text-mute">
          id: {template.templateId} · v{template.version}
          {template.sourceId ? ` · lineage: ${template.sourceId}` : ""}
        </p>
      )}
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
        {template.cases && Object.keys(template.cases).length > 0 && (
          <Meta label="Case management" value={template.cases.enabled ? "seeded · enabled" : "seeded · disabled"} />
        )}
        {template.metrics && Object.keys(template.metrics).length > 0 && (
          <Meta label="Efficiency metrics" value={template.metrics.enabled ? "seeded · enabled" : "seeded · disabled"} />
        )}
      </div>

      {setsUp && (
        <Section label="Sets up on deploy">
          <ul className="space-y-1 text-xs text-body">
            {customTools.map((t) => (
              <li key={`ct-${t.key}`} className="flex items-baseline gap-2">
                <span className="font-medium">{t.name || t.key}</span>
                <span className="font-mono text-[10px] text-mute">custom tool · {t.key}</span>
                {toolUsesSecret(t, secretKeys) && (
                  <span className="text-[10px] text-mute" title="Uses a secret variable — you'll enter the credential on deploy">🔒 needs credential</span>
                )}
              </li>
            ))}
            {connectors.map((c) => (
              <li key={`cn-${c.name}`} className="flex items-baseline gap-2">
                <span className="font-medium">{c.name}</span>
                <span className="font-mono text-[10px] text-mute">connector · {c.preset}</span>
                {Object.keys((c.secretBindings as Record<string, string> | undefined) ?? {}).length > 0 && (
                  <span className="text-[10px] text-mute" title="Uses a secret variable — you'll enter the credential on deploy">🔒 needs credential</span>
                )}
              </li>
            ))}
            {wikis.map((w) => (
              <li key={`wk-${w.name}`} className="flex items-baseline gap-2">
                <span className="font-medium">{w.name}</span>
                <span className="font-mono text-[10px] text-mute">wiki{typeof w.repoUrl === "string" && w.repoUrl ? ` · ${w.repoUrl}` : ""}</span>
              </li>
            ))}
            {knowledgeBases.map((k) => (
              <li key={`kb-${k.name}`} className="flex items-baseline gap-2">
                <span className="font-medium">{k.name}</span>
                <span className="font-mono text-[10px] text-mute">knowledge base{typeof k.embeddingModel === "string" && k.embeddingModel ? ` · ${k.embeddingModel}` : ""}</span>
                <span className="text-[10px] text-mute" title="Created as a recipe — content is ingested locally after deploy">local ingestion</span>
              </li>
            ))}
            {mcpServers.map((m) => (
              <li key={`mcp-${m.name}`} className="flex items-baseline gap-2">
                <span className="font-medium">{m.name}</span>
                <span className="font-mono text-[10px] text-mute">mcp server</span>
                <span className="text-[10px] text-mute" title="Matched by name against servers configured on this machine — never created by the template">requires local config</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

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

// PRD-29 M2: the deploy response is the created agent dict plus the
// post-deploy checklist inputs (§7.4) — only the fields this dialog reads.
interface DeployResult {
  id: string;
  needsAttention: NeedsAttentionItem[];
  setupSummary: SetupSummary;
}

// One resource row in the deploy dialog — mirrors the update modal's
// resource-first rows: reuse (pre-ticked adoption, credential kept) vs create.
function DeployResourceRow({ row, checked, onToggle }: {
  row: DeployRow; checked: boolean; onToggle: () => void;
}) {
  const kind = DEPLOY_KIND_LABELS[row.type];
  if (row.status === "adopt" || row.adopted) {
    const shared = row.affectedAgents.length > 0 ? row.affectedAgents.join(", ") : "";
    return (
      <div className={`rounded-lg border px-3 py-2 text-xs ${checked ? "border-pressed bg-canvas" : "border-warn-soft bg-warn-soft"}`}>
        <label className="flex cursor-pointer items-center gap-2.5">
          <input type="checkbox" className="accent-primary" checked={checked} onChange={onToggle} />
          <span className={`font-medium ${checked ? "text-ink" : "text-warn"}`}>{row.name}</span>
          <span className={checked ? "text-mute" : "font-medium text-warn"}>
            {checked
              ? (row.retainedKeys.length > 0 ? "✓ reuse yours — credential kept" : "✓ reuse yours")
              : "will create a copy instead"}
          </span>
          <span className="ml-auto shrink-0 font-mono text-[10px] text-mute">{kind}</span>
        </label>
        {shared && (
          <p className={`mt-1 pl-6 text-[11px] ${checked ? "text-mute" : "text-warn"}`}>shared with {shared}</p>
        )}
        {!checked && (
          <p className="mt-1 pl-6 text-[11px] text-warn">
            ⚠ Deploy will create a separate {kind} (credential required below)
            instead of reusing this one.
          </p>
        )}
      </div>
    );
  }
  const meta = {
    create: { label: "+ create new", box: "border-success-soft bg-success-soft", nameCls: "text-success", statusCls: "font-medium text-success" },
    update: { label: "~ update (template-owned)", box: "border-pressed bg-canvas", nameCls: "text-ink", statusCls: "text-mute" },
    reuse: {
      label: row.retainedKeys.length > 0 ? "✓ reuse yours — credential kept" : "✓ reuse yours",
      box: "border-pressed bg-canvas", nameCls: "text-ink", statusCls: "text-mute",
    },
  }[row.status];
  return (
    <div className={`rounded-lg border ${meta.box} px-3 py-2 text-xs`}>
      <div className="flex items-center gap-2.5">
        <span className={`font-medium ${meta.nameCls}`}>{row.name}</span>
        <span className={meta.statusCls}>{meta.label}</span>
        <span className="ml-auto shrink-0 font-mono text-[10px] text-mute">{kind}</span>
      </div>
    </div>
  );
}

function DeployDialog({ template, onClose, onDeployed }: {
  template: Template; onClose: () => void; onDeployed: (agentId: string) => void;
}) {
  // Secret answers are kept in a separate state bucket from the rest of the
  // variables — they're posted as `secrets`, never folded into `variables`.
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(template.variables.filter((v) => v.type !== "secret").map((v) => [v.key, v.default ?? ""])));
  const [secrets, setSecrets] = useState<Record<string, string>>(() =>
    Object.fromEntries(template.variables.filter((v) => v.type === "secret").map((v) => [v.key, v.default ?? ""])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deployed, setDeployed] = useState<DeployResult | null>(null);
  // Deploy parity with the update modal: preview which resources will be
  // reused (adoption pre-ticked, credential kept) vs created, and gate the
  // secret inputs on what's actually still needed.
  const [dryRun, setDryRun] = useState<DeployPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [adoptions, setAdoptions] = useState<Adoption[]>([]);
  const autoAdopted = useRef(false);

  const set = (k: string, v: string) => setValues((s) => ({ ...s, [k]: v }));
  const setSecret = (k: string, v: string) => setSecrets((s) => ({ ...s, [k]: v }));
  const preview = subst(template.persona, values);

  const runPreview = useCallback(async (ads: Adoption[]) => {
    setPreviewing(true);
    try {
      const r = await apiPost<DeployPreview>(
        `/api/templates/${encodeURIComponent(template.id)}/deploy-preview`,
        { variables: {}, secrets: {}, adoptions: ads });
      setDryRun(r);
    } catch { setDryRun(null); /* fall back to the static list */ }
    finally { setPreviewing(false); }
  }, [template.id]);
  useEffect(() => { runPreview([]); }, [runPreview]);

  // Adoption is the sensible default for exact-name matches (reuse what's
  // already on the box, keep its credential); auto-tick once per open.
  useEffect(() => {
    if (autoAdopted.current || !dryRun?.resources) return;
    autoAdopted.current = true;
    const need = dryRun.resources.filter((r) => r.status === "adopt");
    if (need.length > 0) {
      setAdoptions(need.map((r) => ({ type: r.type, resourceKey: r.resourceKey })));
    }
  }, [dryRun]);
  const firstPreview = useRef(true);
  useEffect(() => {
    if (firstPreview.current) { firstPreview.current = false; return; }
    runPreview(adoptions);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adoptions]);
  const isAdopted = (type: Adoption["type"], resourceKey: string) =>
    adoptions.some((a) => a.type === type && a.resourceKey === resourceKey);
  const toggleAdoption = (type: Adoption["type"], resourceKey: string) =>
    setAdoptions((prev) => (
      prev.some((a) => a.type === type && a.resourceKey === resourceKey)
        ? prev.filter((a) => !(a.type === type && a.resourceKey === resourceKey))
        : [...prev, { type, resourceKey }]
    ));

  // Which secret variables still need typing: with a preview, only the ones
  // reuse didn't cover; without one (endpoint unreachable), all of them.
  const neededSecrets = useMemo(() => {
    if (!dryRun || dryRun.error) return null;
    return new Set(dryRun.missingSecrets);
  }, [dryRun]);
  const secretNeeded = (key: string) => neededSecrets === null || neededSecrets.has(key);
  const coveredVars = useMemo(
    () => new Set(dryRun && !dryRun.error ? dryRun.coveredVariables ?? [] : []), [dryRun]);
  // A variable is asked unless reuse covers it: secrets via missingSecrets,
  // wiki/KB folder variables via coveredVariables.
  const variableAsked = (v: { key: string; type: string }) =>
    v.type === "secret" ? secretNeeded(v.key) : !coveredVars.has(v.key);

  const customToolCount = template.customTools?.length ?? 0;
  const connectorRefs = template.connectors ?? [];
  const wikiRefs = template.wikis ?? [];
  const kbRefs = template.knowledgeBases ?? [];
  const mcpRefs = template.mcpServers ?? [];
  const willSetUp = customToolCount > 0 || connectorRefs.length > 0
    || wikiRefs.length > 0 || kbRefs.length > 0 || mcpRefs.length > 0;

  const go = async () => {
    for (const v of template.variables) {
      if (!variableAsked(v)) continue;  // covered by reuse
      const val = v.type === "secret" ? secrets[v.key] : values[v.key];
      if (v.required && !(val || "").trim()) { setError(`"${v.label}" is required.`); return; }
    }
    setBusy(true); setError(null);
    try {
      const agent = await apiPost<DeployResult>(
        `/api/templates/${encodeURIComponent(template.id)}/deploy`,
        { variables: values, secrets, adoptions },
      );
      setDeployed(agent);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  if (deployed) {
    return (
      <Modal title={`Deploy — ${template.name}`} onClose={onClose} wide>
        <p className="text-sm text-body">“{template.name}” is deployed and ready.</p>
        <div className="mt-4">
          <NeedsAttentionPanel needsAttention={deployed.needsAttention} setupSummary={deployed.setupSummary} />
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Later</button>
          <button onClick={() => onDeployed(deployed.id)}
            className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">
            Open agent
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Deploy — ${template.name}`} onClose={onClose} wide>
      <p className="text-sm text-body">{template.description}</p>

      {template.variables.length === 0 ? (
        <p className="mt-4 text-xs text-mute">This template has no variables — deploy creates the agent as-is.</p>
      ) : (
        <div className="mt-4 space-y-3">
          {template.variables.filter(variableAsked).map((v) => (
            <div key={v.key}>
              <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
                {v.type === "secret" && <span aria-hidden>🔒</span>}
                {v.label}{v.required && <span className="text-danger">*</span>}
                {v.help && <span className="text-mute" title={v.help}>ⓘ</span>}
              </label>
              {v.type === "secret" ? (
                <>
                  <input className={inputCls} type="password" value={secrets[v.key] ?? ""}
                    onChange={(e) => setSecret(v.key, e.target.value)} placeholder={v.default} />
                  <p className="mt-1 text-[11px] text-mute">Stored locally, never exported.</p>
                </>
              ) : (
                <input className={inputCls} type="text"
                  value={values[v.key] ?? ""} onChange={(e) => set(v.key, e.target.value)}
                  placeholder={v.type === "folder" ? "/Users/you/projects/app" : v.default} />
              )}
            </div>
          ))}
        </div>
      )}

      {dryRun && !dryRun.error && dryRun.resources.length > 0 ? (
        <Section label={`Will set up (${
          dryRun.resources.filter((r) => r.status !== "create").length} reused · ${
          dryRun.resources.filter((r) => r.status === "create").length} new)`}>
          <div className={`space-y-1.5 ${previewing ? "opacity-60 transition-opacity" : ""}`}>
            {dryRun.resources.map((r) => (
              <DeployResourceRow key={`${r.type}-${r.resourceKey}`} row={r}
                checked={isAdopted(r.type, r.resourceKey)}
                onToggle={() => toggleAdoption(r.type, r.resourceKey)} />
            ))}
          </div>
        </Section>
      ) : willSetUp && (
        <Section label="Will set up">
          <ul className="space-y-1 text-xs text-body">
            {customToolCount > 0 && <li>{customToolCount} custom tool{customToolCount > 1 ? "s" : ""}</li>}
            {connectorRefs.map((c) => <li key={c.name}>connector {c.name} (new)</li>)}
            {wikiRefs.map((w) => <li key={w.name}>wiki {w.name}</li>)}
            {kbRefs.map((k) => <li key={k.name}>KB {k.name} (empty)</li>)}
            {mcpRefs.map((m) => <li key={m.name}>MCP {m.name}</li>)}
          </ul>
        </Section>
      )}

      <Section label="Persona preview">
        <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink">{preview}</pre>
      </Section>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
        <button onClick={go} disabled={busy || previewing}
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

function ExportDialog({ templates, onClose, onSaved }: {
  templates: Template[]; onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [agentId, setAgentId] = useState("");
  const [mode, setMode] = useState<"update" | "new">("new");
  const [targetId, setTargetId] = useState("");
  const [name, setName] = useState("");
  const [newId, setNewId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [version, setVersion] = useState("1.0.0");
  const [includeStarters, setIncludeStarters] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only your own (user/imported) templates are valid "update existing" targets —
  // built-ins are read-only and carry no lineage (FR-15c), and registry templates
  // are read-only too: their edits arrive as version bumps from the next Sync,
  // never a local overwrite (PRD-29 M3).
  const galleryTemplates = useMemo(
    () => templates.filter((t) => t.source === "user" || t.source === "imported"), [templates]);

  useEffect(() => {
    (async () => {
      try {
        const list = (await apiJson<{ agents: AgentLite[] }>("/api/agents")).agents;
        setAgents(list); setAgentId(list[0]?.id ?? "");
      } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    })();
  }, []);

  // Pre-select "Update existing" when the chosen agent was deployed from a
  // gallery template (matched by lineage), suggesting its next patch version.
  useEffect(() => {
    const agent = agents.find((a) => a.id === agentId);
    const ref = agent?.templateRef;
    const match = ref && galleryTemplates.find(
      (t) => t.sourceId === ref.sourceId && t.templateId === ref.templateId);
    if (match) {
      setMode("update"); setTargetId(match.id); setVersion(nextPatch(match.version));
    } else {
      setMode("new"); setTargetId(""); setVersion("1.0.0");
    }
  }, [agentId, agents, galleryTemplates]);

  const selectTarget = (id: string) => {
    setTargetId(id);
    const t = galleryTemplates.find((g) => g.id === id);
    if (t) setVersion(nextPatch(t.version));
  };

  const chooseMode = (next: "update" | "new") => {
    setMode(next);
    if (next === "new") { setVersion("1.0.0"); return; }
    const id = targetId || galleryTemplates[0]?.id || "";
    setTargetId(id);
    const t = galleryTemplates.find((g) => g.id === id);
    setVersion(t ? nextPatch(t.version) : "1.0.0");
  };

  // The chosen agent's name backs the id placeholder when the name field is empty.
  const agentName = agents.find((a) => a.id === agentId)?.name ?? "";
  const derivedId = slugify(name) || slugify(agentName);
  const effectiveId = idTouched && newId.trim() ? newId.trim() : derivedId;
  const idValid = mode !== "new" || !effectiveId || SLUG_RE.test(effectiveId);

  const canSave = !!agentId && !!version.trim() && idValid
    && (mode === "new" || (mode === "update" && !!targetId));

  const saveToGallery = async () => {
    if (!canSave) return;
    setBusy(true); setError(null);
    try {
      const q = new URLSearchParams();
      if (mode === "new" && name.trim()) q.set("name", name.trim());
      if (mode === "new" && effectiveId) q.set("templateId", effectiveId);
      if (mode === "update" && targetId) q.set("targetTemplateId", targetId);
      if (version.trim()) q.set("version", version.trim());
      if (includeStarters) q.set("includeStarters", "true");
      const tpl = await apiJson<Template>(
        `/api/templates/from-agent/${encodeURIComponent(agentId)}?${q.toString()}`);
      const result = await apiPost<ImportOutcome>("/api/templates/import", tpl);
      const label = result.template?.name || tpl.name || "template";
      const msg = result.outcome === "created"
        ? `Saved “${label}” v${result.version} to your gallery.`
        : result.outcome === "updated"
          ? `Updated “${label}”: v${result.previousVersion} → v${result.version}.`
          : `“${label}” already up to date (v${result.version}).`;
      toast.success(msg);
      onSaved(); onClose();
    } catch (e) {
      // 400s carry the verbatim reject reason (FR-2 matrix) — surface it exactly.
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast.error(`Save failed — ${msg}`);
    }
    finally { setBusy(false); }
  };

  return (
    <Modal title="Export an agent as a template" onClose={onClose}>
      <p className="text-xs text-mute">
        Captures the agent's persona, model, tools, skills, and guardrails. Memory, sessions, and any secrets are scrubbed automatically.
      </p>
      <div className="mt-4 space-y-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Agent</label>
          <select className={inputCls} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.role ? ` — ${a.role}` : ""}</option>)}
          </select>
        </div>

        <div>
          <p className="mb-1.5 text-xs font-medium text-body">Destination</p>
          <div className="space-y-2">
            <div>
              <label className="flex items-center gap-2 text-sm text-body">
                <input type="radio" name="exportDest" checked={mode === "update"}
                  disabled={galleryTemplates.length === 0}
                  onChange={() => chooseMode("update")} className="accent-primary" />
                <span className={galleryTemplates.length === 0 ? "text-mute" : ""}>Update existing template</span>
                {mode === "update" && (
                  <select className={`${inputCls} ml-1 max-w-[16rem]`} value={targetId}
                    onChange={(e) => selectTarget(e.target.value)}>
                    {galleryTemplates.map((t) => (
                      <option key={t.id} value={t.id}>{t.name} (v{t.version})</option>
                    ))}
                  </select>
                )}
              </label>
              {mode === "update" && targetId && (
                <p className="ml-6 mt-1 font-mono text-[11px] text-mute">
                  id: {galleryTemplates.find((t) => t.id === targetId)?.templateId ?? targetId} · locked (lineage)
                </p>
              )}
            </div>
            <div>
              <label className="flex items-center gap-2 text-sm text-body">
                <input type="radio" name="exportDest" checked={mode === "new"}
                  onChange={() => chooseMode("new")} className="accent-primary" />
                <span>Create new template</span>
              </label>
              {mode === "new" && (
                <div className="ml-6 mt-2 space-y-2">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-body">
                      Display name
                      <span className="ml-1.5 font-normal text-mute">(names deployed agents)</span>
                    </label>
                    <input className={`${inputCls} max-w-[18rem]`} value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder={agentName || "Defaults to the agent's name"} />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-body">
                      Id
                      <span className="ml-1.5 font-normal text-mute">(editable now, fixed after creation)</span>
                    </label>
                    <input className={`${inputCls} max-w-[18rem] font-mono text-xs ${idValid ? "" : "border-danger"}`}
                      value={idTouched ? newId : derivedId}
                      onChange={(e) => { setIdTouched(true); setNewId(e.target.value); }}
                      placeholder="derived from the name" />
                    {!idValid && (
                      <p className="mt-1 text-[11px] text-danger">
                        Lowercase letters, digits, and dashes only (e.g. support-on-duty).
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-body">
            Version
            <span className="ml-1.5 font-normal text-mute">
              {mode === "update" ? "(suggested: next patch — edit for a minor/major bump)" : "(new templates start at 1.0.0)"}
            </span>
          </label>
          <input className={`${inputCls} max-w-[10rem]`} value={version}
            onChange={(e) => setVersion(e.target.value)} placeholder="1.0.0" />
        </div>

        <label className="flex items-center gap-2 text-xs text-body">
          <input type="checkbox" checked={includeStarters} onChange={(e) => setIncludeStarters(e.target.checked)} className="accent-primary" />
          Include the agent's open tasks as starter tasks
        </label>
      </div>

      <p className="mt-4 rounded-xl border border-line bg-softer px-3 py-2 text-[11px] leading-relaxed text-mute">
        ⓘ Secrets are scrubbed. To publish: save here, verify the update on your own
        agent, then Download from the gallery card and commit it to the registry.
      </p>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
        <button onClick={saveToGallery} disabled={busy || !canSave}
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

interface ImportOutcome {
  outcome: "created" | "updated" | "unchanged";
  version: string;
  previousVersion: string | null;
  template: Template;
}

function ImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const toast = useToast();
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
      const result = await apiPost<ImportOutcome>("/api/templates/import", parsed);
      const name = result.template?.name || "template";
      const msg = result.outcome === "created"
        ? `Imported ${name} v${result.version}.`
        : result.outcome === "updated"
          ? `Updated in place: v${result.previousVersion} → v${result.version}.`
          : `Already up to date (v${result.version}).`;
      toast.success(msg);
      onImported(); onClose();
    } catch (e) {
      // 400s carry the verbatim reject reason (version/digest/schemaVersion
      // mismatch) as the thrown message — surface it exactly as returned.
      setError(e instanceof Error ? e.message : String(e));
    }
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
