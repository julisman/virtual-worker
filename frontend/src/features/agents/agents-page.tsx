// Agents — create local agents (persona + tools + skills) and manage skill packs.
// Local-first: everything is stored on this machine. Monochrome theme; plain React.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { compareSemver } from "@/lib/semver";
import { ActivityView, BoardView, ConversationsView, InboxView, OverviewView, ScheduleView, TriggersView } from "./work-tabs";
import type { NeedsAttentionItem, Template } from "@/features/templates/templates-page";
import { NeedsAttentionPanel } from "@/features/templates/templates-page";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

export type ModelSource = "local" | "byok" | "subscription";

// Where an agent's model runs. "local" (default) = the machine's active model;
// "byok"/"subscription" route to an external provider (see Providers).
export interface ProviderInfo {
  id: string;
  label: string;
  kind: string;
  baseUrl: string;
  models: string[];
  subscriptionModels: string[];
  byok: boolean;
  subscription: boolean;
  oauthLogin: boolean;  // subscription = ChatGPT-style OAuth sign-in, not a pasted token
  hasKey: boolean;
  hasToken: boolean;
  hasOauth: boolean;
  account: string;
  baseUrlOverride: string;
  custom: boolean;
  modelDiscovery: boolean;
  reasoningEffort: boolean;
  // Custom gateways only: per-model image-input opt-in ({model_id: true, "*": default}).
  vision?: Record<string, boolean>;
  credentialLabel: string;
  credentialHint: string;
  apiTypes: string[];
  apiType: "chat_completions" | "responses" | "";
  authModes: string[];
  authMode: "bearer" | "none" | "";
  // Custom-gateway per-model cache opt-in ("*" = default row) and telemetry
  // of cached tokens last observed for that model (see docs/prd/PRD-24-prompt-caching.md).
  caching?: Record<string, boolean>;
  cacheSeen?: Record<string, { cacheRead: number; cacheWrite: number }>;
  // Custom-gateway per-model reasoning dialect ("*" = default row) and the
  // per-model toggle to replay captured reasoning on tool-call continuations
  // ("*" = default row; see docs/prd/PRD-25-provider-thinking-compat.md).
  thinkingFormat?: Record<string, string>;
  replayReasoning?: Record<string, boolean>;
  // Whether a reasoning-encryption key is set for this provider (write-only on
  // save via `reasoningKey`; the value never comes back — see
  // docs/prd/PRD-27-reasoning-replay-encryption.md).
  hasReasoningKey?: boolean;
  // OpenCode Go multiplexes wire protocols behind one key; the backend
  // (_opencode_go_kind) is the single source of truth for each model's kind.
  modelKinds?: Record<string, "anthropic" | "openai" | "responses">;
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  persona: string;
  model: string | null;
  modelSource: ModelSource;
  provider: string;
  cacheTtl: string;
  temperature: number;
  topP: number | null;
  topK: number | null;
  repeatPenalty: number | null;
  reasoningEffort: string;
  // Per-agent override of the vision-helper agent used when this agent's model
  // can't see images ("" = use the global default set in Settings → Vision Helper).
  visionHelperAgentId: string;
  workingDir: string;
  role: string;
  dutyStatus: string;
  guardrails: Guardrails;
  webSearch: WebSearch;
  tools: string[];
  skills: string[];
  mcpServers: string[];
  // Per-agent, per-server MCP tool selection: { serverId: [toolName, ...] }.
  // A server absent from the map — or mapped to an empty list — = all its tools.
  mcpToolAllow: Record<string, string[]>;
  customTools: string[];
  connectors: string[];
  // Per-agent connector write/merge grant: { connectorName: {write, merge} }.
  // Absent/missing = off — an agent is read-only for a connector until granted.
  connectorGrants: Record<string, { write?: boolean; merge?: boolean }>;
  // Per-agent synchronous-consult (ask) cap; null = the global default (3).
  askBudget: number | null;
  knowledgeBases: string[];
  wikis: string[];
  updatedAt: string;
  // PRD-17 FR-10: computed by the backend — volatile content found in the
  // persona (dates/UUIDs/hashes) that would break prompt caching every run.
  personaCacheWarnings?: { kind: string; sample: string; hint: string }[];
  // PRD-23: read-only, backend-resolved window info for window-mode context
  // tracking. Absent/window:null = unresolved — treat defensively either way.
  contextWindowInfo?: ContextWindowInfo;
  // PRD-29 M1: lineage back to the template this agent was deployed from — the
  // pinned source/version/digest, resolved variables, and (after any update) a
  // one-step restore snapshot. null = never deployed from a template.
  templateRef: TemplateRef | null;
}

// PRD-29 M1 — an agent's pointer back to the template it was deployed from.
export interface TemplateRef {
  sourceId: string;
  templateId: string;
  version: string;
  commit: string;
  digest: string;
  variables: Record<string, string>;
  bindings: Record<string, unknown>;
  ledger: { type: string; id: string; resourceKey: string; adopted: boolean; provenance: unknown }[];
  baseline: Record<string, unknown>;
  snapshot: null | { managed: Record<string, unknown>; ref: unknown };
}

export interface ContextWindowInfo {
  window: number | null;
  source: "managed server" | "model config" | "catalog" | "probed" | "override" | "settings map" | null;
  effectiveReserve: number | null;
  compactsAt: number | null;
}

export type SearchProvider = "searxng" | "tavily" | "brave" | "duckduckgo";

export interface WebSearch {
  provider: SearchProvider;
  apiKey: string;
  searxngUrl: string;
}

const DEFAULT_WEB_SEARCH: WebSearch = { provider: "searxng", apiKey: "", searxngUrl: "" };

export interface Guardrails {
  shell: "auto" | "off" | "readonly" | "workspace";
  verify: boolean;
  maxIterations: number;
  wrapupWarnAt: number;
  lazyTools?: string;
  dailyCap: number;
  extraDenied: string[];
  allowedCommands: string[];
  tightContext: boolean;
  shellTimeoutS: number;
  contextChars: number;
  autoCompact: boolean;
  compactAt: number;
  mcpToolResultChars: number;
  wikiReadChars: number;
  skillFactory: boolean;
  retainLoadedSkills?: boolean;  // keep loaded-skill bodies in history instead of reloading every turn (chat channels)
  stepChecklist: boolean;
  compression?: "on" | "off";  // PRD-17: crush big tool results ("off" = legacy blind truncation)
  focusOutput?: boolean;  // action-first reply style (after ayghri/i-have-adhd)
  reasoning?: "on" | "off";  // "off" hides the model's <think> output and asks it to answer directly
  // PRD-23: token-accurate context tracking (opt-in, "chars" stays the default).
  contextMode?: "chars" | "window";
  reserveTokens?: number;  // window mode; 0 = auto (max output + safety)
  contextTokens?: number;  // window mode; 0 = auto-detect window override
  compactKeep?: "messages" | "size";  // how the recap's verbatim tail is cut, both modes
  compactKeepAmount?: number;  // messages or tokens per compactKeep; 0 = auto
  maxOutputTokens?: number;  // per-turn output budget (thinking + answer); 0 = auto
}

const DEFAULT_GUARDRAILS: Guardrails = {
  shell: "auto", verify: true, maxIterations: 6, wrapupWarnAt: 2, dailyCap: 60, extraDenied: [], allowedCommands: [],
  tightContext: false, shellTimeoutS: 60, contextChars: 0, autoCompact: false, compactAt: 80,
  mcpToolResultChars: 40000, wikiReadChars: 8000, skillFactory: false, retainLoadedSkills: false, stepChecklist: false,
  compression: "on", focusOutput: false, reasoning: "on",
  contextMode: "chars", reserveTokens: 0, contextTokens: 0, compactKeep: "messages", compactKeepAmount: 0,
  maxOutputTokens: 0,
};

interface Skill {
  slug: string;
  name: string;
  description: string;
  whenToUse: string;
  source?: "user" | "builtin";
}

interface BuiltinSkill extends Skill {
  category: string;
  requiresTools: string[];
  requiresSkills: string[];
  toolsAvailable: Record<string, boolean>;
  installed: boolean;
  updateAvailable?: boolean;
  installedVersion?: string;
}

interface McpServer {
  id: string;
  name: string;
  slug: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  allowedTools: string[];
  enabled: boolean;
}

// One tool advertised by an MCP server (POST /api/mcp/{id}/tools).
interface McpTool {
  name: string;
  description: string;
}

export interface CustomTool {
  id: string;
  name: string;
  description: string;
  transport: "http";
  parameters: { type: string; properties: Record<string, unknown>; required?: string[] };
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  url: string;
  headers: Record<string, string>;
  fixedParams: Record<string, string>;
  allowPrivate: boolean;
  timeout: number;
  enabled: boolean;
  updatedAt: string;
}

const BUILTIN_TOOLS: { name: string; label: string; hint: string }[] = [
  { name: "browser_search", label: "Web search", hint: "Search the web via a self-hosted SearXNG instance (no API key)." },
  { name: "browser_fetch", label: "Web fetch", hint: "Open a page in a browser and read its rendered text." },
];

// One picker chip = the whole interactive-browser toolset. Stored per-name on
// the agent (the backend registry is per-tool), so partial legacy sets still
// work — the chip completes the set on click.
const BROWSER_CONTROL_TOOLS = [
  "browser_open", "browser_snapshot", "browser_click", "browser_click_selector",
  "browser_type", "browser_press", "browser_back", "browser_read", "browser_evaluate",
  "browser_console_messages", "browser_network_requests",
  "browser_tabs", "browser_tab_select",
] as const;
const BROWSER_GROUP_HINT =
  "Drive a real, visible browser that stays logged in: open pages (in tabs), scan "
  + "clickable elements, click (by ref or CSS/XPath selector), type, press keys, go "
  + "back, switch tabs, read the page, run JavaScript for custom controls, and "
  + "inspect the page's console output and network requests (DevTools-style). Can "
  + "attach to your own Chrome via VW_BROWSER_CDP_URL. Enables all of: "
  + BROWSER_CONTROL_TOOLS.join(", ");

const DEFAULT_AGENT_ID = "assistant";
const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";

