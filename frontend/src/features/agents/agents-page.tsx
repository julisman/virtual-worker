// Agents — create local agents (persona + tools + skills) and manage skill packs.
// Local-first: everything is stored on this machine. Monochrome theme; plain React.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";
import { ActivityView, BoardView, OverviewView, ScheduleView, TriggersView } from "./work-tabs";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

export interface Agent {
  id: string;
  name: string;
  description: string;
  persona: string;
  model: string | null;
  temperature: number;
  topP: number | null;
  topK: number | null;
  workingDir: string;
  role: string;
  dutyStatus: string;
  guardrails: Guardrails;
  tools: string[];
  skills: string[];
  mcpServers: string[];
  customTools: string[];
  knowledgeBases: string[];
  updatedAt: string;
}

export interface Guardrails {
  shell: "auto" | "off" | "readonly" | "workspace";
  verify: boolean;
  maxIterations: number;
  dailyCap: number;
  extraDenied: string[];
  channelSend: boolean;
}

const DEFAULT_GUARDRAILS: Guardrails = {
  shell: "auto", verify: true, maxIterations: 6, dailyCap: 60, extraDenied: [], channelSend: false,
};

interface Skill {
  slug: string;
  name: string;
  description: string;
  whenToUse: string;
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
  enabled: boolean;
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
  allowPrivate: boolean;
  timeout: number;
  enabled: boolean;
  updatedAt: string;
}

const BUILTIN_TOOLS: { name: string; label: string; hint: string }[] = [
  { name: "web_search", label: "Web search", hint: "Search the web for up-to-date info." },
  { name: "web_fetch", label: "Web fetch", hint: "Read the contents of a web page." },
];

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