// Each agent gets a stable brand accent for its avatar tile + dot.
// Named agents map to fixed tokens; anything else hashes to one of the four.
const AGENT_ACCENTS = ["bg-primary", "bg-info", "bg-success", "bg-warn"] as const;
function agentAccent(name: string): string {
  const n = (name || "").trim().toLowerCase();
  if (!n) return "bg-primary";
  if (n.startsWith("assistant")) return "bg-primary";
  if (n.startsWith("research")) return "bg-info";
  if (n.startsWith("cod")) return "bg-success";
  if (n.startsWith("scrib")) return "bg-warn";
  let h = 0;
  for (let i = 0; i < n.length; i++) h = (h * 31 + n.charCodeAt(i)) >>> 0;
  return AGENT_ACCENTS[h % AGENT_ACCENTS.length];
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function AgentsPage({ onChat, onOpenSession, onTrace, sub = "agents", panel }: {
  onChat?: (agentId: string) => void;
  onOpenSession?: (sessionId: string) => void;
  onTrace?: (sessionId: string, runId?: string) => void;
  sub?: "agents" | "skills" | "mcp" | "tools";
  panel: Omit<AgentsPanelProps, "onChat" | "onOpenSession" | "onTrace">;   // roster state lifted to the shell
}) {
  // The roster + Skills/MCP are sidebar sub-items now, so the whole top bar is
  // free for content — the panel fills the full height of the pane.
  return (
    <div className="flex h-full min-w-0 flex-col bg-base">
      {sub === "agents" ? (
        <div className="min-h-0 flex-1"><AgentsPanel onChat={onChat} onOpenSession={onOpenSession} onTrace={onTrace} {...panel} /></div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sub === "skills" && <SkillsPanel />}
          {sub === "mcp" && <McpPanel />}
          {sub === "tools" && <ToolsPanel />}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Agents panel
// --------------------------------------------------------------------------- //

const blankAgent = (): Agent => ({
  id: "", name: "", description: "", persona: "", model: null,
  modelSource: "local", provider: "",
  cacheTtl: "",
  temperature: 0.6, topP: 0.95, topK: 64, repeatPenalty: null, reasoningEffort: "", visionHelperAgentId: "", workingDir: "", role: "", dutyStatus: "off",
  guardrails: { ...DEFAULT_GUARDRAILS }, webSearch: { ...DEFAULT_WEB_SEARCH },
  tools: [], skills: [], mcpServers: [], mcpToolAllow: {}, customTools: [], connectors: [], connectorGrants: {}, askBudget: null, knowledgeBases: [], wikis: [], updatedAt: "",
  templateRef: null,
});

interface KnowledgeBase { id: string; name: string; description: string; chunkCount: number; sourceCount: number }
interface WikiRef { id: string; name: string; description: string; pageCount: number }

// File tools an agent gets automatically once it has a working folder.
const FS_TOOLS = ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep"];

// The settings editor's sections — a side-nav item each, one on screen at a time
// (the old single-page editor scrolled through eight cards).
type SettingsSection = "identity" | "model" | "tools" | "knowledge" | "integrations" | "memory" | "guardrails";

const SETTINGS_SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "identity", label: "Identity" },
  { id: "model", label: "Model" },
  { id: "tools", label: "Tools & files" },
  { id: "knowledge", label: "Knowledge" },
  { id: "integrations", label: "Integrations" },
  { id: "memory", label: "Memory" },
  { id: "guardrails", label: "Guardrails" },
];

// Roster + selection are owned by the app shell (the sidebar lists the agents),
// so the panel is a controlled detail view: it edits whichever agent is selected.
export interface AgentsPanelProps {
  onChat?: (agentId: string) => void;
  onOpenSession?: (sessionId: string) => void;  // open a past conversation in Chat
  onTrace?: (sessionId: string, runId?: string) => void;  // open a step's trace in Debug
  agents: Agent[];
  selectedId: string | null;
  creating: boolean;
  onChanged: () => void;                       // reload the roster in the shell
  onSelectedChange: (id: string | null) => void;
  onCreatingChange: (v: boolean) => void;
}

function AgentsPanel({
  onChat, onOpenSession, onTrace, agents, selectedId, creating, onChanged, onSelectedChange, onCreatingChange,
}: AgentsPanelProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [customTools, setCustomTools] = useState<CustomTool[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [wikiRefs, setWikiRefs] = useState<WikiRef[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  // PRD-22 M-sync: global case-sync config, fetched once — feeds the per-agent
  // "Sync to" select. Fail-silent (leave enabled=false) if the route errors.
  // PRD-29 M1: the installed template gallery, fetched once — feeds the
  // provenance strip's "is there a newer version installed?" check.
  const [templates, setTemplates] = useState<Template[]>([]);
  const [applyOpen, setApplyOpen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [draft, setDraft] = useState<Agent>(blankAgent());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"overview" | "board" | "activity" | "schedule" | "triggers" | "conversations" | "inbox" | "settings">("settings");
  const [section, setSection] = useState<SettingsSection>("identity");
  const toast = useToast();

  const selected = useMemo(() => agents.find((a) => a.id === selectedId) ?? null, [agents, selectedId]);
  const isIntern = !creating && !!(selected?.role || "").trim();

  // Land on the work Overview for interns, the editor for plain agents.
  useEffect(() => {
    if (creating) setView("settings");
    else if (selected) setView((selected.role || "").trim() ? "overview" : "settings");
    setSection("identity");                                  // a fresh agent starts on its first section
  }, [creating, selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  const reloadProviders = useCallback(async () => {
    try {
      const p = await apiJson<{ providers: ProviderInfo[] }>("/api/providers");
      setProviders(p.providers);
    } catch { /* providers are optional — leave the list empty */ }
  }, []);

  // The editor's option lists (skills + MCP servers + providers) — loaded once.
  useEffect(() => {
    (async () => {
      try {
        const [s, m, t, k, w] = await Promise.all([
          apiJson<{ skills: Skill[] }>("/api/skills"),
          apiJson<{ servers: McpServer[] }>("/api/mcp"),
          apiJson<{ tools: CustomTool[] }>("/api/tools"),
          apiJson<{ knowledgeBases: KnowledgeBase[] }>("/api/kb"),
          apiJson<{ wikis: WikiRef[] }>("/api/wiki"),
        ]);
        setSkills(s.skills); setServers(m.servers); setCustomTools(t.tools); setKbs(k.knowledgeBases); setWikiRefs(w.wikis);
      } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
      reloadProviders();
      try {
        const tp = await apiJson<{ templates: Template[] }>("/api/templates");
        setTemplates(tp.templates);
      } catch { /* provenance strip just shows the ref without a match then */ }
    })();
  }, [reloadProviders]);

  // Mirror the selected agent (or a blank one when creating) into the editable draft.
  useEffect(() => {
    if (creating) { setDraft(blankAgent()); setError(null); }
    else if (selected) { setDraft({ ...selected }); setError(null); }
  }, [creating, selected]);

  const patch = (p: Partial<Agent>) => setDraft((d) => ({ ...d, ...p }));
  // External providers only use a subset of the sampling knobs: top_k is local-only
  // (we strip it for remote calls), and Claude rejects temperature + top_p together.
  const draftExternal = draft.modelSource === "byok" || draft.modelSource === "subscription";
  const draftAnthropic = draftExternal && draft.provider === "anthropic";
  const draftReasoningEffort = draftExternal && (draft.provider === "anthropic"
    || providers.find((p) => p.id === draft.provider)?.reasoningEffort);
  // PRD-23: window-mode context tracking — the mode select and the resolved
  // window info (read-only, from the last agent GET; absent/null = unresolved).
  const contextMode = draft.guardrails.contextMode ?? "chars";
  const contextWindowInfo = selected?.contextWindowInfo;
  const toggleIn = (key: "tools" | "skills" | "mcpServers" | "customTools" | "connectors" | "knowledgeBases" | "wikis", val: string) =>
    setDraft((d) => ({
      ...d,
      [key]: d[key].includes(val) ? d[key].filter((x) => x !== val) : [...d[key], val],
    }));

  const save = async () => {
    if (!draft.name.trim()) { setError("Give the agent a name."); return; }
    setBusy(true); setError(null);
    const external = draft.modelSource === "byok" || draft.modelSource === "subscription";
    const body = {
      name: draft.name.trim(), description: draft.description, persona: draft.persona,
      // model is the external model id when off "local"; null = use the active local model.
      model: external ? (draft.model || null) : null,
      modelSource: draft.modelSource, provider: external ? draft.provider : "",
      cacheTtl: draft.cacheTtl,
      temperature: draft.temperature, topP: draft.topP, topK: draft.topK,
      repeatPenalty: draft.repeatPenalty,
      reasoningEffort: external ? draft.reasoningEffort : "",
      visionHelperAgentId: draft.visionHelperAgentId,
      workingDir: draft.workingDir, role: draft.role, guardrails: draft.guardrails,
      webSearch: draft.webSearch,
      tools: draft.tools, skills: draft.skills, mcpServers: draft.mcpServers,
      mcpToolAllow: draft.mcpToolAllow,
      customTools: draft.customTools,
      connectors: draft.connectors,
      connectorGrants: draft.connectorGrants,
      askBudget: draft.askBudget,
      knowledgeBases: draft.knowledgeBases,
      wikis: draft.wikis,
    };
    try {
      const saved = creating
        ? await apiPost<Agent>("/api/agents", body)
        : await apiJson<Agent>(`/api/agents/${encodeURIComponent(draft.id)}`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
          });
      const wasCreating = creating;
      onCreatingChange(false);
      onSelectedChange(saved.id);
      onChanged();
      toast.success(wasCreating ? `Agent “${saved.name}” created` : "Changes saved");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast.error(`Save failed — ${msg}`);
    }
    finally { setBusy(false); }
  };

  const remove = async (a: Agent) => {
    if (a.id === DEFAULT_AGENT_ID) return;
    if (!confirm(`Delete agent "${a.name}"?`)) return;
    try {
      await apiJson(`/api/agents/${encodeURIComponent(a.id)}`, { method: "DELETE" });
      onSelectedChange(null);
      onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  // No agent selected and not creating — invite the user to make one.
  if (!creating && !selected) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center bg-base px-8">
        <div className="max-w-sm text-center">
          <div className="flex justify-center"><Monogram name="" /></div>
          <h2 className="font-display mt-4 text-xl font-bold tracking-tight text-ink">No agents yet</h2>
          <p className="mt-1 text-sm text-body">Create your first local agent — give it a persona, a working folder, and memory.</p>
          <button onClick={() => onCreatingChange(true)}
            className="mt-5 rounded-xl bg-primary px-5 py-2.5 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">+ New agent</button>
        </div>
      </div>
    );
  }

  const filesOn = draft.workingDir.trim().length > 0;
  const hasRole = draft.role.trim().length > 0;
  const browserOnCount = BROWSER_CONTROL_TOOLS.filter((t) => draft.tools.includes(t)).length;
  const browserControlOn = browserOnCount > 0;
  const toggleBrowserGroup = () => {
    setDraft((d) => {
      const others = d.tools.filter((t) => !(BROWSER_CONTROL_TOOLS as readonly string[]).includes(t));
      // full set on → turn all off; anything less → complete the set
      const full = BROWSER_CONTROL_TOOLS.every((t) => d.tools.includes(t));
      return { ...d, tools: full ? others : [...others, ...BROWSER_CONTROL_TOOLS] };
    });
  };

  // "N on" badges for the capability sections of the settings side-nav.
  const sectionCounts: Partial<Record<SettingsSection, number>> = {
    tools: draft.tools.length + (filesOn ? FS_TOOLS.length : 0),
    knowledge: draft.skills.length + draft.knowledgeBases.length + draft.wikis.length,
    integrations: draft.mcpServers.length + draft.customTools.length,
  };
  // On/off sections (no meaningful count) get an "on" chip instead.
  const sectionOn: Partial<Record<SettingsSection, boolean>> = {};

  return (
    <div className="flex h-full min-h-0 bg-base">
      {/* cockpit — the roster now lives in the sidebar, so this fills the pane */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-8 py-7">
          {/* identity band — live preview of the agent you're shaping */}
          <div className="flex items-center gap-4">
            <Monogram name={creating ? "" : draft.name} />
            <div className="min-w-0 flex-1">
              <h2 className="font-display truncate text-[22px] font-bold tracking-tight text-ink">
                {creating ? "New agent" : draft.name || "Untitled agent"}
              </h2>
              <p className="mt-0.5 truncate text-sm text-body">
                {draft.description || (creating ? "Give it a name, a personality, and a harness." : "No description yet.")}
              </p>
              {!creating && draft.id && (
                <TemplateProvenance
                  templateRef={draft.templateRef}
                  templates={templates}
                  onUpdate={() => setApplyOpen(true)}
                  onRestore={() => setRestoreOpen(true)}
                />
              )}
            </div>
            {!creating && draft.id && (
              <button onClick={() => onChat?.(draft.id)}
                className="shrink-0 rounded-xl bg-primary-soft px-4 py-2.5 text-sm font-semibold text-primary-deep transition-colors hover:bg-primary hover:text-white active:scale-[.98]">
                Chat with this agent →
              </button>
            )}
            {!creating && draft.id && draft.id !== DEFAULT_AGENT_ID && (
              <button onClick={() => remove(draft)}
                className="shrink-0 rounded-xl border border-pressed px-3 py-2.5 text-sm text-body hover:border-danger hover:text-danger">Delete</button>
            )}
          </div>

          {error && <p className="mt-5 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

          {/* work tabs — Overview/Board/Activity, plus Settings (the config editor) */}
          {!creating && selected && (
            <div className="mt-5 flex gap-1 border-b border-pressed">
              {([["overview", "Overview"], ["board", "Board"], ["activity", "Activity"], ["schedule", "Schedule"], ["triggers", "Triggers"], ["conversations", "Conversations"], ["inbox", "Inbox"], ["settings", "Settings"]] as const).map(([id, label]) => (
                <button key={id} onClick={() => setView(id)}
                  className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                    view === id ? "border-primary text-ink" : "border-transparent text-mute hover:text-ink"
                  }`}>
                  {label}
                  {id === "overview" && isIntern && (
                    <span className={`h-1.5 w-1.5 rounded-full ${draft.dutyStatus !== "off" ? "bg-success" : "bg-mute"}`} />
                  )}
                </button>
              ))}
            </div>
          )}

          {creating || view === "settings" ? (
          <>
          <div className="mt-6 flex items-start gap-6">
            {/* settings side-nav — one section on screen at a time, no mega-scroll */}
            <nav className="sticky top-0 w-44 shrink-0 space-y-0.5">
              {SETTINGS_SECTIONS.map((s) => {
                const count = sectionCounts[s.id];
                return (
                  <button key={s.id} onClick={() => setSection(s.id)}
                    className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium transition-colors ${
                      section === s.id ? "bg-primary-soft text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                    }`}>
                    <span className="min-w-0 flex-1 truncate">{s.label}</span>
                    {typeof count === "number" && count > 0 && (
                      <span className="shrink-0 rounded-full bg-canvas px-1.5 font-mono text-[10px] text-mute">{count}</span>
                    )}
                    {sectionOn[s.id] && (
                      <span className="shrink-0 rounded-full bg-success-soft px-1.5 font-mono text-[10px] text-success">on</span>
                    )}
                  </button>
                );
              })}
            </nav>

            <div className="min-w-0 flex-1 space-y-6">
              {section === "identity" && (
              <Card title="Identity">
                <div className="space-y-4">
                  <Field label="Name">
                    <input className={inputCls} value={draft.name} onChange={(e) => patch({ name: e.target.value })}
                      placeholder="e.g. Research Assistant" />
                  </Field>
                  <Field label="Description" hint="A short note shown in the roster.">
                    <input className={inputCls} value={draft.description} onChange={(e) => patch({ description: e.target.value })}
                      placeholder="What this agent is for" />
                  </Field>
                  <Field label="Job role" hint="Give the agent a job title to run it as an intern — it gets a task board, an activity feed, and a shell. Leave blank for a plain assistant.">
                    <input className={inputCls} value={draft.role} onChange={(e) => patch({ role: e.target.value })}
                      placeholder="e.g. Software Engineering Intern — blank = not an intern" />
                  </Field>
                  <Field label="Persona / system prompt" hint="Becomes the system prompt for this agent.">
                    <textarea className={`${inputCls} min-h-[140px] resize-y font-mono text-xs leading-relaxed`} value={draft.persona}
                      onChange={(e) => patch({ persona: e.target.value })}
                      placeholder="You are a helpful assistant that…" />
                    {(selected?.personaCacheWarnings?.length ?? 0) > 0 && (
                      <div className="mt-1.5 rounded-lg border border-primary-line bg-canvas px-3 py-2">
                        <p className="text-[11px] font-medium text-ink">⚠ This persona contains content that changes over time — it breaks prompt caching, so every chat turn re-reads the whole conversation (slower).</p>
                        <ul className="mt-1 space-y-0.5">
                          {selected!.personaCacheWarnings!.map((w, i) => (
                            <li key={i} className="text-[11px] text-mute">
                              <span className="font-mono text-ink">{w.sample}</span> ({w.kind}) — {w.hint}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </Field>
                  <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                    <div className="min-w-0 pr-3">
                      <label className="text-xs font-medium text-body">Focus output — action-first</label>
                      <p className="text-[11px] text-mute">Replies lead with the answer, number the steps, and end on one concrete next step — no preamble.</p>
                    </div>
                    <button onClick={() => patch({ guardrails: { ...draft.guardrails, focusOutput: !draft.guardrails.focusOutput } })}
                      className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        draft.guardrails.focusOutput ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                      }`}>{draft.guardrails.focusOutput ? "On" : "Off"}</button>
                  </div>
                  <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                    <div className="min-w-0 pr-3">
                      <label className="text-xs font-medium text-body">Reasoning</label>
                      <p className="text-[11px] text-mute">Off keeps the model's {"<think>"} reasoning out of replies — it is asked to answer directly, and any thinking it still emits is stripped.</p>
                    </div>
                    <button onClick={() => patch({ guardrails: { ...draft.guardrails, reasoning: draft.guardrails.reasoning === "off" ? "on" : "off" } })}
                      className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        draft.guardrails.reasoning !== "off" ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                      }`}>{draft.guardrails.reasoning !== "off" ? "On" : "Off"}</button>
                  </div>
                </div>
              </Card>
              )}

              {section === "model" && (<>
              <ModelCard draft={draft} patch={patch} providers={providers} reloadProviders={reloadProviders} />

              <Card title="Generation" hint="How the model samples tokens.">
                <div className="space-y-4">
                  <Field label={`Temperature — ${draft.temperature.toFixed(1)}`} hint="Higher = more creative. Gemma's default is 1.0.">
                    <input type="range" min={0} max={2} step={0.1} value={draft.temperature}
                      onChange={(e) => patch({ temperature: parseFloat(e.target.value) })} className="w-full accent-primary" />
                  </Field>
                  {draftReasoningEffort && (
                    <Field label="Reasoning effort" hint="Supported reasoning models use effort instead of sampling. When set, temperature/top-p are dropped for that request. Model default leaves the model's native thinking untouched; off disables thinking explicitly (cheaper).">
                      <select className={inputCls} value={draft.reasoningEffort}
                        onChange={(e) => patch({ reasoningEffort: e.target.value })}>
                        <option value="">Model default</option>
                        <option value="off">off — no thinking</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                        <option value="xhigh">xhigh</option>
                      </select>
                    </Field>
                  )}
                  <Field label="Max output tokens / turn" hint="One budget covers thinking + answer. Leave empty (or 0) for auto: 16k when a remote reasoning model runs at medium+ effort (its thinking needs room), 3072 otherwise. Raise it if replies get cut off; a too-small budget makes a reasoning model spend it all thinking.">
                    <input className={inputCls} type="number" min={0} max={131072} placeholder="auto"
                      value={draft.guardrails.maxOutputTokens || ""}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, maxOutputTokens: Math.max(0, Math.floor(Number(e.target.value)) || 0) } })} />
                  </Field>
                  <Field label="Vision helper (images)" hint="A text-only agent routes images to this helper to describe them. Global default is set in Settings → Vision Helper.">
                    <select className={inputCls} value={draft.visionHelperAgentId}
                      onChange={(e) => patch({ visionHelperAgentId: e.target.value })}>
                      <option value="">Global default</option>
                      {agents.filter((a) => a.id !== draft.id).map((a) => (
                        <option key={a.id} value={a.id}>{a.name}</option>
                      ))}
                    </select>
                  </Field>
                  <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                    <input type="checkbox" className="mt-0.5 accent-primary"
                      checked={!!draft.guardrails.tightContext}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, tightContext: e.target.checked } })} />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-ink">Tight context (faster)</span>
                      <span className="block text-[11px] text-mute">Caps tool outputs and trims history harder, so each turn re-reads less — noticeably faster on local models and MCP-heavy chats. Trade-off: less context carried per turn.</span>
                    </span>
                  </label>
                  <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                    <input type="checkbox" className="mt-0.5 accent-primary"
                      checked={draft.guardrails.compression !== "off"}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, compression: e.target.checked ? "on" : "off" } })} />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-ink">Compress large tool results (recommended)</span>
                      <span className="block text-[11px] text-mute">Big tool outputs are summarized structure-aware (totals, error rows and extremes always kept) instead of blindly cut off, and the agent can fetch the original on demand with retrieve_full. Off = legacy truncation: anything past the cap is simply lost.</span>
                    </span>
                  </label>
                  <Advanced label="Advanced sampling & limits">
                    <div className="grid grid-cols-2 gap-3">
                      <Field label="top_p" hint={draftAnthropic
                        ? "Nucleus sampling. Claude can't use temperature and top_p together — temperature takes precedence, so top_p is ignored unless you clear the temperature."
                        : "Nucleus sampling. Blank = model default. Gemma: 0.95."}>
                        <input className={inputCls} type="number" min={0} max={1} step={0.01}
                          value={draft.topP ?? ""} placeholder="0.95"
                          onChange={(e) => patch({ topP: e.target.value === "" ? null : Number(e.target.value) })} />
                      </Field>
                      {/* top_k is a local-engine knob; remote providers don't accept it, so hide it there. */}
                      {!draftExternal && (
                        <Field label="top_k" hint="Limit to the K most likely tokens. Blank = model default. Gemma: 64.">
                          <input className={inputCls} type="number" min={0} step={1}
                            value={draft.topK ?? ""} placeholder="64"
                            onChange={(e) => patch({ topK: e.target.value === "" ? null : Number(e.target.value) })} />
                        </Field>
                      )}
                    </div>
                    {/* repeat_penalty is local-only too: it breaks the phrase-cycle loops small
                        fine-tunes fall into; remote providers reject the param. */}
                    {!draftExternal && (
                      <div className="grid grid-cols-2 gap-3">
                        <Field label="repeat_penalty" hint="Local models only. Penalizes repeating tokens so the model can't lock into a phrase loop. Blank = default 1.1; 1 = off.">
                          <input className={inputCls} type="number" min={1} max={2} step={0.05}
                            value={draft.repeatPenalty ?? ""} placeholder="1.1"
                            onChange={(e) => patch({ repeatPenalty: e.target.value === "" ? null : Number(e.target.value) })} />
                        </Field>
                      </div>
                    )}
                    {draftExternal && (
                      <p className="text-[11px] text-mute">
                        {draftAnthropic
                          ? "Claude uses temperature only when both are set; top_k isn’t supported."
                          : "top_k isn’t supported by this provider — it’s ignored."}
                      </p>
                    )}
                    <div className="grid grid-cols-2 gap-3">
                      <Field label="MCP result chars" hint="Max characters of an MCP tool result (e.g. large PIS/Confluence JSON) fed back to the model. Raise it if responses get truncated before the field you need.">
                        <input className={inputCls} type="number" min={2000} max={500000} step={1000} value={draft.guardrails.mcpToolResultChars}
                          onChange={(e) => patch({ guardrails: { ...draft.guardrails, mcpToolResultChars: Number(e.target.value) || 40000 } })} />
                      </Field>
                      <Field label="Wiki read chars" hint="Max characters of a wiki_read result (a full wiki page) fed back to the model. Raise it for long pages that get clipped.">
                        <input className={inputCls} type="number" min={1000} max={200000} step={1000} value={draft.guardrails.wikiReadChars}
                          onChange={(e) => patch({ guardrails: { ...draft.guardrails, wikiReadChars: Number(e.target.value) || 8000 } })} />
                      </Field>
                    </div>
                    <Field label="Context tracking">
                      <select className={inputCls} value={draft.guardrails.contextMode ?? "chars"}
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, contextMode: e.target.value as "chars" | "window" } })}>
                        <option value="chars">Chars budget</option>
                        <option value="window">Model window (tokens)</option>
                      </select>
                      {contextMode === "window" && (
                        <p className="mt-1.5 text-[11px] text-mute">Uses the model's real context window and the token counts the server reports. Falls back to the chars budget if the window is unknown.</p>
                      )}
                    </Field>
                    {contextMode !== "window" && (
                      <div className="grid grid-cols-2 gap-3">
                        <Field label="Context budget (chars)" hint="Conversation char budget before older turns are compacted/elided. 0 = auto (sized to the model's provider: local 40k, remote 240k). To set it explicitly, size it to the model's context window: window tokens × 3 chars × 0.75 — e.g. a 128k-token window → ~290k, a 300k window → ~650k. The discount leaves room for the reply and for dense content (JSON/logs tokenize at fewer chars per token); overshooting the real window fails the request outright, so round down.">
                          <input className={inputCls} type="number" min={0} max={2000000} step={10000} value={draft.guardrails.contextChars}
                            onChange={(e) => patch({ guardrails: { ...draft.guardrails, contextChars: Number(e.target.value) || 0 } })} />
                        </Field>
                      </div>
                    )}
                    <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                      <input type="checkbox" className="mt-0.5 accent-primary"
                        checked={!!draft.guardrails.autoCompact}
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, autoCompact: e.target.checked } })} />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-ink">Auto-compact history</span>
                        <span className="block text-[11px] text-mute">
                          {contextMode === "window"
                            ? "When the conversation nears the window, fold the older turns into one LLM-written recap (evidence preserved as prose) instead of eliding them to stubs."
                            : "When the conversation nears the budget, fold the older turns into one LLM-written recap (evidence preserved as prose) instead of eliding them to stubs."}
                        </span>
                      </span>
                    </label>
                    {draft.guardrails.autoCompact && (
                      <div className="space-y-3 border-t border-pressed pt-3">
                        {contextMode !== "window" && (
                          <div className="grid grid-cols-2 gap-3">
                            <Field label="Compact at (%)" hint="Compact once the conversation passes this percentage of the context budget. Clamped to 30–95.">
                              <input className={inputCls} type="number" min={30} max={95} step={5} value={draft.guardrails.compactAt}
                                onChange={(e) => patch({ guardrails: { ...draft.guardrails, compactAt: Number(e.target.value) || 80 } })} />
                            </Field>
                          </div>
                        )}
                        <div className="grid grid-cols-2 gap-3">
                          <Field label="Keep verbatim" hint="What survives compaction unsummarized: the last N messages, or the last N tokens' worth of recent turns.">
                            <select className={inputCls} value={draft.guardrails.compactKeep ?? "messages"}
                              onChange={(e) => patch({ guardrails: { ...draft.guardrails, compactKeep: e.target.value as "messages" | "size" } })}>
                              <option value="messages">Last messages (count)</option>
                              <option value="size">Recent tokens (size)</option>
                            </select>
                          </Field>
                          <Field label="Amount" hint="What survives compaction unsummarized. 0 = auto (6 messages / ~10% of the window).">
                            <input className={inputCls} type="number" min={0} step={1}
                              value={draft.guardrails.compactKeepAmount ?? 0}
                              onChange={(e) => patch({ guardrails: { ...draft.guardrails, compactKeepAmount: Math.max(0, Math.floor(Number(e.target.value) || 0)) } })} />
                          </Field>
                        </div>
                      </div>
                    )}
                    {contextMode === "window" && (
                      <div className="space-y-3">
                        <Field label="Reserve tokens" hint="Kept free for the reply; compaction fires when the rest is full. 0 = auto (max output + safety).">
                          <input className={inputCls} type="number" min={0} step={1000}
                            value={draft.guardrails.reserveTokens ?? 0}
                            onChange={(e) => patch({ guardrails: { ...draft.guardrails, reserveTokens: Math.max(0, Math.floor(Number(e.target.value) || 0)) } })} />
                        </Field>
                        {contextWindowInfo?.window == null && (
                          <p className="rounded-lg border border-warn-soft bg-warn-soft px-3 py-2 text-[11px] font-medium text-warn">
                            Window unknown for this model — set it here, or the agent uses the chars budget.
                          </p>
                        )}
                        <Field label="Context window override" hint="Only needed when Virtual Worker can't detect the window, or detects it wrong (e.g. a brand-new remote model).">
                          <input className={inputCls} type="number" min={0} step={1000}
                            value={draft.guardrails.contextTokens ?? 0}
                            onChange={(e) => patch({ guardrails: { ...draft.guardrails, contextTokens: Math.max(0, Math.floor(Number(e.target.value) || 0)) } })} />
                        </Field>
                        {contextWindowInfo?.window != null && (
                          <p className="text-[11px] text-mute">
                            Window: {contextWindowInfo.window.toLocaleString()} tokens ({contextWindowInfo.source ?? "unknown"}) · compacts at ~{(contextWindowInfo.compactsAt ?? contextWindowInfo.window).toLocaleString()}
                          </p>
                        )}
                      </div>
                    )}
                  </Advanced>
                </div>
              </Card>
              </>)}

              {section === "tools" && (
              <Card title="Tools & files" badge={<CapBadge on={draft.tools.length > 0} onLabel={`${draft.tools.length} on`} offLabel="none" />}>
                <div className="space-y-4">
                  <SubSection label="Working folder" hint="A folder grants scoped file tools automatically — the agent reads/writes only inside it.">
                    <input className={inputCls} value={draft.workingDir}
                      onChange={(e) => patch({ workingDir: e.target.value })}
                      placeholder="/Users/you/projects/my-app" />
                    {filesOn && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {FS_TOOLS.map((t) => (
                          <span key={t} className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{t}</span>
                        ))}
                      </div>
                    )}
                  </SubSection>
                  <SubSection label="Built-in tools">
                    <div className="flex flex-wrap gap-2">
                      {BUILTIN_TOOLS.map((t) => {
                        const on = draft.tools.includes(t.name);
                        return (
                          <button key={t.name} title={t.hint} onClick={() => toggleIn("tools", t.name)}
                            className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                              on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                            }`}><span aria-hidden>{on ? "✓" : "+"}</span>{t.label}</button>
                        );
                      })}
                      <button title={BROWSER_GROUP_HINT} onClick={toggleBrowserGroup}
                        className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                          browserControlOn ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                        }`}>
                        <span aria-hidden>{browserOnCount === BROWSER_CONTROL_TOOLS.length ? "✓" : browserControlOn ? "◐" : "+"}</span>
                        Browser
                        {browserControlOn && browserOnCount < BROWSER_CONTROL_TOOLS.length && (
                          <span className="text-[10px] opacity-70">{browserOnCount}/{BROWSER_CONTROL_TOOLS.length} — click to enable all</span>
                        )}
                      </button>
                    </div>
                    {browserControlOn && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {BROWSER_CONTROL_TOOLS.filter((t) => draft.tools.includes(t)).map((t) => (
                          <span key={t} className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{t}</span>
                        ))}
                      </div>
                    )}
                  </SubSection>
                  {browserControlOn && (
                    <SubSection label="Browser control" hint="These tools drive one real, visible browser with a saved profile. Open it here to log into your sites once — the agent reuses that logged-in session.">
                      <div className="flex flex-wrap items-center gap-2">
                        <button type="button"
                          onClick={async () => {
                            try {
                              const r = await apiPost<{ ok: boolean; message: string }>("/api/browser/open", { url: "about:blank" });
                              r.ok ? toast.success("Browser opened — log in, then run your skill.") : toast.error(r.message || "Could not open the browser.");
                            } catch (e) {
                              toast.error(`Could not open the browser — ${e instanceof Error ? e.message : String(e)}`);
                            }
                          }}
                          className="rounded-full border border-primary-soft bg-primary-soft px-3 py-1.5 text-xs font-medium text-primary-deep hover:border-primary-line">
                          Open browser to log in
                        </button>
                        <span className="text-xs text-mute">Visible window with a persistent profile — sign in once and it sticks.</span>
                      </div>
                    </SubSection>
                  )}
                  {draft.tools.includes("browser_search") && (
                    <SubSection label="Web search" hint="Which backend the Web search tool uses. SearXNG is self-hosted and free; Tavily/Brave are hosted APIs needing a key; DuckDuckGo is free but best-effort.">
                      <div className="space-y-3">
                        <Field label="Provider">
                          <select className={inputCls} value={draft.webSearch.provider}
                            onChange={(e) => patch({ webSearch: { ...draft.webSearch, provider: e.target.value as SearchProvider } })}>
                            <option value="searxng">SearXNG (self-hosted, no key)</option>
                            <option value="tavily">Tavily (API key)</option>
                            <option value="brave">Brave Search (API key)</option>
                            <option value="duckduckgo">DuckDuckGo (no key, best-effort)</option>
                          </select>
                        </Field>
                        {(draft.webSearch.provider === "tavily" || draft.webSearch.provider === "brave") && (
                          <Field label="API key" hint={draft.webSearch.provider === "tavily"
                            ? "From tavily.com — stored locally on this machine."
                            : "From the Brave Search API dashboard — stored locally on this machine."}>
                            <input className={inputCls} type="password" value={draft.webSearch.apiKey} placeholder="paste key"
                              onChange={(e) => patch({ webSearch: { ...draft.webSearch, apiKey: e.target.value } })} />
                          </Field>
                        )}
                        {draft.webSearch.provider === "duckduckgo" && (
                          <p className="text-xs text-mute">No key needed. DuckDuckGo can rate-limit automated queries, so results may be intermittent.</p>
                        )}
                      </div>
                    </SubSection>
                  )}
                </div>
              </Card>
              )}

              {section === "knowledge" && (
              <Card title="Knowledge & skills">
                <div className="space-y-4">
                  <SubSection label="Skills" count={draft.skills.length} hint="Skill packs the agent can load on demand.">
                    <ChipPicker items={skills}
                      isOn={(s) => draft.skills.includes(s.slug)} onToggle={(s) => toggleIn("skills", s.slug)}
                      keyOf={(s) => s.slug} labelOf={(s) => s.name} titleOf={(s) => s.description}
                      searchText={(s) => `${s.name} ${s.slug} ${s.description}`}
                      selected={draft.skills} onRemoveMissing={(k) => toggleIn("skills", k)}
                      empty="No skills yet — upload some in the Skills tab." placeholder="Search skills…" />
                  </SubSection>
                  <SubSection label="Knowledge bases" count={draft.knowledgeBases.length} hint="Searched with the kb_search tool (RAG over your documents).">
                    <ChipPicker items={kbs}
                      isOn={(k) => draft.knowledgeBases.includes(k.id)} onToggle={(k) => toggleIn("knowledgeBases", k.id)}
                      keyOf={(k) => k.id} labelOf={(k) => k.name}
                      titleOf={(k) => `${k.sourceCount} source(s) · ${k.chunkCount} chunk(s)`}
                      searchText={(k) => `${k.name} ${k.description}`}
                      selected={draft.knowledgeBases} onRemoveMissing={(k) => toggleIn("knowledgeBases", k)}
                      empty="No knowledge bases yet — create one in the Knowledge tab." placeholder="Search knowledge…" />
                  </SubSection>
                  <SubSection label="Wikis" count={draft.wikis.length} hint="Maintained with the wiki_* tools — ingest sources into cross-linked OKF pages, then query them.">
                    <ChipPicker items={wikiRefs}
                      isOn={(w) => draft.wikis.includes(w.id)} onToggle={(w) => toggleIn("wikis", w.id)}
                      keyOf={(w) => w.id} labelOf={(w) => w.name} titleOf={(w) => `${w.pageCount} page(s)`}
                      searchText={(w) => `${w.name} ${w.description}`}
                      selected={draft.wikis} onRemoveMissing={(k) => toggleIn("wikis", k)}
                      empty="No wikis yet — create one in the Wiki tab." placeholder="Search wikis…" />
                  </SubSection>
                  <SubSection label="Skill authoring" hint="Lets this agent create and edit installed skills, which are shared across all agents. Off by default — not tied to a job role.">
                    <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                      <span className="text-xs font-medium text-body">Let this agent write its own skills</span>
                      <button onClick={() => patch({ guardrails: { ...draft.guardrails, skillFactory: !draft.guardrails.skillFactory } })}
                        className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                          draft.guardrails.skillFactory ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                        }`}>{draft.guardrails.skillFactory ? "On" : "Off"}</button>
                    </div>
                  </SubSection>
                </div>
              </Card>
              )}

              {section === "integrations" && (
              <Card title="Integrations">
                <div className="space-y-4">
                  <SubSection label="MCP servers" count={draft.mcpServers.length} hint="External tool providers (Model Context Protocol).">
                    <ChipPicker items={servers}
                      isOn={(s) => draft.mcpServers.includes(s.id)} onToggle={(s) => toggleIn("mcpServers", s.id)}
                      keyOf={(s) => s.id} labelOf={(s) => s.name}
                      titleOf={(s) => (s.transport === "http" ? s.url : `${s.command} ${s.args.join(" ")}`)}
                      searchText={(s) => `${s.name} ${s.transport === "http" ? s.url : `${s.command} ${s.args.join(" ")}`}`}
                      suffixOf={(s) => (!s.enabled ? " (off)" : "")}
                      selected={draft.mcpServers} onRemoveMissing={(k) => toggleIn("mcpServers", k)}
                      empty="No MCP servers yet — add one in the MCP tab." placeholder="Search MCP servers…" />
                    <McpToolPicker servers={servers} selectedIds={draft.mcpServers}
                      toolAllow={draft.mcpToolAllow}
                      onChange={(next) => patch({ mcpToolAllow: next })} />
                  </SubSection>
                  <SubSection label="Custom tools" count={draft.customTools.length} hint="HTTP tools you declared — the lightweight alternative to an MCP server.">
                    <ChipPicker items={customTools}
                      isOn={(t) => draft.customTools.includes(t.id)} onToggle={(t) => toggleIn("customTools", t.id)}
                      keyOf={(t) => t.id} labelOf={(t) => t.name} titleOf={(t) => t.description || `${t.method} ${t.url}`}
                      searchText={(t) => `${t.name} ${t.description} ${t.url}`}
                      suffixOf={(t) => (!t.enabled ? " (off)" : "")}
                      selected={draft.customTools} onRemoveMissing={(k) => toggleIn("customTools", k)}
                      empty="No custom tools yet — create one in the Tools tab." placeholder="Search tools…" />
                  </SubSection>
                </div>
              </Card>
              )}


              {section === "memory" && (
              <Card title="Memory" badge={<CapBadge always />}
                hint="Facts the agent keeps across sessions. The agent writes these itself with memory_write; add or prune them here.">
                {creating || !draft.id ? (
                  <p className="text-xs text-mute">Create the agent first — its memory appears here.</p>
                ) : (
                  <MemoryCard agentId={draft.id} />
                )}
              </Card>
              )}

              {/* autonomy & guardrails — only relevant once it has a job role */}
              {section === "guardrails" && (hasRole ? (
              <Card title="Autonomy & guardrails"
                hint="Automatic bounds for autonomous runs. The destructive/privileged command deny-list is always enforced and can't be turned off here. Tool output caps live under Generation → Advanced.">
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label="Shell">
                    <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
                      {(["auto", "readonly", "workspace", "off"] as const).map((m) => (
                        <button key={m} onClick={() => patch({ guardrails: { ...draft.guardrails, shell: m } })}
                          className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium capitalize transition-colors ${
                            draft.guardrails.shell === m ? "bg-primary text-white shadow-[0_6px_14px_-8px_rgba(43,60,130,0.7)]" : "text-body hover:text-ink"
                          }`}>{m === "readonly" ? "Read-only" : m}</button>
                      ))}
                    </div>
                  </Field>
                  <Field label="Daily task cap" hint="Task executions per day before it auto-pauses. Idle checks don't count.">
                    <input className={inputCls} type="number" min={1} max={1000} value={draft.guardrails.dailyCap}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, dailyCap: Number(e.target.value) || 1 } })} />
                  </Field>
                  <Field label="Consult (ask) budget" hint="How many times this agent may synchronously consult a teammate (ask_agent) in one run. Blank = the default (3).">
                    <input className={inputCls} type="number" min={0} max={20} placeholder="3 (default)"
                      value={draft.askBudget ?? ""}
                      onChange={(e) => patch({ askBudget: e.target.value === "" ? null : Math.max(0, Math.min(20, Math.floor(Number(e.target.value)) || 0)) })} />
                  </Field>
                  <Field label="Max steps / task" hint="Tool-loop iterations before the agent stops on one task. Leave empty (or 0) for unlimited — the agent works until the task is done; set a number only when you want a hard cap.">
                    <input className={inputCls} type="number" min={0} max={1000} placeholder="unlimited"
                      value={draft.guardrails.maxIterations || ""}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, maxIterations: Math.max(0, Math.floor(Number(e.target.value)) || 0) } })} />
                  </Field>
                  <Field label="Wrap-up warning" hint="How many steps before the cap the agent is told to finish up and answer with what it has. 0 = no warning (it still concludes at the cap). Ignored when steps are unlimited.">
                    <input className={inputCls} type="number" min={0} max={100}
                      value={draft.guardrails.wrapupWarnAt}
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, wrapupWarnAt: Math.max(0, Math.min(100, Math.floor(Number(e.target.value)) || 0)) } })} />
                  </Field>
                  <Field label="MCP tool exposure"
                    hint="Auto: full schemas on the local model, lazy discovery on paid models. Show all: always send every schema (bigger prompt). Lazy: always hide behind search_tools.">
                    <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
                      {([["auto", "Auto"], ["off", "Show all"], ["on", "Lazy"]] as const).map(([v, l]) => (
                        <button key={v} onClick={() => patch({ guardrails: { ...draft.guardrails, lazyTools: v } })}
                          className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium transition-colors ${
                            (draft.guardrails.lazyTools ?? "auto") === v ? "bg-primary text-white shadow-[0_6px_14px_-8px_rgba(43,60,130,0.7)]" : "text-body hover:text-ink"
                          }`}>{l}</button>
                      ))}
                    </div>
                  </Field>
                  <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                    <label className="text-xs font-medium text-body">Verify finished tasks</label>
                    <button onClick={() => patch({ guardrails: { ...draft.guardrails, verify: !draft.guardrails.verify } })}
                      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        draft.guardrails.verify ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                      }`}>{draft.guardrails.verify ? "On" : "Off"}</button>
                  </div>
                  <Field label="Step checklist" hint="For multi-step work the agent posts a step plan, shown live by chat surfaces that render it (progress card. Best on API-provider models; small local models may over-use it.">
                    <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                      <span className="text-xs font-medium text-body">Post a live step plan</span>
                      <button onClick={() => patch({ guardrails: { ...draft.guardrails, stepChecklist: !draft.guardrails.stepChecklist } })}
                        className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                          draft.guardrails.stepChecklist ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                        }`}>{draft.guardrails.stepChecklist ? "On" : "Off"}</button>
                    </div>
                  </Field>
                  <Field label="Retain loaded skills" hint="Keep skills the agent loads in its conversation history so it doesn't reload them every turn (chat channel). Best for tool-heavy agents; off keeps history lean for small-context local models.">
                    <div className="flex items-center justify-between rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                      <span className="text-xs font-medium text-body">Keep loaded-skill bodies in history</span>
                      <button onClick={() => patch({ guardrails: { ...draft.guardrails, retainLoadedSkills: !draft.guardrails.retainLoadedSkills } })}
                        className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                          draft.guardrails.retainLoadedSkills ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                        }`}>{draft.guardrails.retainLoadedSkills ? "On" : "Off"}</button>
                    </div>
                  </Field>
                </div>
                <div className="mt-4">
                  <Advanced label="Command allow / deny lists">
                    <Field label="Extra blocked commands" hint="Comma-separated. Added on top of the built-in deny-list (you can only tighten, never loosen it).">
                      <input className={inputCls} value={draft.guardrails.extraDenied.join(", ")}
                        placeholder="e.g. brew, docker"
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, extraDenied: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
                    </Field>
                    <Field label="Allowed commands" hint="Comma-separated operator allow-list. A program named here runs on the shell tool even if the deny-list or tier would refuse it (e.g. curl). An explicit opt-in — a command that's also in the blocked list stays blocked.">
                      <input className={inputCls} value={draft.guardrails.allowedCommands.join(", ")}
                        placeholder="e.g. curl, docker"
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, allowedCommands: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
                    </Field>
                    <Field label="Command timeout (s)" hint="Each shell command is killed after this many seconds (10–600). Raise it for agents whose legitimate commands are slow — e.g. log searches proxied through external CLI can take over 60s.">
                      <input className={inputCls} type="number" min={10} max={600} step={10} value={draft.guardrails.shellTimeoutS}
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, shellTimeoutS: Number(e.target.value) || 60 } })} />
                    </Field>
                  </Advanced>
                </div>
              </Card>
              ) : (
              <p className="rounded-xl border border-dashed border-pressed px-4 py-3 text-xs text-mute">
                <span className="font-semibold text-body">Autonomy &amp; guardrails</span> — give this agent a <span className="font-semibold text-body">job role</span> (in Identity) to run it as an intern with a task board, a shell, and safety guardrails.
              </p>
              ))}
            </div>
          </div>

          {/* commit bar */}
          <div className="sticky bottom-0 z-10 mt-7 flex items-center gap-2 border-t border-pressed bg-base/90 py-4 backdrop-blur">
            <button onClick={save} disabled={busy}
              className="rounded-xl bg-primary px-5 py-2.5 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep active:scale-[.98] disabled:opacity-40">
              {busy ? "Saving…" : creating ? "Create agent" : "Save changes"}
            </button>
            {creating && (
              <button onClick={() => onCreatingChange(false)}
                className="rounded-xl border border-pressed px-5 py-2.5 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
            )}
            <span className="ml-auto font-mono text-[10px] text-mute">Stored on this machine only</span>
          </div>
          </>
          ) : (
            <div className="mt-6">
              {view === "overview" && selected && (
                <OverviewView agentId={selected.id} onChanged={onChanged} />
              )}
              {view === "board" && selected && <BoardView agentId={selected.id} onTrace={onTrace} />}
              {view === "activity" && selected && <ActivityView agentId={selected.id} onTrace={onTrace} />}
              {view === "schedule" && selected && <ScheduleView agentId={selected.id} onTrace={onTrace} />}
              {view === "triggers" && selected && <TriggersView agentId={selected.id} />}
              {view === "conversations" && selected && (
                <ConversationsView agentId={selected.id} onOpenSession={onOpenSession} />
              )}
              {view === "inbox" && selected && <InboxView agentId={selected.id} />}
            </div>
          )}
        </div>
      </div>

      {applyOpen && draft.id && (
        <ApplyTemplateModal agentId={draft.id}
          onClose={() => setApplyOpen(false)}
          onApplied={() => { setApplyOpen(false); onChanged(); }} />
      )}
      {restoreOpen && draft.id && (
        <RestoreTemplateModal agentId={draft.id}
          onClose={() => setRestoreOpen(false)}
          onRestored={() => { setRestoreOpen(false); onChanged(); }} />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Template provenance strip + apply/restore modals (PRD-29 M1)
// --------------------------------------------------------------------------- //

// The compact line under an agent's name/description: no template, template
// gone missing, an update waiting, or up to date — plus the actions each state
// offers ([Update from template], [Restore]).
function TemplateProvenance({ templateRef, templates, onUpdate, onRestore }: {
  templateRef: TemplateRef | null;
  templates: Template[];
  onUpdate: () => void;
  onRestore: () => void;
}) {
  if (!templateRef) {
    return <p className="mt-1 text-[11px] text-mute">(no template)</p>;
  }
  const installed = templates.find(
    (t) => t.sourceId === templateRef.sourceId && t.templateId === templateRef.templateId,
  );
  const hasSnapshot = !!templateRef.snapshot;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2">
      {!installed ? (
        <span className="rounded-full border border-warn-soft bg-warn-soft px-2 py-0.5 text-[11px] font-medium text-warn">
          from {templateRef.templateId} (source not available)
        </span>
      ) : compareSemver(installed.version, templateRef.version) > 0 ? (
        <>
          <span className="rounded-full border border-warn-soft bg-warn-soft px-2 py-0.5 text-[11px] font-medium text-warn">
            Update available v{templateRef.version} → v{installed.version}
          </span>
          <button onClick={onUpdate}
            className="rounded-full bg-primary px-2.5 py-1 text-[11px] font-semibold text-white transition-colors hover:bg-primary-deep">
            Update from template
          </button>
        </>
      ) : (
        <span className="text-[11px] text-mute">from {installed.name} v{templateRef.version} · up to date</span>
      )}
      {hasSnapshot && (
        <button onClick={onRestore}
          className="rounded-full border border-pressed px-2.5 py-1 text-[11px] font-medium text-body transition-colors hover:border-ink hover:text-ink">
          Restore
        </button>
      )}
    </div>
  );
}

// Local modal shell — this codebase has no global Modal; templates-page.tsx
// keeps its own local copy and this mirrors it (same shape, same classes).
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

// A labeled block inside the apply/restore modals — mirrors templates-page.tsx's
// Section (not exported there, so kept as a small local twin here).
function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1.5 text-[11px] font-extrabold uppercase tracking-wide text-mute">{label}</p>
      {children}
    </div>
  );
}

interface ApplyPins { digest?: string; agentRevision: string; dutyStatus: string }
interface MissingVariable { key: string; reason: string; type: string }
interface ManagedDiff { old: unknown; new: unknown }

// PRD-29 M2: one resource the operator can choose to adopt instead of letting
// the update create a duplicate — collides with something owned elsewhere.
// PRD-29 M4: widened alongside ResourceRow below — wikis/knowledgeBases/
// mcpServers can also collide and need the same adopt-to-continue flow.
interface AdoptableRef { name: string; resourceKey: string; affectedAgents: string[] }
export interface Adoption { type: "customTool" | "connector" | "wiki" | "knowledgeBase" | "mcpServer"; resourceKey: string }

interface CustomToolsDiff {
  created: string[];
  updated: string[];
  adoptable: AdoptableRef[];
  retainedSecrets: string[];
  reentryRequired: string[];
}
interface ConnectorsDiff {
  created: string[];
  attached: string[];
  adoptable: AdoptableRef[];
  reentryRequired: string[];
  // Not in the documented contract, but read defensively in case a connector
  // preset ever retains a credential across an update the same way a custom
  // tool does — absent/empty is the expected common case.
  retainedSecrets?: string[];
}

// One connector/tool/wiki/knowledge base/MCP server the update touches —
// unified row shape from the dry-run: status + the secret-variable keys this
// specific resource needs or keeps. PRD-29 M4: wikis/knowledgeBases/mcpServers
// never carry secretKeys/retainedKeys (always empty from the backend).
interface ResourceRow {
  type: "customTool" | "connector" | "wiki" | "knowledgeBase" | "mcpServer";
  name: string;
  resourceKey: string;
  status: "create" | "update" | "reuse" | "adopt";
  adopted: boolean;               // reused via an adoption tick (untickable)
  affectedAgents: string[];
  secretKeys: string[];           // need entry (keyed into `secrets`)
  retainedKeys: string[];         // kept from the existing record
}

interface ApplyDryRun {
  hasTemplate: boolean;
  sourceAvailable: boolean;
  fromVersion?: string;
  toVersion?: string;
  pins?: ApplyPins;
  managed?: Record<string, ManagedDiff>;
  skills?: { added: string[]; removed: string[]; kept: string[] };
  localDivergence?: { field: string; current: unknown; oldTemplate: unknown }[];
  missingVariables?: MissingVariable[];
  hasSnapshot?: boolean;
  dutyActive?: boolean;
  customTools?: CustomToolsDiff;
  connectors?: ConnectorsDiff;
  resources?: ResourceRow[];
  needsAttention?: NeedsAttentionItem[];
}

interface RestoreDryRun {
  hasSnapshot: boolean;
  managed?: Record<string, ManagedDiff>;
  pins?: ApplyPins;
  dutyActive?: boolean;
  note?: string;
}

// One unowned collision the server refused to resolve on its own (409
// adoption_required) — the operator must check the matching adoption box and
// retry.
interface Collision {
  type: "customTool" | "connector" | "wiki" | "knowledgeBase" | "mcpServer";
  resourceKey: string; name?: string; affectedAgents?: string[];
}

interface ApiErrorDetail {
  code?: string;
  message?: string;
  missingVariables?: MissingVariable[];
  collisions?: Collision[];
}

// apply-template / restore-template's 409 body is a structured
// {code,message,...} object, not the string apiJson expects to throw — so
// these two POSTs read the status + parsed body directly instead of going
// through apiJson.
async function postTemplateAction<T = unknown>(path: string, body: unknown): Promise<
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; detail: ApiErrorDetail }
> {
  const res = await apiFetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON body */ }
  if (res.ok) return { ok: true, status: res.status, data: data as T };
  return { ok: false, status: res.status, detail: (data?.detail ?? {}) as ApiErrorDetail };
}

const MANAGED_FIELD_LABELS: Record<string, string> = {
  persona: "Persona", role: "Job role", description: "Description",
  temperature: "Temperature", topP: "top_p", topK: "top_k",
  tools: "Tools", skills: "Skills", guardrails: "Guardrails",
  customTools: "Custom tools", connectors: "Connectors", wikis: "Wikis",
  knowledgeBases: "Knowledge bases", mcpServers: "MCP servers",
  mcpToolAllow: "MCP tool allowlist",
  connectorGrants: "Connector grants", askBudget: "Consult budget",
};
function fieldLabel(field: string): string { return MANAGED_FIELD_LABELS[field] ?? field; }