export function AgentsPage({ onChat, sub = "agents", panel }: {
  onChat?: (agentId: string) => void;
  sub?: "agents" | "skills" | "mcp" | "tools";
  panel: Omit<AgentsPanelProps, "onChat">;   // roster state lifted to the shell
}) {
  // The roster + Skills/MCP are sidebar sub-items now, so the whole top bar is
  // free for content — the panel fills the full height of the pane.
  return (
    <div className="flex h-full min-w-0 flex-col bg-base">
      {sub === "agents" ? (
        <div className="min-h-0 flex-1"><AgentsPanel onChat={onChat} {...panel} /></div>
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
  temperature: 1.0, topP: 0.95, topK: 64, workingDir: "", role: "", dutyStatus: "off",
  guardrails: { ...DEFAULT_GUARDRAILS }, tools: [], skills: [], mcpServers: [], customTools: [], knowledgeBases: [], updatedAt: "",
});

interface KnowledgeBase { id: string; name: string; description: string; chunkCount: number; sourceCount: number }

// File tools an agent gets automatically once it has a working folder.
const FS_TOOLS = ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep"];

// Roster + selection are owned by the app shell (the sidebar lists the agents),
// so the panel is a controlled detail view: it edits whichever agent is selected.
export interface AgentsPanelProps {
  onChat?: (agentId: string) => void;
  agents: Agent[];
  selectedId: string | null;
  creating: boolean;
  onChanged: () => void;                       // reload the roster in the shell
  onSelectedChange: (id: string | null) => void;
  onCreatingChange: (v: boolean) => void;
}

function AgentsPanel({
  onChat, agents, selectedId, creating, onChanged, onSelectedChange, onCreatingChange,
}: AgentsPanelProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [customTools, setCustomTools] = useState<CustomTool[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [draft, setDraft] = useState<Agent>(blankAgent());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"overview" | "board" | "activity" | "schedule" | "triggers" | "settings">("settings");

  const selected = useMemo(() => agents.find((a) => a.id === selectedId) ?? null, [agents, selectedId]);
  const isIntern = !creating && !!(selected?.role || "").trim();

  // Land on the work Overview for interns, the editor for plain agents.
  useEffect(() => {
    if (creating) setView("settings");
    else if (selected) setView((selected.role || "").trim() ? "overview" : "settings");
  }, [creating, selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  // The editor's option lists (skills + MCP servers) — loaded once.
  useEffect(() => {
    (async () => {
      try {
        const [s, m, t, k] = await Promise.all([
          apiJson<{ skills: Skill[] }>("/api/skills"),
          apiJson<{ servers: McpServer[] }>("/api/mcp"),
          apiJson<{ tools: CustomTool[] }>("/api/tools"),
          apiJson<{ knowledgeBases: KnowledgeBase[] }>("/api/kb"),
        ]);
        setSkills(s.skills); setServers(m.servers); setCustomTools(t.tools); setKbs(k.knowledgeBases);
      } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    })();
  }, []);

  // Mirror the selected agent (or a blank one when creating) into the editable draft.
  useEffect(() => {
    if (creating) { setDraft(blankAgent()); setError(null); }
    else if (selected) { setDraft(selected); setError(null); }
  }, [creating, selected]);

  const patch = (p: Partial<Agent>) => setDraft((d) => ({ ...d, ...p }));
  const toggleIn = (key: "tools" | "skills" | "mcpServers" | "customTools" | "knowledgeBases", val: string) =>
    setDraft((d) => ({
      ...d,
      [key]: d[key].includes(val) ? d[key].filter((x) => x !== val) : [...d[key], val],
    }));

  const save = async () => {
    if (!draft.name.trim()) { setError("Give the agent a name."); return; }
    setBusy(true); setError(null);
    const body = {
      name: draft.name.trim(), description: draft.description, persona: draft.persona,
      temperature: draft.temperature, topP: draft.topP, topK: draft.topK,
      workingDir: draft.workingDir, role: draft.role, guardrails: draft.guardrails,
      tools: draft.tools, skills: draft.skills, mcpServers: draft.mcpServers,
      customTools: draft.customTools,
      knowledgeBases: draft.knowledgeBases,
    };
    try {
      const saved = creating
        ? await apiPost<Agent>("/api/agents", body)
        : await apiJson<Agent>(`/api/agents/${encodeURIComponent(draft.id)}`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
          });
      onCreatingChange(false);
      onSelectedChange(saved.id);
      onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
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
              {([["overview", "Overview"], ["board", "Board"], ["activity", "Activity"], ["schedule", "Schedule"], ["triggers", "Triggers"], ["settings", "Settings"]] as const).map(([id, label]) => (
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
          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-5">
            {/* left: who the agent is + how it generates */}
            <div className="space-y-6 lg:col-span-3">
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
                  <Field label="Job role" hint="Give the agent a job title to run it as an intern — it gets a task board, an activity feed, and a read-only shell. Leave blank for a plain assistant.">
                    <input className={inputCls} value={draft.role} onChange={(e) => patch({ role: e.target.value })}
                      placeholder="e.g. Software Engineering Intern — blank = not an intern" />
                  </Field>
                  <Field label="Persona / system prompt" hint="Becomes the system prompt for this agent.">
                    <textarea className={`${inputCls} min-h-[140px] resize-y font-mono text-xs leading-relaxed`} value={draft.persona}
                      onChange={(e) => patch({ persona: e.target.value })}
                      placeholder="You are a helpful assistant that…" />
                  </Field>
                </div>
              </Card>

              <Card title="Generation" hint="How the model samples tokens.">
                <div className="space-y-4">
                  <Field label={`Temperature — ${draft.temperature.toFixed(1)}`} hint="Higher = more creative. Gemma's default is 1.0.">
                    <input type="range" min={0} max={2} step={0.1} value={draft.temperature}
                      onChange={(e) => patch({ temperature: parseFloat(e.target.value) })} className="w-full accent-primary" />
                  </Field>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="top_p" hint="Nucleus sampling. Blank = model default. Gemma: 0.95.">
                      <input className={inputCls} type="number" min={0} max={1} step={0.01}
                        value={draft.topP ?? ""} placeholder="0.95"
                        onChange={(e) => patch({ topP: e.target.value === "" ? null : Number(e.target.value) })} />
                    </Field>
                    <Field label="top_k" hint="Limit to the K most likely tokens. Blank = model default. Gemma: 64.">
                      <input className={inputCls} type="number" min={0} step={1}
                        value={draft.topK ?? ""} placeholder="64"
                        onChange={(e) => patch({ topK: e.target.value === "" ? null : Number(e.target.value) })} />
                    </Field>
                  </div>
                </div>
              </Card>
            </div>

            {/* right: the harness — what the agent can reach, as a capability stack */}
            <div className="space-y-3 lg:col-span-2">
              <div className="flex items-baseline gap-2 px-1">
                <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">The harness</h3>
                <span className="text-[11px] text-mute">— your files · your memory · tools</span>
              </div>

              <Card title="Files" badge={<CapBadge on={filesOn} />}
                hint="A working folder grants scoped file tools automatically.">
                <input className={inputCls} value={draft.workingDir}
                  onChange={(e) => patch({ workingDir: e.target.value })}
                  placeholder="/Users/you/projects/my-app" />
                {filesOn ? (
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {FS_TOOLS.map((t) => (
                      <span key={t} className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{t}</span>
                    ))}
                  </div>
                ) : (
                  <p className="mt-2 text-xs text-mute">Set a folder to let this agent read and write your files — nothing outside it.</p>
                )}
              </Card>

              <Card title="Memory" badge={<CapBadge always />}
                hint="Facts the agent keeps across sessions. The agent writes these itself with memory_write; add or prune them here.">
                {creating || !draft.id ? (
                  <p className="text-xs text-mute">Create the agent first — its memory appears here.</p>
                ) : (
                  <MemoryCard agentId={draft.id} />
                )}
              </Card>

              <Card title="Tools" badge={<CapBadge on={draft.tools.length > 0} onLabel={`${draft.tools.length} on`} offLabel="none" />}>
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
                </div>
              </Card>

              <Card title="Skills" badge={<CapBadge on={draft.skills.length > 0} onLabel={`${draft.skills.length} on`} offLabel="none" />}
                hint="Skill packs the agent can load on demand.">
                {skills.length === 0 ? (
                  <p className="text-xs text-mute">No skills yet — upload some in the Skills tab.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {skills.map((s) => {
                      const on = draft.skills.includes(s.slug);
                      return (
                        <button key={s.slug} title={s.description} onClick={() => toggleIn("skills", s.slug)}
                          className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                            on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                          }`}><span aria-hidden>{on ? "✓" : "+"}</span>{s.name}</button>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="Knowledge" badge={<CapBadge on={draft.knowledgeBases.length > 0} onLabel={`${draft.knowledgeBases.length} on`} offLabel="none" />}
                hint="Knowledge bases the agent can search with the kb_search tool (RAG over your documents).">
                {kbs.length === 0 ? (
                  <p className="text-xs text-mute">No knowledge bases yet — create one in the Knowledge tab.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {kbs.map((k) => {
                      const on = draft.knowledgeBases.includes(k.id);
                      return (
                        <button key={k.id} title={`${k.sourceCount} source(s) · ${k.chunkCount} chunk(s)`}
                          onClick={() => toggleIn("knowledgeBases", k.id)}
                          className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                            on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                          }`}><span aria-hidden>{on ? "✓" : "+"}</span>{k.name}</button>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="MCP servers" badge={<CapBadge on={draft.mcpServers.length > 0} onLabel={`${draft.mcpServers.length} on`} offLabel="none" />}
                hint="External tool providers (Model Context Protocol).">
                {servers.length === 0 ? (
                  <p className="text-xs text-mute">No MCP servers yet — add one in the MCP tab.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {servers.map((s) => {
                      const on = draft.mcpServers.includes(s.id);
                      return (
                        <button key={s.id} title={s.transport === "http" ? s.url : `${s.command} ${s.args.join(" ")}`}
                          onClick={() => toggleIn("mcpServers", s.id)}
                          className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                            on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                          }`}><span aria-hidden>{on ? "✓" : "+"}</span>{s.name}{!s.enabled && " (off)"}</button>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="Custom tools" badge={<CapBadge on={draft.customTools.length > 0} onLabel={`${draft.customTools.length} on`} offLabel="none" />}
                hint="HTTP tools you declared — the lightweight alternative to an MCP server. Add them in the Tools tab.">
                {customTools.length === 0 ? (
                  <p className="text-xs text-mute">No custom tools yet — create one in the Tools tab.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {customTools.map((t) => {
                      const on = draft.customTools.includes(t.id);
                      return (
                        <button key={t.id} title={t.description || `${t.method} ${t.url}`}
                          onClick={() => toggleIn("customTools", t.id)}
                          className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                            on ? "border-primary-soft bg-primary-soft text-primary-deep" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                          }`}><span aria-hidden>{on ? "✓" : "+"}</span>{t.name}{!t.enabled && " (off)"}</button>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="Guardrails"
                hint="Automatic bounds for autonomous runs. The destructive/privileged command deny-list is always enforced and can't be turned off here.">
                <div className="space-y-3">
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
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-medium text-body" title="Lets the agent send chat messages (WhatsApp, …) on its own via channel_send. Replies to an inbound chat never need this.">Message people on channels</label>
                    <button onClick={() => patch({ guardrails: { ...draft.guardrails, channelSend: !draft.guardrails.channelSend } })}
                      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        draft.guardrails.channelSend ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                      }`}>{draft.guardrails.channelSend ? "On" : "Off"}</button>
                  </div>
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-medium text-body">Verify finished tasks</label>
                    <button onClick={() => patch({ guardrails: { ...draft.guardrails, verify: !draft.guardrails.verify } })}
                      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        draft.guardrails.verify ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:border-primary-line hover:text-ink"
                      }`}>{draft.guardrails.verify ? "On" : "Off"}</button>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Max steps / task" hint="Tool-loop iterations before the agent stops on one task.">
                      <input className={inputCls} type="number" min={1} max={50} value={draft.guardrails.maxIterations}
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, maxIterations: Number(e.target.value) || 1 } })} />
                    </Field>
                    <Field label="Daily task cap" hint="Worker passes per day before it auto-pauses.">
                      <input className={inputCls} type="number" min={1} max={1000} value={draft.guardrails.dailyCap}
                        onChange={(e) => patch({ guardrails: { ...draft.guardrails, dailyCap: Number(e.target.value) || 1 } })} />
                    </Field>
                  </div>
                  <Field label="Extra blocked commands" hint="Comma-separated. Added on top of the built-in deny-list (you can only tighten, never loosen it).">
                    <input className={inputCls} value={draft.guardrails.extraDenied.join(", ")}
                      placeholder="e.g. brew, docker"
                      onChange={(e) => patch({ guardrails: { ...draft.guardrails, extraDenied: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
                  </Field>
                </div>
              </Card>
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
              {view === "board" && selected && <BoardView agentId={selected.id} />}
              {view === "activity" && selected && <ActivityView agentId={selected.id} />}
              {view === "schedule" && selected && <ScheduleView agentId={selected.id} />}
              {view === "triggers" && selected && <TriggersView agentId={selected.id} />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Skills panel
// --------------------------------------------------------------------------- //

function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<(Skill & { body?: string; files?: string[] }) | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try { setSkills((await apiJson<{ skills: Skill[] }>("/api/skills")).skills); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

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

      <div className="mt-5 space-y-2">
        {skills.length === 0 && <p className="text-sm text-mute">No skills uploaded yet.</p>}
        {skills.map((s) => (
          <div key={s.slug} className="flex items-start gap-3 rounded-xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{s.name}</span>
                <span className="font-mono text-[10px] text-mute">{s.slug}</span>
              </div>
              {s.description && <p className="mt-0.5 text-xs text-body">{s.description}</p>}
              {s.whenToUse && <p className="mt-0.5 text-xs text-mute">When: {s.whenToUse}</p>}
            </div>
            <button onClick={() => view(s.slug)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">View</button>
            <button onClick={() => remove(s.slug)} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">Delete</button>
          </div>
        ))}
      </div>

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
  const [servers, setServers] = useState<McpServer[]>([]);
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tools, setTools] = useState<Record<string, { name: string; description: string }[] | string>>({});

  // new-server form
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [argsStr, setArgsStr] = useState("");
  const [envStr, setEnvStr] = useState("");
  const [url, setUrl] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await apiJson<{ servers: McpServer[]; available: boolean }>("/api/mcp");
      setServers(r.servers); setAvailable(r.available);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    if (!name.trim()) { setError("Give the server a name."); return; }
    setBusy(true); setError(null);
    const body = transport === "http"
      ? { name: name.trim(), transport, url: url.trim() }
      : { name: name.trim(), transport, command: command.trim(),
          args: argsStr.trim() ? argsStr.trim().split(/\s+/) : [], env: parseEnv(envStr) };
    try {
      await apiPost("/api/mcp", body);
      setName(""); setCommand(""); setArgsStr(""); setEnvStr(""); setUrl("");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
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
        <h3 className="font-display mb-3 text-sm font-bold text-ink">Add MCP server</h3>
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

          <button onClick={add} disabled={busy}
            className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
            {busy ? "Adding…" : "Add server"}
          </button>
        </div>
      </div>

      {error && <p className="mt-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}

      <div className="mt-5 space-y-2">
        {servers.length === 0 && <p className="text-sm text-mute">No MCP servers yet.</p>}
        {servers.map((s) => {
          const result = tools[s.id];
          return (
            <div key={s.id} className="rounded-xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{s.name}</span>
                    <span className="rounded-full border border-pressed px-2 py-0.5 font-mono text-[10px] text-mute">{s.transport}</span>
                    {!s.enabled && <span className="font-mono text-[10px] text-mute">disabled</span>}
                  </div>
                  <p className="mt-0.5 truncate font-mono text-[11px] text-body">
                    {s.transport === "http" ? s.url : `${s.command} ${s.args.join(" ")}`}
                  </p>
                </div>
                <button onClick={() => test(s.id)} disabled={!available}
                  className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink disabled:opacity-40">Test</button>
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
  params: [] as Param[], headersStr: "", allowPrivate: false, timeout: 20,
});

function ToolsPanel() {
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

  const startEdit = (t: CustomTool) => {
    setForm({
      id: t.id, name: t.name, description: t.description, method: t.method, url: t.url,
      params: schemaToParams(t.parameters), headersStr: headersToText(t.headers),
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
      headers: parseEnv(form.headersStr), allowPrivate: form.allowPrivate,
      timeout: form.timeout,
    };
    try {
      if (editing) {
        await apiJson(`/api/tools/${encodeURIComponent(form.id)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
      } else {
        await apiPost("/api/tools", body);
      }
      setForm(blankToolForm());
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
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

          <Field label="Parameters" hint="The arguments the model supplies. Becomes the tool's JSON Schema.">
            <div className="space-y-2">
              {form.params.length === 0 && <p className="text-xs text-mute">No parameters — the tool takes no arguments.</p>}
              {form.params.map((p, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input className={`${inputCls} flex-1`} value={p.name} onChange={(e) => setParam(i, { name: e.target.value })} placeholder="name" />
                  <select className={`${inputCls} w-28`} value={p.type} onChange={(e) => setParam(i, { type: e.target.value })}>
                    {PARAM_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                  <input className={`${inputCls} flex-1`} value={p.description} onChange={(e) => setParam(i, { description: e.target.value })} placeholder="description" />
                  <button onClick={() => setParam(i, { required: !p.required })}
                    title="Required?"
                    className={`shrink-0 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors ${
                      p.required ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:text-ink"
                    }`}>req</button>
                  <button onClick={() => removeParam(i)} className="shrink-0 text-mute hover:text-danger">✕</button>
                </div>
              ))}
              <button onClick={addParam} className="rounded-lg border border-pressed px-2.5 py-1 text-xs text-body hover:text-ink">+ Add parameter</button>
            </div>
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
          {hint && <span className="cursor-help text-mute" title={hint}>ⓘ</span>}
          {badge && <span className="ml-auto">{badge}</span>}
        </div>
      )}
      {children}
    </section>
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

function MemoryCard({ agentId }: { agentId: string }) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await apiJson<{ memories: Memory[]; total: number }>(`/api/agents/${encodeURIComponent(agentId)}/memory`);
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
        <div className="mt-2 space-y-1">
          {memories.map((m) => (
            <div key={m.id} className="group flex items-start gap-2 rounded-lg px-2 py-1 hover:bg-soft">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-mute" />
              <span className="min-w-0 flex-1 text-xs text-body">{m.content}
                {m.tags && <span className="ml-1.5 font-mono text-[10px] text-mute">[{m.tags}]</span>}</span>
              <button onClick={() => remove(m.id)} title="Forget"
                className="shrink-0 text-mute opacity-0 hover:text-ink group-hover:opacity-100">✕</button>
            </div>
          ))}
          {total > memories.length && (
            <p className="px-2 pt-1 text-[10px] text-mute">+{total - memories.length} older note(s)</p>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-body">
        {label}
        {hint && <span className="text-mute" title={hint}>ⓘ</span>}
      </label>
      {children}
    </div>
  );
}

export default AgentsPage;