// A non-array object never renders via String() — that prints "[object Object]".
function isPlainObject(v: unknown): boolean {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

function fmtScalar(v: unknown): string {
  if (v == null || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map((x) => (isPlainObject(x) ? JSON.stringify(x) : String(x))).join(", ") : "none";
  if (isPlainObject(v)) return JSON.stringify(v);
  return String(v);
}

// ---- Line diff for the upgrade/restore modals --------------------------- //
// A minimal LCS line diff so long fields (persona, guardrails JSON) show only
// what actually changed instead of two full copies. Unchanged runs collapse.

interface DiffLine { kind: "ctx" | "del" | "add"; text: string }

function diffTextLines(oldText: string, newText: string): DiffLine[] | null {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  // O(n·m) DP — personas/JSON are small; bail out (caller shows full panels)
  // on pathological sizes rather than freezing the modal.
  if (a.length * b.length > 250_000) return null;
  const n = a.length, m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: "ctx", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ kind: "del", text: a[i++] });
    else out.push({ kind: "add", text: b[j++] });
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

const DIFF_CONTEXT = 2;        // unchanged lines kept visible around a change
const DIFF_COLLAPSE_MIN = 5;   // shorter unchanged runs just stay visible

function DiffLineRow({ line }: { line: DiffLine }) {
  const cls = line.kind === "del"
    ? "bg-danger-soft text-danger"
    : line.kind === "add"
      ? "bg-success-soft text-success"
      : "text-panel-ink/80";
  const sign = line.kind === "del" ? "−" : line.kind === "add" ? "+" : " ";
  return (
    <div className={`whitespace-pre-wrap px-2 ${cls}`}>
      <span className="mr-1.5 inline-block w-2 select-none opacity-70">{sign}</span>
      {line.text || " "}
    </div>
  );
}

/** Unified diff with collapsed unchanged runs (expand via <details>). */
function UnifiedDiff({ oldText, newText }: { oldText: string; newText: string }) {
  const lines = useMemo(() => diffTextLines(oldText, newText), [oldText, newText]);
  if (!lines) {
    // Too large to diff — fall back to the two full panels.
    return (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {([["Current", oldText], ["Template", newText]] as const).map(([label, text]) => (
          <div key={label}>
            <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-mute">{label}</p>
            <pre className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded-lg bg-panel p-2 font-mono text-[10px] leading-relaxed text-panel-ink">{text}</pre>
          </div>
        ))}
      </div>
    );
  }
  // Split into changed blocks and unchanged runs; long unchanged runs keep
  // DIFF_CONTEXT lines on each side and collapse the middle.
  const segments: React.ReactNode[] = [];
  let run: DiffLine[] = [];
  let key = 0;
  const flushCtx = (isEdge: "start" | "middle" | "end") => {
    if (run.length === 0) return;
    const head = isEdge === "start" ? 0 : DIFF_CONTEXT;
    const tail = isEdge === "end" ? 0 : DIFF_CONTEXT;
    const hidden = run.length - head - tail;
    if (hidden < DIFF_COLLAPSE_MIN) {
      run.forEach((l) => segments.push(<DiffLineRow key={key++} line={l} />));
    } else {
      run.slice(0, head).forEach((l) => segments.push(<DiffLineRow key={key++} line={l} />));
      const mid = run.slice(head, run.length - tail);
      segments.push(
        <details key={key++} className="group/diff">
          <summary className="cursor-pointer list-none px-2 py-0.5 text-center text-[10px] text-mute hover:text-ink [&::-webkit-details-marker]:hidden">
            <span className="group-open/diff:hidden">⋯ {mid.length} unchanged lines</span>
            <span className="hidden group-open/diff:inline">− hide unchanged lines</span>
          </summary>
          {mid.map((l, idx) => <DiffLineRow key={idx} line={l} />)}
        </details>,
      );
      run.slice(run.length - tail).forEach((l) => segments.push(<DiffLineRow key={key++} line={l} />));
    }
    run = [];
  };
  let seenChange = false;
  for (const line of lines) {
    if (line.kind === "ctx") { run.push(line); continue; }
    flushCtx(seenChange ? "middle" : "start");
    seenChange = true;
    segments.push(<DiffLineRow key={key++} line={line} />);
  }
  flushCtx("end");
  return (
    <div className="max-h-60 overflow-y-auto rounded-lg bg-panel py-1.5 font-mono text-[10px] leading-relaxed">
      {segments}
    </div>
  );
}

// One managed-field row: an expandable line-diff for long text (persona) and
// any object-valued field (guardrails, mcpToolAllow, …), an inline "old → new"
// for everything else.
function ManagedDiffRow({ field, diff, newLabel = "template" }: {
  field: string; diff: ManagedDiff; newLabel?: string;
}) {
  if (field === "persona" || field === "guardrails"
      || isPlainObject(diff.old) || isPlainObject(diff.new)) {
    const oldText = typeof diff.old === "string" ? diff.old : JSON.stringify(diff.old ?? null, null, 2);
    const newText = typeof diff.new === "string" ? diff.new : JSON.stringify(diff.new ?? null, null, 2);
    return (
      <details className="group rounded-lg border border-pressed bg-canvas px-3 py-2 [&::-webkit-details-marker]:hidden">
        <summary className="flex cursor-pointer list-none items-center gap-1.5 text-xs font-medium text-ink">
          <span className="transition-transform group-open:rotate-90">▸</span>
          {fieldLabel(field)} <span className="text-[11px] font-normal text-mute">— changed · − current / + {newLabel}</span>
        </summary>
        <div className="mt-2">
          <UnifiedDiff oldText={oldText} newText={newText} />
        </div>
      </details>
    );
  }
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-pressed bg-canvas px-3 py-2 text-xs">
      <span className="shrink-0 font-medium text-ink">{fieldLabel(field)}</span>
      <span className="min-w-0 text-right text-mute">
        <span className="text-body">{fmtScalar(diff.old)}</span> → <span className="font-medium text-ink">{fmtScalar(diff.new)}</span>
      </span>
    </div>
  );
}

const KEPT_LINE = "memory · sessions · working folder · credentials · variable answers";
const SNAPSHOT_NOTE = "A pre-update snapshot enables one-step restore (guardrails only ever tighten).";

// One "adopt this instead of creating a duplicate" row — used both for the
// dry-run's `adoptable` suggestions and for a 409 adoption_required collision.
function AdoptionRow({ name, resourceKey, affectedAgents, checked, onToggle }: {
  name: string; resourceKey: string; affectedAgents: string[]; checked: boolean; onToggle: () => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-warn-soft bg-warn-soft px-3 py-2 text-xs text-warn">
      <input type="checkbox" className="mt-0.5 accent-primary" checked={checked} onChange={onToggle} />
      <span>
        <span className="font-medium">{name}</span>
        {" "}(also used by: {affectedAgents.length > 0 ? affectedAgents.join(", ") : "another agent"} ⚠)
      </span>
      <span className="ml-auto shrink-0 font-mono text-[10px] opacity-70">{resourceKey}</span>
    </label>
  );
}

// PRD-29 M4: display label per resource type — customTool/connector kept their
// existing "tool"/"connector" wording, the new types get their own.
const RESOURCE_KIND_LABELS: Record<ResourceRow["type"], string> = {
  customTool: "tool", connector: "connector",
  wiki: "wiki", knowledgeBase: "knowledge base", mcpServer: "MCP server",
};

// One resource row in the update modal — status-first, credentials inline.
// "adopt"/adopted rows carry the required-confirmation checkbox (pre-ticked by
// the modal); create rows carry their own credential inputs. wiki/knowledgeBase/
// mcpServer rows never carry secretKeys/retainedKeys, so credInputs is a no-op
// for them — no new section-specific UI needed beyond the label.
function ResourceRowItem({ row, checked, onToggle, secrets, setSecret }: {
  row: ResourceRow; checked: boolean; onToggle: () => void;
  secrets: Record<string, string>; setSecret: (k: string, v: string) => void;
}) {
  const kind = RESOURCE_KIND_LABELS[row.type];
  const credInputs = row.secretKeys.length > 0 && (
    <div className="mt-2 space-y-2">
      {row.secretKeys.map((k) => (
        <div key={k}>
          <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
            <span aria-hidden>🔒</span>{k}
          </label>
          <input className={inputCls} type="password" value={secrets[k] ?? ""}
            onChange={(e) => setSecret(k, e.target.value)} />
          <p className="mt-1 text-[11px] text-mute">Stored locally, never exported.</p>
        </div>
      ))}
    </div>
  );
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
              : "required to update"}
          </span>
          <span className="ml-auto shrink-0 font-mono text-[10px] text-mute">{kind}</span>
        </label>
        {shared && (
          <p className={`mt-1 pl-6 text-[11px] ${checked ? "text-mute" : "text-warn"}`}>shared with {shared}</p>
        )}
        {!checked && (
          <p className="mt-1 pl-6 text-[11px] text-warn">
            ⚠ The template manages this {kind} — untick only if you plan to remove
            it from the template. The update can't proceed while unticked.
          </p>
        )}
        {checked && credInputs && <div className="pl-6">{credInputs}</div>}
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
      {credInputs}
    </div>
  );
}

function ApplyTemplateModal({ agentId, onClose, onApplied }: {
  agentId: string; onClose: () => void; onApplied: () => void;
}) {
  const toast = useToast();
  const [dryRun, setDryRun] = useState<ApplyDryRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [variables, setVariables] = useState<Record<string, string>>({});
  // Secret answers (re-entered credentials) are kept separate from `variables`
  // and posted as `secrets` — never folded into the variables map.
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [missing, setMissing] = useState<MissingVariable[]>([]);
  const [adoptions, setAdoptions] = useState<Adoption[]>([]);
  // A 409 adoption_required collision the operator must confirm before retrying.
  const [collisions, setCollisions] = useState<Collision[]>([]);
  const [needsDuty, setNeedsDuty] = useState(false);
  const [confirmDuty, setConfirmDuty] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<{ needsAttention: NeedsAttentionItem[] } | null>(null);

  const runDryRun = useCallback(async (vars: Record<string, string>, secs: Record<string, string>, ads: Adoption[]) => {
    setLoading(true); setError(null);
    try {
      const r = await apiPost<ApplyDryRun>(
        `/api/agents/${encodeURIComponent(agentId)}/apply-template?dryRun=1`,
        { variables: vars, secrets: secs, adoptions: ads },
      );
      setDryRun(r);
      setMissing(r.missingVariables ?? []);
      setNeedsDuty(!!r.dutyActive);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [agentId]);

  useEffect(() => { runDryRun({}, {}, []); }, [runDryRun]);

  // Toggling an adoption changes what the preview would create/attach — re-run
  // the dry-run so the checklist reflects it (skip the very first render,
  // already covered by the mount effect above).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    runDryRun(variables, secrets, adoptions);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adoptions]);

  // Adoption is mandatory for every name-match (apply 409s otherwise), so the
  // boxes default to ticked: the first preview's "adopt" rows are confirmed
  // automatically — the operator sees "reuse yours" and can still untick one
  // to be told, inline, what that means. Runs once per modal open.
  const autoAdopted = useRef(false);
  useEffect(() => {
    if (autoAdopted.current || !dryRun?.resources) return;
    autoAdopted.current = true;
    const need = dryRun.resources.filter((r) => r.status === "adopt");
    if (need.length > 0) {
      setAdoptions(need.map((r) => ({ type: r.type, resourceKey: r.resourceKey })));
    }
  }, [dryRun]);

  const setVar = (k: string, v: string) => setVariables((s) => ({ ...s, [k]: v }));
  const setSecret = (k: string, v: string) => setSecrets((s) => ({ ...s, [k]: v }));
  const isAdopted = (type: Adoption["type"], resourceKey: string) =>
    adoptions.some((a) => a.type === type && a.resourceKey === resourceKey);
  const toggleAdoption = (type: Adoption["type"], resourceKey: string) =>
    setAdoptions((prev) => (
      prev.some((a) => a.type === type && a.resourceKey === resourceKey)
        ? prev.filter((a) => !(a.type === type && a.resourceKey === resourceKey))
        : [...prev, { type, resourceKey }]
    ));

  // Non-secret missing variables still feed `variables`; secret ones (incl.
  // reason "binding-reentry") join the credentials re-entry list below. The
  // `secrets` bucket is keyed by SECRET VARIABLE KEY only — missingVariables
  // is the sole source of truth for those keys (each revoked binding already
  // surfaces its real variable key here with reason "binding-reentry").
  // customTools.reentryRequired / connectors.reentryRequired hold tool
  // slugs / connector names, not secret-variable keys — they're display-only
  // (see reentryResourceLabels below), never folded into reentryKeys.
  const nonSecretMissing = missing.filter((m) => m.type !== "secret");
  const reentryKeys = useMemo(() => {
    const keys = new Set<string>();
    missing.filter((m) => m.type === "secret").forEach((m) => keys.add(m.key));
    return Array.from(keys);
  }, [missing]);

  const missingUnfilled = nonSecretMissing.some((m) => !(variables[m.key] || "").trim());
  const reentryUnfilled = reentryKeys.some((k) => !(secrets[k] || "").trim());
  const collisionsUnresolved = collisions.some((c) => !isAdopted(c.type, c.resourceKey));
  const resourceRows = dryRun?.resources ?? [];
  const adoptUnresolved = resourceRows.some(
    (r) => r.status === "adopt" && !isAdopted(r.type, r.resourceKey));
  // Re-entry keys not owned by any resource row (e.g. a revoked binding whose
  // resource left the template) still get a generic input below.
  const rowSecretKeys = new Set(resourceRows.flatMap((r) => r.secretKeys));
  const orphanReentryKeys = reentryKeys.filter((k) => !rowSecretKeys.has(k));
  const canRender = !!dryRun?.hasTemplate && !!dryRun?.sourceAvailable;

  const apply = async () => {
    if (!dryRun?.pins) return;
    setApplying(true); setError(null);
    const result = await postTemplateAction<{ needsAttention?: NeedsAttentionItem[] }>(
      `/api/agents/${encodeURIComponent(agentId)}/apply-template`,
      { pins: dryRun.pins, variables, secrets, adoptions, confirmDuty },
    );
    if (result.ok) {
      toast.success(`Updated to v${dryRun.toVersion ?? "latest"}`);
      setApplied({ needsAttention: result.data?.needsAttention ?? [] });
      setApplying(false);
      return;
    }
    setApplying(false);
    const { detail } = result;
    if (detail.code === "re_preview_required") {
      toast.info("Template or agent changed — re-previewing.");
      await runDryRun(variables, secrets, adoptions);
    } else if (detail.code === "variables_required") {
      setMissing(detail.missingVariables ?? []);
      setError(detail.message ?? "Additional answers are required.");
    } else if (detail.code === "confirm_duty_required") {
      setNeedsDuty(true);
      setError(detail.message ?? "This agent is on duty now — confirm to proceed.");
    } else if (detail.code === "adoption_required") {
      setCollisions(detail.collisions ?? []);
      setError(detail.message ?? "Some resources already exist under another agent — confirm adoption below to continue.");
    } else {
      setError(detail.message ?? `HTTP ${result.status}`);
    }
  };

  const managedRows = Object.entries(dryRun?.managed ?? {}).filter(
    ([field, diff]) => field !== "skills" && JSON.stringify(diff.old) !== JSON.stringify(diff.new),
  );
  const skills = dryRun?.skills;
  const hasSkillChanges = !!skills && (skills.added.length > 0 || skills.removed.length > 0);
  const canApply = canRender && !!dryRun?.pins && !missingUnfilled && !reentryUnfilled
    && !collisionsUnresolved && !adoptUnresolved
    && !(needsDuty && !confirmDuty) && !applying && !loading;

  if (applied) {
    return (
      <Modal title="Update from template" onClose={onClose} wide>
        <p className="text-sm text-body">Update applied.</p>
        <div className="mt-4">
          <NeedsAttentionPanel needsAttention={applied.needsAttention} />
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <button onClick={onApplied}
            className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">
            Done
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Update from template" onClose={onClose} wide>
      {loading && !dryRun && <p className="text-sm text-mute">Checking for changes…</p>}
      {dryRun && !dryRun.hasTemplate && (
        <p className="rounded-xl border border-pressed bg-canvas px-3 py-2 text-xs text-mute">This agent has no template to update from.</p>
      )}
      {dryRun?.hasTemplate && !dryRun.sourceAvailable && (
        <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 text-xs text-danger">
          The template source is no longer available — this agent can't be updated automatically.
        </p>
      )}
      {canRender && dryRun && (
        // A dry-run refresh (adoption toggled) keeps the previous preview on
        // screen — swapping to a loading state would flicker the checklist.
        <div className={`space-y-4 ${loading ? "opacity-60 transition-opacity" : ""}`}>
          <div className="flex items-center gap-2">
            <span className="font-display text-sm font-bold text-ink">v{dryRun.fromVersion} → v{dryRun.toVersion}</span>
            {dryRun.pins?.digest && (
              <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-mute">digest {dryRun.pins.digest.slice(0, 8)}</span>
            )}
          </div>

          {collisions.length > 0 && (
            <Section label="Confirm adoption to continue">
              <div className="space-y-1.5">
                {collisions.map((c) => (
                  <AdoptionRow key={`${c.type}-${c.resourceKey}`} name={c.name ?? c.resourceKey} resourceKey={c.resourceKey}
                    affectedAgents={c.affectedAgents ?? []} checked={isAdopted(c.type, c.resourceKey)}
                    onToggle={() => toggleAdoption(c.type, c.resourceKey)} />
                ))}
              </div>
            </Section>
          )}

          {managedRows.length > 0 && (
            <Section label="What changes">
              <div className="space-y-1.5">
                {managedRows.map(([field, diff]) => <ManagedDiffRow key={field} field={field} diff={diff} />)}
              </div>
            </Section>
          )}

          {(dryRun.localDivergence?.length ?? 0) > 0 && (
            <Section label="Locally changed since deploy (will be overwritten)">
              <div className="space-y-1.5">
                {dryRun.localDivergence!.map((d, i) => (
                  <div key={i} className="flex items-start justify-between gap-3 rounded-lg border border-warn-soft bg-warn-soft px-3 py-2 text-xs">
                    <span className="shrink-0 font-medium text-warn">{fieldLabel(d.field)}</span>
                    <span className="min-w-0 text-right text-warn">
                      {fmtScalar(d.current)} → {fmtScalar(d.oldTemplate)}
                    </span>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {hasSkillChanges && (
            <Section label="Skills">
              <div className="flex flex-wrap gap-1.5">
                {skills!.added.map((s) => (
                  <span key={`+${s}`} className="rounded-full bg-success-soft px-2 py-0.5 font-mono text-[10px] text-success">+{s}</span>
                ))}
                {skills!.removed.map((s) => (
                  <span key={`-${s}`} className="rounded-full bg-danger-soft px-2 py-0.5 font-mono text-[10px] text-danger">−{s}</span>
                ))}
              </div>
            </Section>
          )}

          {resourceRows.length > 0 && (
            <Section label={`Connectors & tools (${
              resourceRows.filter((r) => r.status !== "create").length} reused · ${
              resourceRows.filter((r) => r.status === "create").length} new)`}>
              <div className="space-y-1.5">
                {resourceRows.map((r) => (
                  <ResourceRowItem key={`${r.type}-${r.resourceKey}`} row={r}
                    checked={isAdopted(r.type, r.resourceKey)}
                    onToggle={() => toggleAdoption(r.type, r.resourceKey)}
                    secrets={secrets} setSecret={setSecret} />
                ))}
              </div>
            </Section>
          )}

          {orphanReentryKeys.length > 0 && (
            <Section label="Credentials needing re-entry">
              <div className="space-y-3">
                {orphanReentryKeys.map((k) => (
                  <div key={`reentry-${k}`}>
                    <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
                      <span aria-hidden>🔒</span>{k} <span className="text-[10px] text-mute">re-entry required</span>
                    </label>
                    <input className={inputCls} type="password" value={secrets[k] ?? ""}
                      onChange={(e) => setSecret(k, e.target.value)} />
                    <p className="mt-1 text-[11px] text-mute">Stored locally, never exported.</p>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {nonSecretMissing.length > 0 && (
            <Section label="Additional answers needed">
              <div className="space-y-3">
                {nonSecretMissing.map((v) => (
                  <div key={v.key}>
                    <label className="mb-1 block text-xs font-medium text-body">
                      {v.key} <span className="text-[10px] text-mute">({v.type})</span>
                    </label>
                    <input className={inputCls} type="text"
                      value={variables[v.key] ?? ""} onChange={(e) => setVar(v.key, e.target.value)} />
                  </div>
                ))}
              </div>
            </Section>
          )}

          <Section label="Kept (yours)">
            <p className="text-[11px] text-mute">{KEPT_LINE}</p>
          </Section>

          <p className="text-[11px] text-mute">{SNAPSHOT_NOTE}</p>

          {needsDuty && (
            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-warn-soft bg-warn-soft px-3 py-2.5">
              <input type="checkbox" className="mt-0.5 accent-primary" checked={confirmDuty}
                onChange={(e) => setConfirmDuty(e.target.checked)} />
              <span className="text-xs font-medium text-warn">This agent is on duty now — I understand</span>
            </label>
          )}
        </div>
      )}

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">
          {canRender ? "Cancel" : "Close"}
        </button>
        {canRender && (
          <button onClick={apply} disabled={!canApply}
            className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
            {applying ? "Applying…" : "Apply update"}
          </button>
        )}
      </div>
    </Modal>
  );
}

function RestoreTemplateModal({ agentId, onClose, onRestored }: {
  agentId: string; onClose: () => void; onRestored: () => void;
}) {
  const toast = useToast();
  const [dryRun, setDryRun] = useState<RestoreDryRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [needsDuty, setNeedsDuty] = useState(false);
  const [confirmDuty, setConfirmDuty] = useState(false);
  const [busy, setBusy] = useState(false);

  const runDryRun = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await apiPost<RestoreDryRun>(
        `/api/agents/${encodeURIComponent(agentId)}/restore-template?dryRun=1`, {},
      );
      setDryRun(r);
      setNeedsDuty(!!r.dutyActive);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [agentId]);

  useEffect(() => { runDryRun(); }, [runDryRun]);

  const restore = async () => {
    if (!dryRun?.pins) return;
    setBusy(true); setError(null);
    const result = await postTemplateAction(`/api/agents/${encodeURIComponent(agentId)}/restore-template`, {
      pins: dryRun.pins, confirmDuty,
    });
    if (result.ok) {
      toast.success("Restored to the pre-update snapshot.");
      onRestored();
      return;
    }
    setBusy(false);
    const { detail } = result;
    if (detail.code === "re_preview_required") {
      toast.info("The agent changed — re-previewing.");
      await runDryRun();
    } else if (detail.code === "confirm_duty_required") {
      setNeedsDuty(true);
      setError(detail.message ?? "This agent is on duty now — confirm to proceed.");
    } else {
      setError(detail.message ?? `HTTP ${result.status}`);
    }
  };

  const managedRows = Object.entries(dryRun?.managed ?? {}).filter(
    ([, diff]) => JSON.stringify(diff.old) !== JSON.stringify(diff.new),
  );
  const canRestore = !!dryRun?.hasSnapshot && !!dryRun?.pins && !(needsDuty && !confirmDuty) && !busy;

  return (
    <Modal title="Restore previous version" onClose={onClose} wide>
      {loading && <p className="text-sm text-mute">Checking the snapshot…</p>}
      {!loading && dryRun && !dryRun.hasSnapshot && (
        <p className="rounded-xl border border-pressed bg-canvas px-3 py-2 text-xs text-mute">No snapshot to restore.</p>
      )}
      {!loading && dryRun?.hasSnapshot && (
        <div className="space-y-4">
          <p className="text-xs text-body">Reverts the template-managed fields to how they were just before the last update.</p>
          {managedRows.length > 0 && (
            <Section label="Will change (current → snapshot)">
              <div className="space-y-1.5">
                {managedRows.map(([field, diff]) => <ManagedDiffRow key={field} field={field} diff={diff} newLabel="snapshot" />)}
              </div>
            </Section>
          )}
          {dryRun.note && <p className="text-[11px] text-mute">{dryRun.note}</p>}
          {needsDuty && (
            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-warn-soft bg-warn-soft px-3 py-2.5">
              <input type="checkbox" className="mt-0.5 accent-primary" checked={confirmDuty}
                onChange={(e) => setConfirmDuty(e.target.checked)} />
              <span className="text-xs font-medium text-warn">This agent is on duty now — I understand</span>
            </label>
          )}
        </div>
      )}

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-6 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">
          {dryRun?.hasSnapshot ? "Cancel" : "Close"}
        </button>
        {dryRun?.hasSnapshot && (
          <button onClick={restore} disabled={!canRestore}
            className="rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
            {busy ? "Restoring…" : "Restore"}
          </button>
        )}
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Skills panel
// --------------------------------------------------------------------------- //

const SKILLS_PAGE_SIZE = 8;

function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [builtin, setBuiltin] = useState<BuiltinSkill[]>([]);
  const [installing, setInstalling] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<(Skill & { body?: string; files?: string[] }) | null>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, b] = await Promise.all([
        apiJson<{ skills: Skill[] }>("/api/skills"),
        apiJson<{ skills: BuiltinSkill[] }>("/api/skills/builtin"),
      ]);
      setSkills(s.skills); setBuiltin(b.skills);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const [bQuery, setBQuery] = useState("");
  const [bTag, setBTag] = useState<string | null>(null);
  const [bPage, setBPage] = useState(1);

  // Builtin gallery grouped by category (research / ops / …), sorted by
  // the backend already. Search + tag (category) filter client-side.
  const builtinTags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of builtin) {
      const k = s.category || "general";
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [builtin]);

  // Flat filtered list (backend order: category, then name), paginated, then
  // the page slice re-grouped by category for display.
  const builtinFiltered = useMemo(() => {
    const q = bQuery.trim().toLowerCase();
    return builtin.filter((s) => {
      if (bTag && (s.category || "general") !== bTag) return false;
      if (!q) return true;
      return s.name.toLowerCase().includes(q) ||
        s.slug.toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q) ||
        (s.whenToUse || "").toLowerCase().includes(q);
    });
  }, [builtin, bQuery, bTag]);
  const bTotalPages = Math.max(1, Math.ceil(builtinFiltered.length / SKILLS_PAGE_SIZE));
  const bPageItems = builtinFiltered.slice((bPage - 1) * SKILLS_PAGE_SIZE, bPage * SKILLS_PAGE_SIZE);
  const builtinByCategory = useMemo(() => {
    const m = new Map<string, BuiltinSkill[]>();
    for (const s of bPageItems) {
      const k = s.category || "general";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(s);
    }
    return [...m.entries()];
  }, [bPageItems]);
  const builtinShown = builtinFiltered.length;
  useEffect(() => { setBPage(1); }, [bQuery, bTag]);              // new filter → first page
  useEffect(() => { if (bPage > bTotalPages) setBPage(bTotalPages); }, [bPage, bTotalPages]); // clamp
  // Slugs shipped as builtin — badges installed rows as "built-in" vs "uploaded".
  const builtinSlugs = useMemo(() => new Set(builtin.map((s) => s.slug)), [builtin]);

  const install = async (slug: string) => {
    setInstalling(slug); setError(null); setNotice(null);
    try {
      const r = await apiJson<{ installed: string[]; missingTools: string[]; domainGrants?: { skill: string; domains: string[] }[] }>(
        `/api/skills/install/${encodeURIComponent(slug)}`, { method: "POST" });
      const extra = r.installed.filter((s) => s !== slug);
      const parts = [`Installed ${r.installed.join(", ")}.`];
      if (extra.length) parts.push(`(pulled in required skill${extra.length > 1 ? "s" : ""}: ${extra.join(", ")})`);
      if (r.missingTools?.length)
        parts.push(`Warning: required CLI tool${r.missingTools.length > 1 ? "s" : ""} not on PATH: ${r.missingTools.join(", ")} — the skill may fail at runtime until installed.`);
      const grantedDomains = [...new Set((r.domainGrants ?? []).flatMap((g) => g.domains ?? []))];
      if (grantedDomains.length)
        parts.push(`Grants authenticated web access to: ${grantedDomains.join(", ")} (manage in Connectors).`);
      setNotice(parts.join(" "));
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setInstalling(null); }
  };

  // Installed builtin packs whose shipped copy drifted — drives "Update all".
  const updatableCount = useMemo(
    () => builtin.filter((s) => s.installed && s.updateAvailable).length, [builtin]);

  const updateAll = async () => {
    setInstalling("__all__"); setError(null); setNotice(null);
    try {
      const r = await apiJson<{ updated: string[]; missingTools: string[] }>(
        "/api/skills/update-all", { method: "POST" });
      const parts = [r.updated.length
        ? `Updated ${r.updated.join(", ")}.`
        : "All installed skills are already up to date."];
      if (r.missingTools?.length)
        parts.push(`Warning: required CLI tool${r.missingTools.length > 1 ? "s" : ""} not on PATH: ${r.missingTools.join(", ")} — the skill may fail at runtime until installed.`);
      setNotice(parts.join(" "));
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setInstalling(null); }
  };

  // Search across name / slug / description / when-to-use, then paginate the
  // matches client-side (the list endpoint returns every skill, already sorted).
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter((s) =>
      s.name.toLowerCase().includes(q) ||
      s.slug.toLowerCase().includes(q) ||
      (s.description || "").toLowerCase().includes(q) ||
      (s.whenToUse || "").toLowerCase().includes(q));
  }, [skills, query]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / SKILLS_PAGE_SIZE));
  const pageStart = (page - 1) * SKILLS_PAGE_SIZE;
  const pageItems = filtered.slice(pageStart, pageStart + SKILLS_PAGE_SIZE);
  useEffect(() => { setPage(1); }, [query]);                       // new search → first page
  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]); // clamp

  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true); setError(null);
    try {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f);
        const res = await apiFetch("/api/skills", { method: "POST", body: form });
        if (!res.ok) {
          const j = await res.json().catch(() => null);
          throw new Error(j?.detail || `upload failed (${res.status})`);
        }
      }
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const remove = async (slug: string) => {
    if (!confirm(`Delete skill "${slug}"?`)) return;
    try { await apiJson(`/api/skills/${encodeURIComponent(slug)}`, { method: "DELETE" }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const view = async (slug: string) => {
    try { setOpen(await apiJson(`/api/skills/${encodeURIComponent(slug)}`)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const exportSkill = async (slug: string) => {
    // Download the skill as a .zip bundle (round-trips through the uploader) so
    // it can be shared with others.
    try {
      const res = await apiFetch(`/api/skills/${encodeURIComponent(slug)}/export`);
      if (!res.ok) throw new Error(`export failed (${res.status})`);
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url; a.download = `${slug}.zip`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); upload(e.dataTransfer.files); }}
        className="rounded-2xl border border-dashed border-pressed bg-softer p-8 text-center"
      >
        <p className="text-sm text-body">Drop a <span className="font-mono">SKILL.md</span> or a <span className="font-mono">.zip</span> bundle here</p>
        <p className="mt-1 text-xs text-mute">agentskills.io format — frontmatter (name / description / when_to_use) + markdown body</p>
        <button onClick={() => fileRef.current?.click()} disabled={busy}
          className="mt-3 rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Uploading…" : "Choose file"}
        </button>
        <input ref={fileRef} type="file" accept=".md,.markdown,.zip" hidden multiple
          onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
      </div>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
      {notice && <p className="mt-3 rounded-xl border border-pressed bg-softer px-3 py-2 text-[11px] text-body">{notice}</p>}

      <div className="mt-6">
        <h3 className="font-display text-sm font-bold text-ink">Installed skills</h3>
        <p className="mt-0.5 text-xs text-mute">Your library — attach these to agents. Upload above, or install from the built-in gallery below.</p>
        {skills.length > 0 && (
          <div className="mt-2 flex items-center gap-3">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search installed skills…"
              className="min-w-0 flex-1 rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink placeholder:text-mute focus:border-primary focus:outline-none"
            />
            <span className="whitespace-nowrap text-xs text-mute">
              {filtered.length} {filtered.length === 1 ? "skill" : "skills"}
            </span>
          </div>
        )}

        <div className="mt-3 space-y-2">
          {skills.length === 0 && <p className="text-sm text-mute">No skills installed yet — upload a pack or install one from the built-in gallery below.</p>}
          {skills.length > 0 && filtered.length === 0 && (
            <p className="text-sm text-mute">No skills match “{query}”.</p>
          )}
          {pageItems.map((s) => (
            <div key={s.slug} className="flex items-start gap-3 rounded-xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{s.name}</span>
                  <span className="font-mono text-[10px] text-mute">{s.slug}</span>
                  <span className={`rounded-md border px-1.5 py-0.5 text-[10px] ${builtinSlugs.has(s.slug) ? "border-primary-line text-primary" : "border-pressed text-mute"}`}>
                    {builtinSlugs.has(s.slug) ? "built-in" : "uploaded"}
                  </span>
                </div>
                {s.description && <p className="mt-0.5 text-xs text-body">{s.description}</p>}
                {s.whenToUse && <p className="mt-0.5 text-xs text-mute">When: {s.whenToUse}</p>}
              </div>
              <button onClick={() => view(s.slug)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">View</button>
              <button onClick={() => exportSkill(s.slug)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Export</button>
              <button onClick={() => remove(s.slug)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Delete</button>
            </div>
          ))}
        </div>

        {totalPages > 1 && (
          <div className="mt-4 flex items-center justify-center gap-3">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40"
            >Prev</button>
            <span className="text-xs text-mute">Page {page} of {totalPages}</span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40"
            >Next</button>
          </div>
        )}
      </div>

      {builtinByCategory.length > 0 && (
        <div className="mt-6">
          <div className="flex items-center gap-3">
            <h3 className="font-display text-sm font-bold text-ink">Built-in skills</h3>
            {updatableCount > 0 && (
              <button onClick={updateAll} disabled={installing !== null}
                className="ml-auto rounded-lg bg-warn-soft px-3 py-1 text-xs font-semibold text-warn hover:opacity-80 disabled:opacity-40">
                {installing === "__all__" ? "Updating…" : `Update all (${updatableCount})`}
              </button>
            )}
          </div>
          <p className="mt-0.5 text-xs text-mute">Shipped with Virtual Worker — install into your library, then attach to agents.</p>
          <div className="mt-2 flex items-center gap-3">
            <input
              value={bQuery}
              onChange={(e) => setBQuery(e.target.value)}
              placeholder="Search built-in skills…"
              className="min-w-0 flex-1 rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink placeholder:text-mute focus:border-primary focus:outline-none"
            />
            <span className="whitespace-nowrap text-xs text-mute">
              {builtinShown} of {builtin.length}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <button onClick={() => setBTag(null)}
              className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${bTag === null ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"}`}>
              All
            </button>
            {builtinTags.map(([tag, count]) => (
              <button key={tag} onClick={() => setBTag(bTag === tag ? null : tag)}
                className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${bTag === tag ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"}`}>
                {tag} <span className="opacity-70">{count}</span>
              </button>
            ))}
          </div>
          {builtinByCategory.length === 0 && (
            <p className="mt-3 text-sm text-mute">No built-in skills match{bQuery ? ` “${bQuery}”` : ""}{bTag ? ` in ${bTag}` : ""}.</p>
          )}
          {builtinByCategory.map(([cat, items]) => (
            <div key={cat}>
              <p className="mt-3 text-xs font-semibold uppercase tracking-wide text-mute">{cat}</p>
              <div className="mt-1 space-y-2">
                {items.map((s) => (
                  <div key={s.slug} className="flex items-start gap-3 rounded-xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">{s.name}</span>
                        <span className="font-mono text-[10px] text-mute">{s.slug}</span>
                        {s.requiresTools.map((t) => (
                          <span key={t} title={s.toolsAvailable[t] ? `CLI tool "${t}" found on PATH` : `CLI tool "${t}" NOT on PATH`}
                            className={`rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${s.toolsAvailable[t] ? "border-pressed text-mute" : "border-danger-soft bg-danger-soft text-danger"}`}>
                            {t}{s.toolsAvailable[t] ? "" : " missing"}
                          </span>
                        ))}
                      </div>
                      {s.description && <p className="mt-0.5 text-xs text-body">{s.description}</p>}
                      {s.requiresSkills.length > 0 && (
                        <p className="mt-0.5 text-xs text-mute">Also installs: {s.requiresSkills.join(", ")}</p>
                      )}
                    </div>
                    {s.installed ? (
                      <div className="flex items-center gap-2">
                        {s.updateAvailable ? (
                          <span className="rounded-md bg-warn-soft px-2 py-1 text-xs font-medium text-warn">Update available</span>
                        ) : (
                          <span className="rounded-md bg-primary-soft px-2 py-1 text-xs font-medium text-primary">Installed</span>
                        )}
                        <button onClick={() => install(s.slug)} disabled={installing !== null}
                          className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink disabled:opacity-40">
                          {installing === s.slug ? "…" : s.updateAvailable ? "Update" : "Reinstall"}
                        </button>
                      </div>
                    ) : (
                      <button onClick={() => install(s.slug)} disabled={installing !== null}
                        className="rounded-lg bg-primary px-3 py-1 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-40">
                        {installing === s.slug ? "Installing…" : "Install"}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
          {bTotalPages > 1 && (
            <div className="mt-4 flex items-center justify-center gap-3">
              <button
                onClick={() => setBPage((p) => Math.max(1, p - 1))}
                disabled={bPage <= 1}
                className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40"
              >Prev</button>
              <span className="text-xs text-mute">Page {bPage} of {bTotalPages}</span>
              <button
                onClick={() => setBPage((p) => Math.min(bTotalPages, p + 1))}
                disabled={bPage >= bTotalPages}
                className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40"
              >Next</button>
            </div>
          )}
        </div>
      )}

      {open && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-ink/30 p-6" onClick={() => setOpen(null)}>
          <div className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-line bg-canvas p-5 shadow-[0_24px_60px_-30px_rgba(40,51,92,0.55)]" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center gap-2">
              <h3 className="font-display text-sm font-bold text-ink">{open.name}</h3>
              <span className="font-mono text-[10px] text-mute">{open.slug}</span>
              <button onClick={() => setOpen(null)} className="ml-auto text-mute hover:text-ink">✕</button>
            </div>
            {open.files && open.files.length > 0 && (
              <p className="mb-2 text-xs text-mute">Resources: {open.files.join(", ")}</p>
            )}
            <pre className="whitespace-pre-wrap rounded-xl bg-panel p-3 font-mono text-[12px] leading-relaxed text-panel-ink">{open.body}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// MCP panel
// --------------------------------------------------------------------------- //

function parseEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq > 0) env[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
  }
  return env;
}

function McpPanel() {
  const toast = useToast();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tools, setTools] = useState<Record<string, { name: string; description: string }[] | string>>({});

  // add/edit form. editingId is null for a new server, else the server being edited.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [argsStr, setArgsStr] = useState("");
  const [envStr, setEnvStr] = useState("");
  const [url, setUrl] = useState("");
  const [allowedStr, setAllowedStr] = useState("");

  const resetForm = () => {
    setEditingId(null); setTransport("stdio");
    setName(""); setCommand(""); setArgsStr(""); setEnvStr(""); setUrl(""); setAllowedStr("");
  };
  const startEdit = (s: McpServer) => {
    setEditingId(s.id); setTransport(s.transport); setName(s.name);
    setCommand(s.command || ""); setArgsStr((s.args || []).join(" ")); setUrl(s.url || "");
    setAllowedStr((s.allowedTools || []).join(", "));
    setEnvStr(Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join("\n"));
    setError(null);
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const load = useCallback(async () => {
    try {
      const r = await apiJson<{ servers: McpServer[]; available: boolean }>("/api/mcp");
      setServers(r.servers); setAvailable(r.available);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const submit = async () => {
    if (!name.trim()) { setError("Give the server a name."); return; }
    setBusy(true); setError(null);
    const allowedTools = allowedStr.split(/[,\n]/).map((t) => t.trim()).filter(Boolean);
    const body = transport === "http"
      ? { name: name.trim(), transport, url: url.trim(), allowedTools }
      : { name: name.trim(), transport, command: command.trim(),
          args: argsStr.trim() ? argsStr.trim().split(/\s+/) : [], env: parseEnv(envStr),
          allowedTools };
    try {
      const wasEditing = !!editingId;
      if (editingId) {
        await apiJson(`/api/mcp/${encodeURIComponent(editingId)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        await apiPost("/api/mcp", body);
      }
      resetForm();
      await load();
      toast.success(wasEditing ? "MCP server updated" : `MCP server “${body.name}” added`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast.error(`Save failed — ${msg}`);
    }
    finally { setBusy(false); }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this MCP server?")) return;
    try { await apiJson(`/api/mcp/${encodeURIComponent(id)}`, { method: "DELETE" }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const toggleEnabled = async (s: McpServer) => {
    try {
      await apiJson(`/api/mcp/${encodeURIComponent(s.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !s.enabled }),
      });
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const test = async (id: string) => {
    setTools((t) => ({ ...t, [id]: "…" }));
    try {
      const r = await apiPost<{ tools: { name: string; description: string }[] }>(`/api/mcp/${encodeURIComponent(id)}/tools`, {});
      setTools((t) => ({ ...t, [id]: r.tools }));
    } catch (e) { setTools((t) => ({ ...t, [id]: e instanceof Error ? e.message : String(e) })); }
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      {!available && (
        <p className="mb-4 rounded-xl border border-warn-soft bg-warn-soft px-3 py-2 text-xs text-warn">
          The <span className="font-mono">mcp</span> Python SDK isn't installed — run <span className="font-mono">setup.sh</span> to enable MCP. You can still save server configs.
        </p>
      )}

      <div className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h3 className="font-display mb-3 text-sm font-bold text-ink">{editingId ? "Edit MCP server" : "Add MCP server"}</h3>
        <div className="space-y-3">
          <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
            {(["stdio", "http"] as const).map((t) => (
              <button key={t} onClick={() => setTransport(t)}
                className={`flex-1 rounded-lg px-3 py-1 text-sm font-medium transition-colors ${
                  transport === t ? "bg-primary text-white shadow-[0_6px_14px_-8px_rgba(43,60,130,0.7)]" : "text-body hover:text-ink"
                }`}>{t === "stdio" ? "stdio (local command)" : "http (remote URL)"}</button>
            ))}
          </div>

          <Field label="Name">
            <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Filesystem" />
          </Field>

          {transport === "stdio" ? (
            <>
              <Field label="Command" hint="The executable to spawn locally.">
                <input className={inputCls} value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" />
              </Field>
              <Field label="Arguments" hint="Space-separated.">
                <input className={inputCls} value={argsStr} onChange={(e) => setArgsStr(e.target.value)}
                  placeholder="-y @modelcontextprotocol/server-filesystem /path" />
              </Field>
              <Field label="Environment" hint="One KEY=value per line (optional).">
                <textarea className={`${inputCls} min-h-[60px] resize-y font-mono text-xs`} value={envStr}
                  onChange={(e) => setEnvStr(e.target.value)} placeholder="API_KEY=…" />
              </Field>
            </>
          ) : (
            <Field label="URL" hint="A streamable-HTTP MCP endpoint. Traffic leaves this machine.">
              <input className={inputCls} value={url} onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/mcp" />
            </Field>
          )}

          <Field label="Allowed tools"
            hint="Comma-separated tool names to expose to agents; empty = every tool the server advertises. Use Test below to see the names. Handy when a server mixes read and write tools.">
            <input className={inputCls} value={allowedStr} onChange={(e) => setAllowedStr(e.target.value)}
              placeholder="get_order_status, search_docs, …  (empty = all)" />
          </Field>

          <div className="flex items-center gap-2">
            <button onClick={submit} disabled={busy}
              className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
              {busy ? (editingId ? "Saving…" : "Adding…") : (editingId ? "Save changes" : "Add server")}
            </button>
            {editingId && (
              <button onClick={resetForm} disabled={busy}
                className="rounded-xl border border-pressed px-4 py-2 text-sm font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-40">
                Cancel
              </button>
            )}
          </div>
        </div>
      </div>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-5 space-y-2">
        {servers.length === 0 && <p className="text-sm text-mute">No MCP servers yet.</p>}
        {servers.map((s) => {
          const result = tools[s.id];
          return (
            <div key={s.id} className={`rounded-xl border bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)] ${editingId === s.id ? "border-primary-line ring-1 ring-primary-line" : "border-line"}`}>
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{s.name}</span>
                    <span className="rounded-full border border-pressed px-2 py-0.5 font-mono text-[10px] text-mute">{s.transport}</span>
                    {(s.allowedTools?.length ?? 0) > 0 && (
                      <span className="rounded-full border border-pressed px-2 py-0.5 font-mono text-[10px] text-mute"
                        title={s.allowedTools.join(", ")}>{s.allowedTools.length} tool{s.allowedTools.length > 1 ? "s" : ""} allowed</span>
                    )}
                    {!s.enabled && <span className="font-mono text-[10px] text-mute">disabled</span>}
                    {editingId === s.id && <span className="font-mono text-[10px] text-primary-deep">editing…</span>}
                  </div>
                  <p className="mt-0.5 truncate font-mono text-[11px] text-body">
                    {s.transport === "http" ? s.url : `${s.command} ${s.args.join(" ")}`}
                  </p>
                </div>
                <button onClick={() => test(s.id)} disabled={!available}
                  className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink disabled:opacity-40">Test</button>
                <button onClick={() => startEdit(s)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Edit</button>
                <button onClick={() => toggleEnabled(s)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">{s.enabled ? "Disable" : "Enable"}</button>
                <button onClick={() => remove(s.id)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Delete</button>
              </div>
              {result !== undefined && (
                <div className="mt-2 border-t border-pressed pt-2">
                  {typeof result === "string" ? (
                    <p className="font-mono text-[11px] text-mute">{result === "…" ? "Connecting…" : result}</p>
                  ) : result.length === 0 ? (
                    <p className="text-xs text-mute">No tools exposed.</p>
                  ) : (
                    <div className="flex flex-wrap gap-1.5">
                      {result.map((t) => (
                        <span key={t.name} title={t.description} className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{t.name}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Tools panel — custom HTTP tools (the lightweight alternative to MCP)
// --------------------------------------------------------------------------- //

const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;
type Method = (typeof HTTP_METHODS)[number];

interface Param { name: string; type: string; required: boolean; description: string }

const PARAM_TYPES = ["string", "number", "integer", "boolean"] as const;

// JSON Schema <-> editable param rows. A tool's `parameters` is a JSON-Schema
// object; the builder edits it as a flat list of named params (M1 scope).
function schemaToParams(schema: CustomTool["parameters"] | undefined): Param[] {
  const props = (schema?.properties as Record<string, { type?: string; description?: string }>) || {};
  const req = new Set(schema?.required || []);
  return Object.entries(props).map(([name, p]) => ({
    name, type: p?.type || "string", required: req.has(name), description: p?.description || "",
  }));
}
function paramsToSchema(params: Param[]): CustomTool["parameters"] {
  const properties: Record<string, { type: string; description?: string }> = {};
  const required: string[] = [];
  for (const p of params) {
    const name = p.name.trim();
    if (!name) continue;
    properties[name] = { type: p.type };
    if (p.description.trim()) properties[name].description = p.description.trim();
    if (p.required) required.push(name);
  }
  return required.length ? { type: "object", properties, required } : { type: "object", properties };
}

function headersToText(h: Record<string, string>): string {
  return Object.entries(h).map(([k, v]) => `${k}=${v}`).join("\n");
}

const blankToolForm = () => ({
  id: "", name: "", description: "", method: "GET" as Method, url: "",
  params: [] as Param[], headersStr: "", fixedStr: "", allowPrivate: false, timeout: 20,
});

function ToolsPanel() {
  const toast = useToast();
  const [tools, setTools] = useState<CustomTool[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(blankToolForm());
  const [results, setResults] = useState<Record<string, string>>({});
  const [testArgs, setTestArgs] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try { setTools((await apiJson<{ tools: CustomTool[] }>("/api/tools")).tools); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const editing = !!form.id;
  const setF = (p: Partial<ReturnType<typeof blankToolForm>>) => setForm((f) => ({ ...f, ...p }));
  const setParam = (i: number, p: Partial<Param>) =>
    setForm((f) => ({ ...f, params: f.params.map((x, j) => (j === i ? { ...x, ...p } : x)) }));
  const addParam = () => setForm((f) => ({ ...f, params: [...f.params, { name: "", type: "string", required: false, description: "" }] }));
  const removeParam = (i: number) => setForm((f) => ({ ...f, params: f.params.filter((_, j) => j !== i) }));

  // Live preview of the shape sent to the endpoint, so it's clear what each
  // parameter becomes: a JSON body (POST/PUT/PATCH) or a query string (GET/DELETE).
  // Model-supplied params show a "…" placeholder; operator-pinned fixed params
  // show their literal value. Dotted names (a.b) render as nested objects.
  const namedParams = form.params.filter((p) => p.name.trim());
  const fixedEntries = Object.entries(parseEnv(form.fixedStr));
  const isQuery = form.method === "GET" || form.method === "DELETE";
  const sample = (t: string): unknown => (t === "number" || t === "integer" ? 0 : t === "boolean" ? true : "…");
  const setDotted = (obj: Record<string, unknown>, key: string, val: unknown) => {
    const parts = key.split(".");
    let node = obj;
    for (const p of parts.slice(0, -1)) {
      if (typeof node[p] !== "object" || node[p] === null) node[p] = {};
      node = node[p] as Record<string, unknown>;
    }
    node[parts[parts.length - 1]] = val;
  };
  let paramPreview = "";
  if (namedParams.length || fixedEntries.length) {
    if (isQuery) {
      paramPreview = "?" + [
        ...namedParams.map((p) => `${p.name.trim()}=…`),
        ...fixedEntries.map(([k, v]) => `${k}=${v}`),
      ].join("&");
    } else {
      const obj: Record<string, unknown> = {};
      for (const p of namedParams) setDotted(obj, p.name.trim(), sample(p.type));
      for (const [k, v] of fixedEntries) setDotted(obj, k, v); // fixed wins
      paramPreview = JSON.stringify(obj, null, 2);
    }
  }

  const startEdit = (t: CustomTool) => {
    setForm({
      id: t.id, name: t.name, description: t.description, method: t.method, url: t.url,
      params: schemaToParams(t.parameters), headersStr: headersToText(t.headers),
      fixedStr: headersToText(t.fixedParams || {}),
      allowPrivate: t.allowPrivate, timeout: t.timeout,
    });
    setError(null);
  };

  const save = async () => {
    if (!form.name.trim()) { setError("Give the tool a name."); return; }
    setBusy(true); setError(null);
    const body = {
      name: form.name.trim(), description: form.description, method: form.method,
      url: form.url.trim(), parameters: paramsToSchema(form.params),
      headers: parseEnv(form.headersStr), fixedParams: parseEnv(form.fixedStr),
      allowPrivate: form.allowPrivate,
      timeout: form.timeout,
    };
    try {
      const wasEditing = editing;
      if (editing) {
        await apiJson(`/api/tools/${encodeURIComponent(form.id)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
      } else {
        await apiPost("/api/tools", body);
      }
      setForm(blankToolForm());
      await load();
      toast.success(wasEditing ? "Tool updated" : `Tool “${body.name}” created`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast.error(`Save failed — ${msg}`);
    }
    finally { setBusy(false); }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this custom tool?")) return;
    try { await apiJson(`/api/tools/${encodeURIComponent(id)}`, { method: "DELETE" }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const toggleEnabled = async (t: CustomTool) => {
    try {
      await apiJson(`/api/tools/${encodeURIComponent(t.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !t.enabled }),
      });
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const test = async (id: string) => {
    setResults((r) => ({ ...r, [id]: "…" }));
    let args: Record<string, unknown> = {};
    const raw = (testArgs[id] || "").trim();
    if (raw) {
      try { args = JSON.parse(raw); }
      catch { setResults((r) => ({ ...r, [id]: "sample args must be valid JSON" })); return; }
    }
    try {
      const res = await apiPost<{ result: string }>(`/api/tools/${encodeURIComponent(id)}/test`, { args });
      setResults((r) => ({ ...r, [id]: res.result }));
    } catch (e) { setResults((r) => ({ ...r, [id]: e instanceof Error ? e.message : String(e) })); }
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h3 className="font-display mb-1 text-sm font-bold text-ink">{editing ? "Edit custom tool" : "New custom tool"}</h3>
        <p className="mb-3 text-xs text-mute">An HTTP endpoint the agent can call by declaration — no MCP server, no backend code. The model's arguments are sent as query params (GET/DELETE) or a JSON body.</p>
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <Field label="Name" hint="What the model calls. Lowercase identifier: ^[a-z][a-z0-9_]*$.">
                <input className={inputCls} value={form.name} onChange={(e) => setF({ name: e.target.value })}
                  placeholder="e.g. lookup_order" />
              </Field>
            </div>
            <Field label="Method">
              <select className={inputCls} value={form.method} onChange={(e) => setF({ method: e.target.value as Method })}>
                {HTTP_METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </Field>
          </div>

          <Field label="Description" hint="Tells the model when to use this tool.">
            <input className={inputCls} value={form.description} onChange={(e) => setF({ description: e.target.value })}
              placeholder="Look up an order by id in our internal API" />
          </Field>

          <Field label="URL" hint="The endpoint to call. Traffic leaves this machine.">
            <input className={inputCls} value={form.url} onChange={(e) => setF({ url: e.target.value })}
              placeholder="https://api.example.com/orders" />
          </Field>

          <Field label="Parameters"
            hint={`The arguments the model fills in. Each becomes a key ${form.method === "GET" || form.method === "DELETE" ? "in the query string" : "in the JSON request body"}. For a nested body, use a dotted name like message.input_str.`}>
            <div className="space-y-2">
              {form.params.length === 0 && (
                <p className="rounded-xl border border-dashed border-pressed px-3 py-3 text-center text-xs text-mute">
                  No parameters yet — the tool takes no arguments.
                </p>
              )}
              {form.params.map((p, i) => (
                <div key={i} className="rounded-xl border border-pressed bg-soft/50 p-3">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1 space-y-2">
                      <div className="grid grid-cols-[1fr_8rem] gap-2">
                        <div>
                          <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-mute">Field name</label>
                          <input className={inputCls} value={p.name} onChange={(e) => setParam(i, { name: e.target.value })} placeholder="e.g. message.input_str" />
                        </div>
                        <div>
                          <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-mute">Type</label>
                          <select className={inputCls} value={p.type} onChange={(e) => setParam(i, { type: e.target.value })}>
                            {PARAM_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                          </select>
                        </div>
                      </div>
                      <div>
                        <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-mute">
                          Description <span className="font-normal normal-case text-mute">— tells the model what to put here</span>
                        </label>
                        <input className={inputCls} value={p.description} onChange={(e) => setParam(i, { description: e.target.value })} placeholder="The deployment id to invoke" />
                      </div>
                      <label className="flex w-fit cursor-pointer items-center gap-1.5 text-xs text-body">
                        <input type="checkbox" checked={p.required} onChange={() => setParam(i, { required: !p.required })} className="accent-primary" />
                        Required
                      </label>
                    </div>
                    <button onClick={() => removeParam(i)} title="Remove parameter"
                      className="shrink-0 rounded-lg border border-pressed px-2 py-1 text-mute transition-colors hover:border-danger hover:text-danger">✕</button>
                  </div>
                </div>
              ))}
              <button onClick={addParam} className="rounded-lg border border-pressed px-2.5 py-1.5 text-xs font-medium text-body hover:border-primary hover:text-ink">+ Add parameter</button>
              {paramPreview && (
                <div className="rounded-xl border border-pressed bg-panel p-3">
                  <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-mute">
                    {form.method === "GET" || form.method === "DELETE" ? "Sent as query string" : "Sent as JSON body"}
                  </p>
                  <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-panel-ink">{paramPreview}</pre>
                </div>
              )}
            </div>
          </Field>

          <Field label="Fixed parameters"
            hint={`Constant values always sent ${form.method === "GET" || form.method === "DELETE" ? "in the query string" : "in the JSON body"}, alongside the model's. One KEY=value per line; dotted keys (a.b) nest. The model never sees or sets these.`}>
            <textarea className={`${inputCls} min-h-[52px] resize-y font-mono text-xs`} value={form.fixedStr}
              onChange={(e) => setF({ fixedStr: e.target.value })} placeholder={"endpoint_deployment_hash_id=m53bgubjnayvc6j2bpmi7hzf\nendpoint_deployment_key=…"} />
          </Field>

          <Field label="Headers" hint="One KEY=value per line. May carry an API key — stored locally, sent only on the request, never shown to the model.">
            <textarea className={`${inputCls} min-h-[60px] resize-y font-mono text-xs`} value={form.headersStr}
              onChange={(e) => setF({ headersStr: e.target.value })} placeholder="Authorization=Bearer sk-…" />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Timeout (s)">
              <input className={inputCls} type="number" min={1} max={120} value={form.timeout}
                onChange={(e) => setF({ timeout: Number(e.target.value) || 20 })} />
            </Field>
            <div className="flex items-end justify-between pb-2">
              <label className="text-xs font-medium text-body" title="By default private/loopback hosts are blocked (SSRF guard). Enable only for a service on your own machine/network.">Allow private hosts</label>
              <button onClick={() => setF({ allowPrivate: !form.allowPrivate })}
                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                  form.allowPrivate ? "border-warn bg-warn text-white" : "border-pressed bg-canvas text-mute hover:text-ink"
                }`}>{form.allowPrivate ? "On" : "Off"}</button>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button onClick={save} disabled={busy}
              className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
              {busy ? "Saving…" : editing ? "Save changes" : "Create tool"}
            </button>
            {editing && (
              <button onClick={() => setForm(blankToolForm())}
                className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
            )}
          </div>
        </div>
      </div>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-5 space-y-2">
        {tools.length === 0 && <p className="text-sm text-mute">No custom tools yet.</p>}
        {tools.map((t) => {
          const result = results[t.id];
          return (
            <div key={t.id} className="rounded-xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium">{t.name}</span>
                    <span className="rounded-full border border-pressed px-2 py-0.5 font-mono text-[10px] text-mute">{t.method}</span>
                    {!t.enabled && <span className="font-mono text-[10px] text-mute">disabled</span>}
                    {t.allowPrivate && <span className="rounded-full bg-warn-soft px-2 py-0.5 text-[10px] text-warn">private ok</span>}
                  </div>
                  {t.description && <p className="mt-0.5 text-xs text-body">{t.description}</p>}
                  <p className="mt-0.5 truncate font-mono text-[11px] text-mute">{t.url}</p>
                </div>
                <button onClick={() => startEdit(t)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Edit</button>
                <button onClick={() => toggleEnabled(t)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">{t.enabled ? "Disable" : "Enable"}</button>
                <button onClick={() => remove(t.id)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Delete</button>
              </div>
              <div className="mt-2 flex items-center gap-2 border-t border-pressed pt-2">
                <input className={`${inputCls} flex-1 font-mono text-[11px]`} value={testArgs[t.id] || ""}
                  onChange={(e) => setTestArgs((a) => ({ ...a, [t.id]: e.target.value }))}
                  placeholder='sample args, e.g. {"order_id":"123"}' />
                <button onClick={() => test(t.id)} className="shrink-0 rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Test</button>
              </div>
              {result !== undefined && (
                <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-lg bg-panel p-2 font-mono text-[11px] leading-relaxed text-panel-ink">{result === "…" ? "Calling…" : result}</pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Bits
// --------------------------------------------------------------------------- //

// An agent's monogram — its identity at a glance (first letter on its brand accent).
function Monogram({ name, size = "lg", muted = false }: { name: string; size?: "sm" | "lg"; muted?: boolean }) {
  const ch = (name || "").trim().charAt(0).toUpperCase() || "+";
  const dim = size === "lg" ? "h-12 w-12 rounded-2xl text-xl" : "h-9 w-9 rounded-xl text-sm";
  const skin = muted || !ch || ch === "+" ? "bg-soft text-mute" : `${agentAccent(name)} text-white`;
  return (
    <div className={`font-display flex shrink-0 items-center justify-center font-bold shadow-[0_8px_18px_-10px_rgba(43,60,130,0.6)] ${dim} ${skin}`}>
      {ch}
    </div>
  );
}

// A titled surface — the unit the cockpit is built from.
function Card({ title, hint, badge, children }: {
  title?: string; hint?: string; badge?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      {(title || badge) && (
        <div className="mb-3 flex items-center gap-1.5">
          {title && <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">{title}</h3>}
          {hint && <HintIcon label={title} hint={hint} />}
          {badge && <span className="ml-auto">{badge}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

// A labeled sub-section inside a grouped Card (e.g. Skills within Knowledge &
// skills), with an optional "N on" count so a card can hold several capabilities
// without each needing its own box.
function SubSection({ label, hint, count, children }: {
  label: string; hint?: string; count?: number; children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-1.5">
        <span className="text-xs font-semibold text-ink">{label}</span>
        {hint && <span className="text-[11px]"><HintIcon label={label} hint={hint} /></span>}
        {typeof count === "number" && count > 0 && (
          <span className="ml-auto text-[10px] font-medium text-primary">{count} on</span>
        )}
      </div>
      {children}
    </div>
  );
}

// A native collapsible for rarely-touched knobs — keeps advanced settings out of
// the way without extra React state.
function Advanced({ label = "Advanced", children }: { label?: string; children: React.ReactNode }) {
  return (
    <details className="group [&::-webkit-details-marker]:hidden">
      <summary className="flex cursor-pointer list-none items-center gap-1 text-[11px] font-semibold text-mute hover:text-ink">
        <span className="transition-transform group-open:rotate-90">▸</span>{label}
      </summary>
      <div className="mt-3 space-y-4">{children}</div>
    </details>
  );
}

// --- shared search + pagination primitives -------------------------------- //
// Used by the picker Cards (Skills/Knowledge/Wiki/MCP/Tools) and MemoryCard so a
// long list stays browsable: type to filter, page through the rest.

const CHIP_PAGE_SIZE = 24;   // chips per page in a picker Card

// A compact search input with an optional "N of M" match counter.
function SearchBox({ value, onChange, placeholder, count, total }: {
  value: string; onChange: (v: string) => void; placeholder: string;
  count?: number; total?: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <input className={`${inputCls} flex-1`} value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} />
      {value.trim() !== "" && count !== undefined && (
        <span className="shrink-0 font-mono text-[10px] text-mute">{count}/{total}</span>
      )}
    </div>
  );
}

// Prev / page-of / Next controls. No-op parent when there's a single page.
function Pager({ page, totalPages, setPage }: {
  page: number; totalPages: number; setPage: Dispatch<SetStateAction<number>>;
}) {
  return (
    <div className="flex items-center justify-center gap-3 pt-1">
      <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}
        className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40">Prev</button>
      <span className="text-xs text-mute">Page {page} of {totalPages}</span>
      <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page >= totalPages}
        className="rounded-lg border border-pressed px-3 py-1 text-xs text-body hover:text-ink disabled:opacity-40">Next</button>
    </div>
  );
}

// A searchable, paginated grid of toggle chips — the shared body of every
// "attach items to this agent" Card. Callers supply how to read/label/match/
// toggle an item; the search + paging behaviour lives here once.
function ChipPicker<T>({ items, isOn, onToggle, keyOf, labelOf, titleOf, searchText, suffixOf, empty, placeholder, selected, onRemoveMissing }: {
  items: T[];
  isOn: (t: T) => boolean;
  onToggle: (t: T) => void;
  keyOf: (t: T) => string;
  labelOf: (t: T) => string;
  titleOf: (t: T) => string;
  searchText: (t: T) => string;
  suffixOf?: (t: T) => string;
  empty: string;
  placeholder: string;
  selected?: string[];          // saved keys — may reference records that no longer exist
  onRemoveMissing?: (key: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () => (q ? items.filter((t) => searchText(t).toLowerCase().includes(q)) : items),
    // searchText is a stable inline arrow; intentionally keyed on items + query only.
    [items, q], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / CHIP_PAGE_SIZE));
  useEffect(() => { setPage(1); }, [q]);                                  // new search → first page
  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]); // clamp
  const start = (page - 1) * CHIP_PAGE_SIZE;
  const pageItems = filtered.slice(start, start + CHIP_PAGE_SIZE);

  // Saved keys whose record was deleted/renamed: without a chip they are
  // uncountable-but-unremovable (the "3 on, 2 chips" bug) — render them as
  // dashed ghost chips a click removes.
  const known = new Set(items.map(keyOf));
  const missing = (selected ?? []).filter((k) => !known.has(k));
  const missingChips = missing.length > 0 && onRemoveMissing && (
    <div className="flex flex-wrap gap-2">
      {missing.map((k) => (
        <button key={`missing:${k}`} title="This record no longer exists — click to remove it from the agent."
          onClick={() => onRemoveMissing(k)}
          className="inline-flex items-center gap-1 rounded-full border border-dashed border-pressed bg-canvas px-3 py-1.5 text-xs font-medium text-mute line-through hover:border-primary-line hover:text-ink">
          <span aria-hidden>✕</span>{k} (missing)
        </button>
      ))}
    </div>
  );

  if (items.length === 0) return <div className="space-y-2">{missingChips}<p className="text-xs text-mute">{empty}</p></div>;
  return (
    <div className="space-y-2">
      {missingChips}
      <SearchBox value={query} onChange={setQuery} placeholder={placeholder}
        count={filtered.length} total={items.length} />
      {filtered.length === 0 ? (
        <p className="text-xs text-mute">No matches for “{query.trim()}”.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {pageItems.map((t) => {
            const on = isOn(t);
            return (
              <button key={keyOf(t)} title={titleOf(t)} onClick={() => onToggle(t)}
                className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                  on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                }`}><span aria-hidden>{on ? "✓" : "+"}</span>{labelOf(t)}{suffixOf ? suffixOf(t) : ""}</button>
            );
          })}
        </div>
      )}
      {totalPages > 1 && <Pager page={page} totalPages={totalPages} setPage={setPage} />}
    </div>
  );
}

// Per-agent, per-server MCP tool selection. One collapsible row per attached
// server: "all tools" (the key is absent/empty) or an explicit ticked subset,
// intersected with the server-level ceiling. Tool lists are fetched on first
// expand and cached per server id. See runner.merge_mcp_allow for the runtime.
function McpToolPicker({ servers, selectedIds, toolAllow, onChange }: {
  servers: McpServer[];
  selectedIds: string[];
  toolAllow: Record<string, string[]>;
  onChange: (next: Record<string, string[]>) => void;
}) {
  const attached = useMemo(
    () => servers.filter((s) => selectedIds.includes(s.id)),
    [servers, selectedIds],
  );
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [tools, setTools] = useState<Record<string, McpTool[]>>({});   // cache per server id
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [query, setQuery] = useState<Record<string, string>>({});      // per server id
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({}); // `${serverId}:${prefix}`

  if (attached.length === 0) return null;

  const fetchTools = async (id: string) => {
    if (tools[id] || loading[id]) return;                              // cached / in-flight
    setLoading((l) => ({ ...l, [id]: true }));
    setErrors((e) => { const n = { ...e }; delete n[id]; return n; });
    try {
      const res = await apiPost<{ tools: McpTool[] }>(`/api/mcp/${encodeURIComponent(id)}/tools`, {});
      setTools((t) => ({ ...t, [id]: res.tools }));
    } catch (err) {
      setErrors((e) => ({ ...e, [id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      setLoading((l) => { const n = { ...l }; delete n[id]; return n; });
    }
  };

  const toggleExpand = (id: string) => {
    const next = !open[id];
    setOpen((o) => ({ ...o, [id]: next }));
    if (next) void fetchTools(id);
  };

  // Store `names` for a server. Every eligible tool ticked normalizes back to
  // an ABSENT key (= all, future server tools auto-included); an empty list is
  // kept as an explicit NONE (the "uncheck all" state).
  const setSelection = (id: string, names: string[], eligible: string[]) => {
    const uniq = names.filter((n, i) => names.indexOf(n) === i);
    const next = { ...toolAllow };
    const isAll = eligible.length > 0 && eligible.every((e) => uniq.includes(e));
    if (isAll) delete next[id];
    else next[id] = uniq;
    onChange(next);
  };
  const setAll = (id: string) => { const next = { ...toolAllow }; delete next[id]; onChange(next); };
  const setNone = (id: string) => onChange({ ...toolAllow, [id]: [] });

  return (
    <div className="mt-2 space-y-2">
      {attached.map((s) => {
        const stored = toolAllow[s.id];        // undefined = all · [] = none · [names]
        const sel = stored ?? [];
        const isAll = stored === undefined;
        const ceiling = s.allowedTools || [];
        const list = tools[s.id];
        const eligible = (list || [])
          .filter((t) => ceiling.length === 0 || ceiling.includes(t.name))
          .map((t) => t.name);
        return (
          <div key={s.id} className="rounded-xl border border-pressed bg-canvas">
            <button type="button" onClick={() => toggleExpand(s.id)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left">
              <span aria-hidden className={`text-mute transition-transform ${open[s.id] ? "rotate-90" : ""}`}>▸</span>
              <span className="text-xs font-medium text-body">{s.name}</span>
              <span className="min-w-0 truncate text-[11px] text-mute" title={isAll ? "every tool the server advertises" : sel.join(", ")}>
                {isAll ? "all tools" : sel.length === 0 ? "none — pick tools"
                  : <>
                      <span className="font-mono">{sel.slice(0, 3).join(", ")}</span>
                      {sel.length > 3 && <span> +{sel.length - 3} more</span>}
                    </>}
              </span>
              <span className="ml-auto text-[11px] text-primary">choose tools</span>
            </button>
            {open[s.id] && (
              <div className="border-t border-pressed px-3 py-2">
                {loading[s.id] && <p className="text-[11px] text-mute">Loading tools…</p>}
                {errors[s.id] && <p className="text-[11px] text-danger">Couldn’t list tools — {errors[s.id]}</p>}
                {list && list.length === 0 && <p className="text-[11px] text-mute">This server advertises no tools.</p>}
                {list && list.length > 0 && (() => {
                  const q = (query[s.id] || "").trim().toLowerCase();
                  const isBlocked = (name: string) => ceiling.length > 0 && !ceiling.includes(name);
                  const isChecked = (name: string) => !isBlocked(name) && (isAll || sel.includes(name));
                  const toggleOne = (name: string) => {
                    if (isBlocked(name)) return;
                    const base = isAll ? eligible : sel;
                    const nextNames = isChecked(name)
                      ? base.filter((n) => n !== name)
                      : [...base, name];
                    setSelection(s.id, nextNames, eligible);
                  };
                  const toggleMany = (names: string[]) => {
                    const targets = names.filter((n) => !isBlocked(n));
                    if (targets.length === 0) return;
                    const base = isAll ? eligible : sel;
                    const allOn = targets.every((n) => base.includes(n));
                    const nextNames = allOn
                      ? base.filter((n) => !targets.includes(n))
                      : [...base, ...targets.filter((n) => !base.includes(n))];
                    setSelection(s.id, nextNames, eligible);
                  };
                  const row = (t: McpTool) => {
                    const blocked = isBlocked(t.name);
                    return (
                      <label key={t.name}
                        title={blocked ? "blocked at server level" : (t.description || t.name)}
                        className={`flex items-start gap-2 text-[11px] ${blocked ? "cursor-not-allowed opacity-50" : "cursor-pointer text-body"}`}>
                        <input type="checkbox" className="mt-0.5" checked={isChecked(t.name)} disabled={blocked}
                          onChange={() => toggleOne(t.name)} />
                        <span className="min-w-0">
                          <span className="font-mono text-body">{t.name}</span>
                          {blocked && <span className="ml-1 text-mute">(blocked at server level)</span>}
                        </span>
                      </label>
                    );
                  };
                  // Group by the name's first `_` segment (apollo_, dms_, wms_…).
                  // Grouping only earns its keep on big flat lists.
                  const groups = new Map<string, McpTool[]>();
                  for (const t of list) {
                    const p = t.name.split("_")[0] || t.name;
                    groups.set(p, [...(groups.get(p) || []), t]);
                  }
                  const grouped = !q && list.length > 12 && groups.size > 1;
                  const filtered = q ? list.filter((t) => t.name.toLowerCase().includes(q)) : list;
                  const selectedCount = isAll ? eligible.length : sel.length;
                  return (
                    <div className="space-y-1.5">
                      <div className="flex items-center gap-3 pb-1">
                        <label className="flex items-center gap-2 text-[11px] font-medium text-body">
                          <input type="checkbox" checked={isAll}
                            ref={(el) => { if (el) el.indeterminate = !isAll && sel.length > 0; }}
                            onChange={() => (isAll ? setNone(s.id) : setAll(s.id))} />
                          <span>All tools</span>
                          <span className="text-mute">— future tools auto-included; uncheck to start from none</span>
                        </label>
                        <span className="ml-auto text-[11px] text-mute">{selectedCount}/{eligible.length} selected</span>
                      </div>
                      <input value={query[s.id] || ""}
                        onChange={(e) => setQuery((m) => ({ ...m, [s.id]: e.target.value }))}
                        placeholder={`Search ${list.length} tools…`}
                        className="w-full rounded-lg border border-pressed bg-canvas px-2 py-1 text-[11px] text-ink outline-none placeholder:text-mute focus:border-primary" />
                      <div className="max-h-64 space-y-1 overflow-y-auto pr-1">
                        {q && filtered.length === 0 && (
                          <p className="text-[11px] text-mute">No tool matches “{query[s.id]}”.</p>
                        )}
                        {!grouped && filtered.map(row)}
                        {grouped && [...groups.entries()].map(([prefix, members]) => {
                          const key = `${s.id}:${prefix}`;
                          const eligibleMembers = members.filter((t) => !isBlocked(t.name));
                          const onCount = eligibleMembers.filter((t) => isChecked(t.name)).length;
                          const allOn = eligibleMembers.length > 0 && onCount === eligibleMembers.length;
                          return (
                            <div key={key} className="rounded-lg border border-line">
                              <div className="flex w-full items-center gap-2 px-2 py-1.5">
                                <input type="checkbox" checked={allOn} disabled={eligibleMembers.length === 0}
                                  ref={(el) => { if (el) el.indeterminate = onCount > 0 && !allOn; }}
                                  onChange={() => toggleMany(members.map((t) => t.name))} />
                                <button type="button" onClick={() => setGroupOpen((m) => ({ ...m, [key]: !m[key] }))}
                                  className="flex min-w-0 flex-1 items-center gap-2 text-left">
                                  <span aria-hidden className={`text-[10px] text-mute transition-transform ${groupOpen[key] ? "rotate-90" : ""}`}>▸</span>
                                  <span className="font-mono text-[11px] text-body">{prefix}_*</span>
                                  <span className="text-[10px] text-mute">{members.length} tool{members.length > 1 ? "s" : ""}</span>
                                  <span className="ml-auto text-[10px] text-mute">{onCount} on</span>
                                </button>
                              </div>
                              {groupOpen[key] && (
                                <div className="space-y-1 border-t border-line px-2 py-1.5 pl-7">
                                  {members.map(row)}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })()}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// State pill for a harness capability: powered (ink), always-on, or off (outline).
function CapBadge({ on, always, onLabel = "On", offLabel = "Off" }: {
  on?: boolean; always?: boolean; onLabel?: string; offLabel?: string;
}) {
  if (always || on) {
    return <span className="rounded-full bg-primary px-2 py-0.5 text-[10px] font-semibold text-white">{always ? "Always on" : onLabel}</span>;
  }
  return <span className="rounded-full border border-pressed bg-canvas px-2 py-0.5 text-[10px] font-medium text-mute">{offLabel}</span>;
}

interface Memory { id: string; content: string; tags: string; updatedAt: string }

const MEMORY_PAGE_SIZE = 15;   // notes shown per page in the memory list

function MemoryCard({ agentId }: { agentId: string }) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    try {
      // Pull a generous window so search + paging run fully client-side (instant,
      // no round-trip per keystroke). `total` still reflects the true store count.
      const r = await apiJson<{ memories: Memory[]; total: number }>(
        `/api/agents/${encodeURIComponent(agentId)}/memory?limit=2000`);
      setMemories(r.memories); setTotal(r.total);
    } catch { /* */ }
  }, [agentId]);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    const content = draft.trim();
    if (!content) return;
    setBusy(true);
    try { await apiPost(`/api/agents/${encodeURIComponent(agentId)}/memory`, { content }); setDraft(""); await load(); }
    catch { /* */ } finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await apiJson(`/api/agents/${encodeURIComponent(agentId)}/memory/${encodeURIComponent(id)}`, { method: "DELETE" }); await load(); }
    catch { /* */ }
  };

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () => (q ? memories.filter((m) => `${m.content} ${m.tags}`.toLowerCase().includes(q)) : memories),
    [memories, q],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / MEMORY_PAGE_SIZE));
  useEffect(() => { setPage(1); }, [q]);                                  // new search → first page
  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]); // clamp
  const start = (page - 1) * MEMORY_PAGE_SIZE;
  const pageItems = filtered.slice(start, start + MEMORY_PAGE_SIZE);

  return (
    <div className="rounded-xl border border-line bg-softer p-3">
      <div className="flex items-center gap-2">
        <input
          className={`${inputCls} flex-1`} value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
          placeholder="Add a memory the agent should keep…" />
        <button onClick={add} disabled={busy || !draft.trim()}
          className="shrink-0 rounded-xl bg-primary px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-primary-deep disabled:opacity-40">Remember</button>
      </div>
      {memories.length === 0 ? (
        <p className="mt-2 text-xs text-mute">Nothing remembered yet. The agent adds notes as it learns about you.</p>
      ) : (
        <div className="mt-3 space-y-2">
          <SearchBox value={query} onChange={setQuery} placeholder="Search memory…"
            count={filtered.length} total={memories.length} />
          {filtered.length === 0 ? (
            <p className="px-2 text-xs text-mute">No memories match “{query.trim()}”.</p>
          ) : (
            <div className="space-y-1">
              {pageItems.map((m) => (
                <MemoryRow key={m.id} m={m} onRemove={() => remove(m.id)} />
              ))}
            </div>
          )}
          {totalPages > 1 && <Pager page={page} totalPages={totalPages} setPage={setPage} />}
          {total > memories.length && (
            <p className="px-2 pt-1 text-[10px] text-mute">Showing the {memories.length} most recent of {total} note(s).</p>
          )}
        </div>
      )}
    </div>
  );
}

const MEMORY_PREVIEW_CHARS = 50;   // preview length in the list; full text is in the modal

// One memory row: a short preview in the list (a note can be very long), with a
// "See more" that opens the full text in a modal. break-words keeps an
// unbreakable token (long id/hash) from overflowing.
function MemoryRow({ m, onRemove }: { m: Memory; onRemove: () => void }) {
  const [showFull, setShowFull] = useState(false);
  const isLong = (m.content?.length ?? 0) > MEMORY_PREVIEW_CHARS;
  const preview = isLong ? m.content.slice(0, MEMORY_PREVIEW_CHARS).trimEnd() + "…" : m.content;
  return (
    <>
      <div className="group flex items-start gap-2 rounded-lg px-2 py-1 hover:bg-soft">
        <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-mute" />
        <span className="min-w-0 flex-1 break-words text-xs text-body">
          {preview}
          {m.tags && <span className="ml-1.5 font-mono text-[10px] text-mute">[{m.tags}]</span>}
          {isLong && (
            <button onClick={() => setShowFull(true)}
              className="ml-1.5 align-baseline text-[10px] font-medium text-primary hover:underline">
              See more
            </button>
          )}
        </span>
        <button onClick={onRemove} title="Forget"
          className="shrink-0 text-mute opacity-0 hover:text-ink group-hover:opacity-100">✕</button>
      </div>
      {showFull && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-ink/30 p-6" onClick={() => setShowFull(false)}>
          <div className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-line bg-canvas p-5 shadow-[0_24px_60px_-30px_rgba(40,51,92,0.55)]" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center gap-2">
              <h3 className="font-display text-sm font-bold text-ink">Memory</h3>
              {m.tags && <span className="font-mono text-[10px] text-mute">[{m.tags}]</span>}
              <button onClick={() => setShowFull(false)} className="ml-auto text-mute hover:text-ink">✕</button>
            </div>
            <p className="whitespace-pre-wrap break-words rounded-xl bg-panel p-3 text-[12px] leading-relaxed text-panel-ink">{m.content}</p>
          </div>
        </div>
      )}
    </>
  );
}

// Per-agent model source: the local active model (default), or an external paid
// provider via BYOK (your own API key) or a subscription token. Credentials are
// stored once under Providers and reused across agents.
const MODEL_MODES: { id: ModelSource; label: string; hint: string }[] = [
  { id: "local", label: "Local", hint: "Use the machine's active model (default)." },
  { id: "byok", label: "BYOK", hint: "Bring your own API key (e.g. DeepSeek)." },
  { id: "subscription", label: "Subscription", hint: "Use a provider login token (ChatGPT / Claude)." },
];

function ModelCard({ draft, patch, providers, reloadProviders }: {
  draft: Agent;
  patch: (p: Partial<Agent>) => void;
  providers: ProviderInfo[];
  reloadProviders: () => void;
}) {
  const toast = useToast();
  const source: ModelSource = draft.modelSource || "local";
  const [secret, setSecret] = useState("");
  const [savingCred, setSavingCred] = useState(false);
  const [credMsg, setCredMsg] = useState<string | null>(null);
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [customApiType, setCustomApiType] = useState<"chat_completions" | "responses">("chat_completions");
  const [customAuthMode, setCustomAuthMode] = useState<"bearer" | "none">("bearer");
  const [customReasoningEffort, setCustomReasoningEffort] = useState(false);
  const [reasoningKeyInput, setReasoningKeyInput] = useState("");
  const [clearReasoningKey, setClearReasoningKey] = useState(false);
  const [customModels, setCustomModels] = useState<string[]>([]);
  const [loadingCustomModels, setLoadingCustomModels] = useState(false);

  const eligible = providers.filter((p) =>
    source === "byok" ? p.byok : source === "subscription" ? p.subscription : false);
  const current = providers.find((p) => p.id === draft.provider) || eligible[0];
  const hasCred = source === "subscription" ? !!current?.hasToken : !!current?.hasKey;
  const customAnthropic = current?.id === "opencode-go"
    && !!(current.caching?.[draft.model || ""] || current.caching?.["*"])
    && current.modelKinds?.[draft.model || ""] === "anthropic";
  const extendedCacheAvailable = source === "byok"
    && (current?.id === "anthropic" || customAnthropic);
  // The two custom gateways get a per-model "Caching" control table; official
  // providers (anthropic/openai/deepseek) don't — see docs/prd/PRD-24-prompt-caching.md.
  const isCustomGateway = !!current?.custom || current?.id === "opencode-go";
  const canDiscoverModels = !!current?.modelDiscovery
    && !(source === "subscription" && current?.oauthLogin);
  const modelsFor = (provider: ProviderInfo | undefined, mode = source) =>
    mode === "subscription" && provider?.subscriptionModels.length
      ? provider.subscriptionModels : provider?.models || [];
  const currentModels = modelsFor(current);

  useEffect(() => {
    if (!current?.custom) return;
    setCustomBaseUrl(current.baseUrlOverride || current.baseUrl || "");
    setCustomApiType(current.apiType === "responses" ? "responses" : "chat_completions");
    setCustomAuthMode(current.authMode === "none" ? "none" : "bearer");
    setCustomReasoningEffort(!!current.reasoningEffort);
    setReasoningKeyInput("");
    setClearReasoningKey(false);
  }, [current?.id, current?.baseUrl, current?.baseUrlOverride, current?.apiType, current?.authMode, current?.reasoningEffort, current?.custom]);

  const pickMode = (m: ModelSource) => {
    setSecret(""); setCredMsg(null);
    if (m === "local") { patch({ modelSource: "local", provider: "", model: null }); return; }
    const list = providers.filter((p) => (m === "byok" ? p.byok : p.subscription));
    const prov = draft.provider && list.some((p) => p.id === draft.provider)
      ? draft.provider : (list[0]?.id || "");
    const info = list.find((p) => p.id === prov);
    patch({ modelSource: m, provider: prov, model: modelsFor(info, m)[0] || draft.model || "" });
  };

  const pickProvider = (pid: string) => {
    setSecret(""); setCredMsg(null);
    const info = providers.find((p) => p.id === pid);
    patch({ provider: pid, model: modelsFor(info)[0] || "" });
  };

  const loadCustomModels = useCallback(async () => {
    if (!canDiscoverModels) return;
    setLoadingCustomModels(true);
    try {
      const result = await apiJson<{ models: string[] }>(
        `/api/providers/${encodeURIComponent(current.id)}/models`);
      const models = result.models || [];
      setCustomModels(models);
      setCredMsg(models.length ? null : "No models returned by this gateway.");
    } catch (e) {
      setCustomModels([]);
      setCredMsg(`Could not load models: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoadingCustomModels(false);
    }
  }, [canDiscoverModels, current?.id]);

  useEffect(() => {
    setCustomModels([]);
    if (canDiscoverModels && (!current.custom || current.baseUrlOverride)) loadCustomModels();
  }, [canDiscoverModels, current?.custom, current?.id, current?.baseUrlOverride, loadCustomModels]);

  const saveCred = async () => {
    if (!current) return;
    if (current.custom && !customBaseUrl.trim()) return;
    if (!current.custom && !secret.trim()) return;
    setSavingCred(true); setCredMsg(null);
    try {
      const body: Record<string, string | boolean> = {};
      if (current.custom) {
        body.baseUrl = customBaseUrl.trim();
        body.apiType = customApiType;
        body.authMode = customAuthMode;
        body.reasoningEffort = customReasoningEffort;
        if (reasoningKeyInput.trim()) body.reasoningKey = reasoningKeyInput.trim();
        else if (clearReasoningKey) body.reasoningKey = "";
        if (customAuthMode === "none") body.apiKey = "";
        else if (secret.trim()) body.apiKey = secret.trim();
      } else {
        const field = source === "subscription" ? "token" : "apiKey";
        body[field] = secret.trim();
      }
      await apiJson(`/api/providers/${encodeURIComponent(current.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setSecret(""); setReasoningKeyInput(""); setClearReasoningKey(false);
      setCredMsg("Saved."); reloadProviders();
      if (canDiscoverModels) loadCustomModels();
      toast.success(current.custom
        ? `${current.label} configuration saved`
        : `${current.label} ${source === "subscription" ? "token" : "key"} saved`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setCredMsg(msg);
      toast.error(`Save failed — ${msg}`);
    }
    finally { setSavingCred(false); }
  };

  return (
    <Card title="Model" hint="Where this agent's model runs. Local-first by default; switch to a paid provider per agent.">
      <div className="space-y-4">
        <div className="flex rounded-xl border border-pressed p-0.5">
          {MODEL_MODES.map((m) => (
            <button key={m.id} type="button" title={m.hint} onClick={() => pickMode(m.id)}
              className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                source === m.id ? "bg-primary text-white" : "text-body hover:text-ink"}`}>
              {m.label}
            </button>
          ))}
        </div>

        {source === "local" ? (
          <p className="text-xs text-mute">
            Runs against whatever model is active (switch it in the Models tab). Nothing leaves this machine.
          </p>
        ) : eligible.length === 0 ? (
          <p className="text-xs text-mute">No providers support this mode yet.</p>
        ) : (
          <>
            <Field label="Provider">
              <select className={inputCls} value={current?.id || ""}
                onChange={(e) => pickProvider(e.target.value)}>
                {eligible.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </Field>
            {!current?.custom && (
              <Field label="Model" hint="The provider's model id.">
                <div className="flex gap-2">
                  <input className={`${inputCls} flex-1`} list={`models-${current?.id}`} value={draft.model || ""}
                    onChange={(e) => patch({ model: e.target.value })}
                    placeholder={loadingCustomModels ? "Loading models…" : currentModels[0] || "model id"} />
                  <datalist id={`models-${current?.id}`}>
                    {(canDiscoverModels && customModels.length ? customModels : currentModels)
                      .map((m) => <option key={m} value={m} />)}
                  </datalist>
                  {canDiscoverModels && (
                    <button type="button" onClick={loadCustomModels}
                      disabled={loadingCustomModels || !hasCred}
                      title={hasCred ? "Refresh models from OpenCode Go" : "Save a Go API key first"}
                      className="shrink-0 rounded-xl border border-pressed px-3 py-2 text-xs font-semibold text-body hover:border-ink hover:text-ink disabled:opacity-40">
                      {loadingCustomModels ? "Loading…" : "Refresh models"}
                    </button>
                  )}
                </div>
                {canDiscoverModels && <p className="mt-1.5 text-[11px] text-mute">Refresh lists the models available to this API key; the selected model is this assistant’s default.</p>}
                {source === "subscription" && current?.oauthLogin && <p className="mt-1.5 text-[11px] text-mute">Curated Codex subscription catalog (Luna is the default for new agents). Account-specific availability is not exposed by a documented API, so you can still enter another Codex model ID.</p>}
              </Field>
            )}
            {extendedCacheAvailable && (
              <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-pressed bg-canvas px-3 py-2.5">
                <input type="checkbox" className="mt-0.5 accent-primary"
                  checked={draft.cacheTtl === "1h"}
                  onChange={(e) => patch({ cacheTtl: e.target.checked ? "1h" : "" })} />
                <span>
                  <span className="block text-xs font-medium text-ink">Extended cache (1h)</span>
                  <span className="mt-0.5 block text-[11px] text-mute">Costs 2× on cache writes but reads are 0.1×; useful only for schedules with 5-minute–1-hour gaps.</span>
                </span>
              </label>
            )}
            {source === "subscription" && current?.oauthLogin ? (
              // Only ChatGPT uses subscription OAuth; the Claude subscription
              // path is deliberately disabled (see backend gateway/providers.py
              // OAUTH_SUBSCRIPTION), so oauthLogin is never true for anthropic.
              <ChatGptLogin provider={current} reloadProviders={reloadProviders} />
            ) : current?.custom ? (
              <div className="space-y-3">
                <Field label="Base URL" hint="Include /v1 when your gateway uses it. A full endpoint URL is also accepted.">
                  <input className={`${inputCls} font-mono text-xs`} value={customBaseUrl}
                    onChange={(e) => setCustomBaseUrl(e.target.value)}
                    placeholder="http://127.0.0.1:8000/v1" />
                </Field>
                <Field label="API type" hint="Choose the wire protocol exposed by your gateway.">
                  <select className={inputCls} value={customApiType}
                    onChange={(e) => setCustomApiType(e.target.value as "chat_completions" | "responses")}>
                    <option value="chat_completions">Chat Completions</option>
                    <option value="responses">Responses API</option>
                  </select>
                </Field>
                <Field label="Authentication">
                  <select className={inputCls} value={customAuthMode}
                    onChange={(e) => {
                      const mode = e.target.value as "bearer" | "none";
                      setCustomAuthMode(mode);
                      if (mode === "none") setSecret("");
                    }}>
                    <option value="bearer">Bearer API key</option>
                    <option value="none">No authentication</option>
                  </select>
                </Field>
                <label className="flex items-center gap-2 text-xs text-body">
                  <input type="checkbox" checked={customReasoningEffort}
                    onChange={(e) => setCustomReasoningEffort(e.target.checked)} />
                  <span className="inline-flex items-center gap-1">
                    Forward reasoning effort
                    <HintIcon label="Forward reasoning effort"
                      hint={"For models without a managed Thinking style below: only forward OpenAI's reasoning_effort param if this gateway honors it. Models with a Thinking style send effort in that dialect instead."} />
                  </span>
                </label>
                {customAuthMode === "bearer" && (
                  <Field label="API key (optional to replace)"
                    hint="Stored locally, shared across agents, and never shown again.">
                    <input className={`${inputCls} flex-1 font-mono text-xs`} type="password"
                      value={secret} onChange={(e) => setSecret(e.target.value)}
                      placeholder={hasCred ? "•••••••• (saved — paste to replace)" : "Paste bearer key"} />
                  </Field>
                )}
                <button type="button" onClick={saveCred}
                  disabled={savingCred || !customBaseUrl.trim() || (customAuthMode === "bearer" && !hasCred && !secret.trim())}
                  className="rounded-xl border border-pressed px-3 py-2 text-xs font-semibold text-body hover:border-ink hover:text-ink disabled:opacity-40">
                  {savingCred ? "Saving…" : "Save provider configuration"}
                </button>
                <Field label="Default model" hint="Choose this assistant's saved default; individual chats can override it.">
                  <div className="flex gap-2">
                    <input className={`${inputCls} flex-1`} list="custom-gateway-models" value={draft.model || ""}
                      onChange={(e) => patch({ model: e.target.value })}
                      placeholder={loadingCustomModels ? "Loading models…" : "Refresh models or enter a model id"} />
                    <datalist id="custom-gateway-models">
                      {customModels.map((model) => <option key={model} value={model} />)}
                    </datalist>
                    <button type="button" onClick={loadCustomModels}
                      disabled={loadingCustomModels || !current.baseUrlOverride}
                      title={current.baseUrlOverride ? "Refresh models from this gateway" : "Save the provider configuration first"}
                      className="shrink-0 rounded-xl border border-pressed px-3 py-2 text-xs font-semibold text-body hover:border-ink hover:text-ink disabled:opacity-40">
                      {loadingCustomModels ? "Loading…" : "Refresh models"}
                    </button>
                  </div>
                </Field>
                <Field label="Reasoning encryption key (optional)"
                  hint="Encrypts stored reasoning for the Replay capability below. Non-OpenAI models (DeepSeek/Kimi) emit plaintext reasoning — without a key it isn't stored, so replay has nothing to re-send. Encrypted at rest in the local DB; protects a leaked database file, not theft of the whole data directory.">
                  <div className="space-y-1.5 text-xs text-body">
                    <div className="flex gap-2">
                      <input className={`${inputCls} flex-1 font-mono text-xs`} type="password"
                        value={reasoningKeyInput}
                        onChange={(e) => { setReasoningKeyInput(e.target.value); setClearReasoningKey(false); }}
                        placeholder="Paste or generate a key" />
                      <button type="button"
                        onClick={() => {
                          setReasoningKeyInput(btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))));
                          setClearReasoningKey(false);
                        }}
                        className="rounded-xl border border-pressed px-3 py-2 text-xs font-semibold text-body hover:border-ink hover:text-ink">
                        Generate
                      </button>
                    </div>
                    <div className="text-mute">
                      {current.hasReasoningKey
                        ? <span className="text-success">Key set — encrypted reasoning enabled</span>
                        : "No key — non-OpenAI reasoning won't be stored"}
                      {reasoningKeyInput.trim() && (
                        <button type="button"
                          onClick={() => { setReasoningKeyInput(""); setClearReasoningKey(true); }}
                          className="ml-2 underline hover:text-ink">
                          Clear key
                        </button>
                      )}
                    </div>
                  </div>
                </Field>
                <CapabilitiesTable current={current} customModels={customModels} reloadProviders={reloadProviders} />
                <div className="text-[11px]">
                  {customAuthMode === "none"
                    ? <span className="text-warn">No Authorization header will be sent. Use this only with a gateway you trust.</span>
                    : hasCred
                      ? <span className="text-success">✓ Bearer key configured</span>
                      : <span className="text-warn">A bearer key is required for this authentication mode.</span>}
                  {credMsg && <span className="text-mute"> · {credMsg}</span>}
                </div>
              </div>
            ) : (
              <Field
                label={source === "subscription" ? "Access token" : current?.credentialLabel || "API key"}
                hint={source === "subscription"
                  ? "Your provider login/access token. Stored locally, shared across agents, never shown again."
                  : current?.credentialHint || "Your provider API key. Stored locally, shared across agents, never shown again."}>
                <div className="flex gap-2">
                  <input className={`${inputCls} flex-1 font-mono text-xs`} type="password"
                    value={secret} onChange={(e) => setSecret(e.target.value)}
                    placeholder={hasCred ? "•••••••• (saved — paste to replace)" : `Paste ${current?.label} ${source === "subscription" ? "token" : "key"}`} />
                  <button type="button" onClick={saveCred} disabled={savingCred || !secret.trim()}
                    className="shrink-0 rounded-xl border border-pressed px-3 py-2 text-xs font-semibold text-body hover:border-ink hover:text-ink disabled:opacity-40">
                    {savingCred ? "Saving…" : "Save"}
                  </button>
                </div>
                <div className="mt-1.5 flex items-center gap-2 text-[11px]">
                  {hasCred
                    ? <span className="text-success">✓ {source === "subscription" ? "Token" : current?.credentialLabel || "Key"} configured for {current?.label}</span>
                    : <span className="text-warn">No {source === "subscription" ? "token" : current?.credentialLabel || "key"} yet — this agent will error until one is saved.</span>}
                  {credMsg && <span className="text-mute">· {credMsg}</span>}
                </div>
              </Field>
            )}
            {/* current.custom already rendered its own CapabilitiesTable above; this
                covers the other custom gateway (opencode-go), which is byok
                and falls into the standard credential Field branch. */}
            {isCustomGateway && !current?.custom && current && (
              <CapabilitiesTable current={current} customModels={customModels} reloadProviders={reloadProviders} />
            )}
            <p className="text-[11px] text-mute">
              Heads up: this agent will send prompts to {current?.label} — calls leave your machine.
            </p>
          </>
        )}
      </div>
    </Card>
  );
}

// Per-model capability control table for the two custom gateways
// (openai-compatible, opencode-go) — caching (PRD-24), thinking dialect,
// vision, and reasoning replay (PRD-25). One row spans the discovered-model
// list plus a "*" default row. Only the default row, customized rows, and
// rows with cache telemetry render by default; the rest sit behind a
// "show more" toggle with a filter when the list is long. Column headers
// carry hover tooltips — no hint wall.
const CAPABILITY_HINTS = {
  caching: "Enable only if this gateway supports prompt caching. Virtual Worker picks the mechanism that fits each model's protocol; watch Last seen to confirm.",
  thinking: "Chat Completions only: choose how this model receives the agent's Reasoning-effort setting. OpenAI sends top-level reasoning_effort when forwarding is enabled; OpenRouter and Z.ai use their native dialects. Responses API always uses native reasoning.effort and ignores this selection.",
  vision: "Enable only for models that accept images. Images sent to the agent are forwarded as multimodal input; otherwise saved to the working folder, not shown to the model.",
  replay: "Re-sends captured reasoning on tool-call continuations so the model keeps its train of thought. Extra prompt tokens; storing non-OpenAI reasoning needs the encryption key above.",
} as const;

// Shared hover/focus hint bubble — a small ⓘ with a tooltip that opens on
// hover or keyboard focus. Replaces native title= tooltips (slow, unstyled).
function HintIcon({ hint, label }: { hint: string; label?: string }) {
  return (
    <span className="relative inline-flex group/hint">
      <button type="button" tabIndex={0}
        aria-label={label ? `${label}: ${hint}` : hint}
        className="inline-flex cursor-help items-center text-mute outline-none hover:text-ink focus-visible:text-ink">
        ⓘ
      </button>
      <span role="tooltip"
        className="invisible absolute bottom-full left-1/2 z-30 mb-1.5 w-52 -translate-x-1/2 rounded-lg bg-ink px-2 py-1.5 text-[11px] font-normal normal-case leading-snug tracking-normal text-white opacity-0 shadow-lg transition-opacity group-hover/hint:visible group-hover/hint:opacity-100 group-focus-within/hint:visible group-focus-within/hint:opacity-100 pointer-events-none">
        {hint}
      </span>
    </span>
  );
}

function HeaderInfo({ label, hint }: { label: string; hint: string }) {
  return (
    <span className="inline-flex items-center">
      {label}
      <span className="ml-0.5"><HintIcon label={label} hint={hint} /></span>
    </span>
  );
}

function CapabilitiesTable({ current, customModels, reloadProviders }: {
  current: ProviderInfo;
  customModels: string[];
  reloadProviders: () => void;
}) {
  const toast = useToast();
  const [saving, setSaving] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [filter, setFilter] = useState("");
  // Replay is custom-gateway only (set_record rejects it for opencode-go;
  // replay_reasoning_for returns false for non-custom providers).
  const replaySupported = !!current.custom;
  const replayOn = (model: string) =>
    current.replayReasoning?.[model] ?? current.replayReasoning?.["*"] === true;

  const rows = useMemo(() => {
    // Discovery list plus any model id already present in a capability map or
    // the telemetry — a stale/configured model must stay editable even when
    // it disappears from the gateway's /models response.
    const ids = new Set(customModels);
    for (const map of [current.caching, current.vision, current.thinkingFormat,
                       current.replayReasoning, current.cacheSeen]) {
      for (const key of Object.keys(map || {})) {
        if (key !== "*") ids.add(key);
      }
    }
    return ["*", ...[...ids].sort()];
  }, [customModels, current.caching, current.vision, current.thinkingFormat,
      current.replayReasoning, current.cacheSeen]);
  const hasOverride = (model: string) =>
    model === "*" || current.caching?.[model] !== undefined || current.vision?.[model] !== undefined
      || !!current.thinkingFormat?.[model] || current.replayReasoning?.[model] !== undefined;
  const visible = showAll
    ? rows.filter((model) => model === "*" || !filter.trim()
        || model.toLowerCase().includes(filter.trim().toLowerCase()))
    : rows.filter((model) => hasOverride(model) || current.cacheSeen?.[model]);
  const hiddenCount = rows.length - (showAll
    ? visible.filter((model) => model !== "*").length
    : visible.length);
  const customizedCount = rows.filter((m) => m !== "*" && hasOverride(m)).length;

  const put = async (field: string, model: string, value: string | boolean | null, fail: string) => {
    setSaving(`${field}:${model}`);
    setMsg(null);
    try {
      await apiJson(`/api/providers/${encodeURIComponent(current.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: { [model]: value } }),
      });
      reloadProviders();
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : String(e);
      setMsg(errMsg);
      toast.error(`${fail} — ${errMsg}`);
    } finally {
      setSaving(null);
    }
  };

  // Tri-state capability control: the "*" default row is On/Off; other rows
  // get Inherit (no row) / On / Off so a model can be forced off even when
  // the default row is on (and vice versa).
  const capControl = (field: "caching" | "vision" | "replayReasoning", model: string,
                      map: Record<string, boolean> | undefined, label: string, fail: string) => {
    const busy = saving === `${field}:${model}`;
    const isDefault = model === "*";
    const v = map?.[model];
    const title = isDefault ? "all models (default)" : model;
    return (
      <select
        className={`w-[5.75rem] rounded-lg border border-pressed bg-canvas px-1.5 py-1 text-[11px] outline-none focus:border-primary disabled:opacity-40 ${!isDefault && v === undefined ? "text-mute" : "text-ink"}`}
        value={v === true ? "on" : v === false || isDefault ? "off" : ""}
        disabled={busy}
        aria-label={`${label} for ${title}`}
        onChange={(e) => {
          const val = e.target.value;
          put(field, model, val === "" ? null : val === "on", fail);
        }}>
        {!isDefault && <option value="">Inherit</option>}
        <option value="on">On</option>
        <option value="off">Off</option>
      </select>
    );
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-xs font-medium text-body">Model capabilities</p>
        {customizedCount > 0 && (
          <span className="text-[11px] text-mute">{customizedCount} customized</span>
        )}
      </div>
      <p className="text-[11px] text-mute">
        Per-model opt-ins — "{current.label}" doesn't guarantee any of them, unlike
        the official providers where this is decided for you. A model row set to On
        or Off overrides the default row; Inherit follows it. Hover a column name
        for what it does.
      </p>
      <div className="rounded-xl border border-pressed">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-pressed text-[11px] text-mute">
              <th className="px-3 py-1.5 font-medium">Model</th>
              <th className="px-2 py-1.5 font-medium">
                <HeaderInfo label="Prompt caching" hint={CAPABILITY_HINTS.caching} />
              </th>
              <th className="px-2 py-1.5 font-medium">
                <HeaderInfo label="Thinking" hint={CAPABILITY_HINTS.thinking} />
              </th>
              <th className="px-2 py-1.5 font-medium">
                <HeaderInfo label="Vision" hint={CAPABILITY_HINTS.vision} />
              </th>
              {replaySupported && (
                <th className="px-2 py-1.5 font-medium">
                  <HeaderInfo label="Replay" hint={CAPABILITY_HINTS.replay} />
                </th>
              )}
              <th className="px-2 py-1.5 font-medium">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((model) => {
              const isDefault = model === "*";
              const seen = current.cacheSeen?.[model];
              const title = isDefault ? "All models (default) *" : model;
              const thinkingFormat = current.thinkingFormat?.[model]
                ?? (isDefault ? "openai" : "");
              return (
                <tr key={model} className="border-b border-pressed text-body last:border-b-0">
                  <td className="max-w-[220px] truncate px-3 py-1.5 font-mono text-[11px]" title={isDefault ? undefined : model}>
                    {title}
                  </td>
                  <td className="px-2 py-1.5">
                    {capControl("caching", model, current.caching, "Prompt caching", "Caching setting failed")}
                  </td>
                  <td className="px-2 py-1.5">
                    <select
                      className={`w-[5.75rem] rounded-lg border border-pressed bg-canvas px-1.5 py-1 text-[11px] outline-none focus:border-primary disabled:opacity-40 ${!isDefault && !current.thinkingFormat?.[model] ? "text-mute" : "text-ink"}`}
                      value={thinkingFormat}
                      disabled={saving === `thinkingFormat:${model}`}
                      aria-label={`Thinking format for ${title}`}
                      onChange={(e) => put("thinkingFormat", model, e.target.value || null, "Thinking setting failed")}>
                      {!isDefault && <option value="">Inherit</option>}
                      <option value="openai">OpenAI</option>
                      <option value="openrouter">OpenRouter</option>
                      <option value="zai">Z.ai</option>
                    </select>
                  </td>
                  <td className="px-2 py-1.5">
                    {capControl("vision", model, current.vision, "Vision input", "Vision setting failed")}
                  </td>
                  {replaySupported && (
                    <td className="px-2 py-1.5">
                      <span className="relative inline-flex items-center gap-0.5 group">
                        {capControl("replayReasoning", model, current.replayReasoning, "Replay reasoning", "Replay setting failed")}
                        {replayOn(model) && !current.hasReasoningKey && (
                          <>
                            <span className="text-warn text-[11px] leading-none">⚠</span>
                            <span className="invisible absolute bottom-full left-1/2 z-30 mb-1 w-52 -translate-x-1/2 rounded-lg bg-ink px-2 py-1.5 text-[11px] leading-snug text-white opacity-0 shadow-lg transition-opacity group-hover:visible group-hover:opacity-100 pointer-events-none">
                              This provider has no encryption key — non-OpenAI plaintext
                              reasoning won't be stored, so replay is limited until one is set above.
                            </span>
                          </>
                        )}
                      </span>
                    </td>
                  )}
                  <td className="px-2 py-1.5 text-[11px] text-mute">
                    {seen ? `read ${seen.cacheRead} · write ${seen.cacheWrite}` : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {showAll && (
        <input className="rounded-lg border border-pressed bg-canvas px-2 py-1 text-[11px] text-ink outline-none focus:border-primary"
          value={filter} onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter models…" aria-label="Filter capability table models" />
      )}
      {hiddenCount > 0 && (
        <button type="button" onClick={() => setShowAll(!showAll)}
          className="text-[11px] font-medium text-mute underline hover:text-ink">
          {showAll ? "Hide" : `▸ Show ${hiddenCount} more model${hiddenCount === 1 ? "" : "s"}`}
        </button>
      )}
      {msg && <p className="text-[11px] text-warn">{msg}</p>}
    </div>
  );
}

// ChatGPT subscription sign-in (OAuth/PKCE). Opens the OpenAI authorize URL and
// polls the backend until the loopback callback on :1455 completes the login.
function ChatGptLogin({ provider, reloadProviders }: {
  provider: ProviderInfo;
  reloadProviders: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>(provider.hasOauth ? "connected" : "idle");
  const [account, setAccount] = useState(provider.account);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const connected = status === "connected" || provider.hasOauth;

  const signIn = async () => {
    setBusy(true); setError(null);
    try {
      const { authUrl } = await apiPost<{ authUrl: string }>("/api/providers/openai/oauth/start", {});
      window.open(authUrl, "_blank", "noopener");
      setStatus("awaiting");
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const s = await apiJson<{ status: string; account: string; error: string | null }>(
            "/api/providers/openai/oauth/status");
          setStatus(s.status); setAccount(s.account || "");
          if (s.status === "connected" || s.status === "error") {
            if (pollRef.current) clearInterval(pollRef.current);
            if (s.status === "error") setError(s.error || "Sign-in failed.");
            reloadProviders();
          }
        } catch { /* keep polling */ }
      }, 1500);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const disconnect = async () => {
    try {
      await apiPost("/api/providers/openai/oauth/disconnect", {});
      setStatus("idle"); setAccount(""); reloadProviders();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <Field label="ChatGPT account" hint="Sign in with your ChatGPT Plus/Pro subscription — no API key. Uses the Codex backend; only a refresh token is stored locally.">
      {connected ? (
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-success">✓ Signed in{account ? ` (${account})` : ""}</span>
          <button type="button" onClick={disconnect}
            className="ml-auto rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:border-ink hover:text-ink">
            Disconnect
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <button type="button" onClick={signIn} disabled={busy}
            className="rounded-xl bg-primary px-3 py-2 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-40">
            {busy ? "Starting…" : status === "awaiting" ? "Waiting for browser…" : "Sign in with ChatGPT"}
          </button>
          {status === "awaiting" && <span className="text-[11px] text-mute">Complete the login in the opened tab.</span>}
        </div>
      )}
      {error && <p className="mt-1.5 text-[11px] text-danger">{error}</p>}
    </Field>
  );
}

// Claude subscription sign-in (OAuth/PKCE, copy-paste). Claude shows a code the
// user pastes back (no loopback redirect for third-party apps).
function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
        {label}
        {hint && <HintIcon label={label} hint={hint} />}
      </label>
      {children}
    </div>
  );
}

// A free-text add/remove chip list (PRD-31 exclusions) — mirrors the
// allow-prefix editor in connectors-page.tsx: chip pill with a × to remove,
// a text input, Enter or "+" to add.

export default AgentsPage;
