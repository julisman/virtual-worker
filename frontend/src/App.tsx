// App shell — collapsible left sidebar nav + routed content pane.
// Brand theme (navy on warm cream); sidebar collapse state persists.

import { useCallback, useEffect, useState } from "react";
import { apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { LoginScreen } from "@/features/team/login-screen";
import appIcon from "@/assets/icon.svg";
import { PlaygroundPage } from "@/features/playground/playground-page";
import { DebugPage } from "@/features/debug/debug-page";
import { HubPage } from "@/features/hub/hub-page";
import { ConnectPage } from "@/features/connect/connect-page";
import { KnowledgePage } from "@/features/knowledge/knowledge-page";
import { AgentsPage, type Agent } from "@/features/agents/agents-page";
import { TeamPage } from "@/features/team/team-page";
import { TemplatesPage } from "@/features/templates/templates-page";
import { ChannelsPage } from "@/features/channels/channels-page";
import { SettingsPage } from "@/features/settings/settings-page";
import { DocsPage } from "@/features/docs/docs-page";
import { MonitoringPage } from "@/features/monitoring/monitoring-page";
import { AnalyticsPage } from "@/features/analytics/analytics-page";

type Tab = "playground" | "agents" | "team" | "channels" | "templates" | "knowledge" | "hub" | "connect" | "debug" | "monitor" | "analytics" | "docs" | "settings";

export interface SessionMeta { id: string; title: string; model: string | null; agentId?: string | null; updatedAt: string; messageCount: number }
export interface OpenRequest { id: string | null; nonce: number }
export interface AgentRequest { id: string; nonce: number }

// --------------------------------------------------------------------------- //
// Icons (inline, stroke-based to inherit currentColor)
// --------------------------------------------------------------------------- //

type IconProps = { className?: string };
const I = (p: { children: React.ReactNode } & IconProps) => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={p.className}>
    {p.children}
  </svg>
);
const ChatIcon = (p: IconProps) => <I {...p}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></I>;
const HubIcon = (p: IconProps) => <I {...p}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></I>;
const DebugIcon = (p: IconProps) => <I {...p}><path d="M4 6h16M4 12h16M4 18h10" /></I>;
const ConnectIcon = (p: IconProps) => <I {...p}><path d="M8 12h8M8 12a4 4 0 1 1-4-4M16 12a4 4 0 1 0 4-4" /><circle cx="4" cy="8" r="0" /></I>;
const AgentsIcon = (p: IconProps) => <I {...p}><rect x="5" y="8" width="14" height="11" rx="2" /><path d="M12 3v5M9 13h.01M15 13h.01" /></I>;
const TeamIcon = (p: IconProps) => <I {...p}><circle cx="9" cy="7" r="3" /><path d="M3 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2" /><path d="M16 3.5a3 3 0 0 1 0 6M21 21v-2a4 4 0 0 0-3-3.87" /></I>;
const SettingsIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></I>;
const CollapseIcon = (p: IconProps) => <I {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></I>;
const PlusIcon = (p: IconProps) => <I {...p}><path d="M12 5v14M5 12h14" /></I>;
const DocsIcon = (p: IconProps) => <I {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></I>;
const MonitorIcon = (p: IconProps) => <I {...p}><path d="M3 12h4l2 6 4-14 2 8h6" /></I>;
const AnalyticsIcon = (p: IconProps) => <I {...p}><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" /><rect x="12" y="7" width="3" height="10" /><rect x="17" y="13" width="3" height="4" /></I>;
const KnowledgeIcon = (p: IconProps) => <I {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /><path d="M9 7h6M9 11h6" /></I>;
const ChannelsIcon = (p: IconProps) => <I {...p}><path d="M4 4h16v11H8l-4 4z" /><path d="M8 8h8M8 11h5" /></I>;
const TemplatesIcon = (p: IconProps) => <I {...p}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /><path d="M6.5 14v7M3 17.5h7" /></I>;

const NAV: { id: Tab; label: string; icon: (p: IconProps) => React.ReactNode }[] = [
  { id: "playground", label: "Chat", icon: ChatIcon },
  { id: "agents", label: "Agents", icon: AgentsIcon },
  { id: "team", label: "Team", icon: TeamIcon },
  { id: "channels", label: "Channels", icon: ChannelsIcon },
  { id: "templates", label: "Templates", icon: TemplatesIcon },
  { id: "knowledge", label: "Knowledge", icon: KnowledgeIcon },
  { id: "hub", label: "Models", icon: HubIcon },
  { id: "connect", label: "Connect", icon: ConnectIcon },
  { id: "debug", label: "Debug", icon: DebugIcon },
  { id: "monitor", label: "Monitor", icon: MonitorIcon },
  { id: "analytics", label: "Analytics", icon: AnalyticsIcon },
  { id: "docs", label: "Docs", icon: DocsIcon },
  { id: "settings", label: "Settings", icon: SettingsIcon },
];

// --------------------------------------------------------------------------- //
// App
// --------------------------------------------------------------------------- //

type AgentsSub = "agents" | "skills" | "mcp" | "tools";
const AGENT_SUBS: { id: AgentsSub; label: string }[] = [
  { id: "agents", label: "Agents" },
  { id: "skills", label: "Skills" },
  { id: "mcp", label: "MCP" },
  { id: "tools", label: "Tools" },
];

export function App() {
  const { loading: authLoading, teamMode, user, logout } = useAuth();
  const [tab, setTab] = useState<Tab>("playground");
  const [agentsSub, setAgentsSub] = useState<AgentsSub>("agents");
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem("vw:sidebar-collapsed") === "1",
  );

  useEffect(() => {
    localStorage.setItem("vw:sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  // ---- chat sessions (history lives in this sidebar) ---- //
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(() => localStorage.getItem("vw:chat-session"));
  const [openReq, setOpenReq] = useState<OpenRequest>(() => {
    const last = localStorage.getItem("vw:chat-session");
    return { id: last, nonce: last ? 1 : 0 };
  });

  const loadSessions = useCallback(async () => {
    try { setSessions((await apiJson<{ sessions: SessionMeta[] }>("/api/sessions")).sessions); }
    catch { /* */ }
  }, []);
  useEffect(() => { loadSessions(); }, [loadSessions]);

  const openChat = useCallback((id: string | null) => setOpenReq((r) => ({ id, nonce: r.nonce + 1 })), []);
  const deleteChat = useCallback(async (id: string) => {
    await apiJson(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
    if (id === currentId) openChat(null);
    loadSessions();
  }, [currentId, openChat, loadSessions]);

  // Chat reports its active session id back up so we can highlight + persist it.
  const onSessionId = useCallback((id: string | null) => {
    setCurrentId(id);
    if (id) localStorage.setItem("vw:chat-session", id);
    else localStorage.removeItem("vw:chat-session");
  }, []);

  // ---- agent roster (lives in this sidebar now) ---- //
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [creatingAgent, setCreatingAgent] = useState(false);

  const loadAgents = useCallback(async () => {
    try {
      const list = (await apiJson<{ agents: Agent[] }>("/api/agents")).agents;
      setAgents(list);
      // Keep a valid selection; if none/empty, fall to the first agent (or create).
      setSelectedAgentId((cur) => (cur && list.some((a) => a.id === cur) ? cur : list[0]?.id ?? null));
      setCreatingAgent((c) => (list.length === 0 ? true : c));
    } catch { /* */ }
  }, []);
  useEffect(() => { loadAgents(); }, [loadAgents]);

  const selectAgent = useCallback((id: string) => {
    setSelectedAgentId(id); setCreatingAgent(false); setAgentsSub("agents"); setTab("agents");
  }, []);
  const startNewAgent = useCallback(() => {
    setCreatingAgent(true); setAgentsSub("agents"); setTab("agents");
  }, []);

  // "Chat with this agent" from the Agents page → start a fresh chat as that agent.
  const [agentReq, setAgentReq] = useState<AgentRequest>({ id: "", nonce: 0 });
  const chatWithAgent = useCallback((id: string) => {
    openChat(null);
    setAgentReq((r) => ({ id, nonce: r.nonce + 1 }));
    setTab("playground");
  }, [openChat]);

  // After a template deploy, land on the freshly-created agent (FR-12).
  const onDeployed = useCallback(async (id: string) => {
    await loadAgents();
    setSelectedAgentId(id); setCreatingAgent(false); setAgentsSub("agents"); setTab("agents");
  }, [loadAgents]);

  // Team mode (PRD-07): when on and there's no session, gate the whole app behind
  // the login screen. In solo mode (default) teamMode is false and this never fires.
  if (authLoading) return <div className="h-screen bg-base" />;
  if (teamMode && !user) return <LoginScreen />;

  return (
    <div className="flex h-screen overflow-hidden bg-base text-ink">
      {/* Sidebar */}
      <aside
        className={`flex shrink-0 flex-col border-r border-pressed bg-softer transition-[width] duration-200 ${
          collapsed ? "w-16" : "w-[252px]"
        }`}
      >
        {/* Brand + collapse toggle */}
        <div className={`flex items-center gap-2 px-4 py-4 ${collapsed ? "justify-center" : ""}`}>
          <img src={appIcon} alt="Virtual Worker" onClick={collapsed ? () => setCollapsed(false) : undefined}
            title={collapsed ? "Expand sidebar" : undefined}
            className={`h-[34px] w-[34px] shrink-0 rounded-[10px] ${collapsed ? "cursor-pointer" : ""}`} />
          {!collapsed && (
            <>
              <span className="font-display text-[21px] font-semibold text-ink" title="Virtual Worker">Virtual Worker</span>
              <span className="rounded-full bg-primary-soft px-2 py-0.5 text-[10px] font-bold text-primary-badge">
                v0.1
              </span>
              <button
                onClick={() => setCollapsed((c) => !c)}
                title="Collapse sidebar"
                className="ml-auto rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink"
              >
                <CollapseIcon />
              </button>
            </>
          )}
        </div>

        {/* Nav */}
        <nav className="flex flex-col gap-1 px-2">
          {NAV.map((n) => {
            const active = tab === n.id;
            const showSubs = n.id === "agents" && tab === "agents" && !collapsed;
            return (
              <div key={n.id}>
                <button
                  onClick={() => setTab(n.id)}
                  title={collapsed ? n.label : undefined}
                  className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-bold transition-colors ${
                    collapsed ? "justify-center" : ""
                  } ${active ? "bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)]" : "text-body hover:bg-soft hover:text-ink"}`}
                >
                  <n.icon className="shrink-0" />
                  {!collapsed && <span>{n.label}</span>}
                </button>
                {showSubs && (
                  <div className="mb-1 ml-[1.35rem] mt-1 flex flex-col gap-0.5 border-l border-pressed pl-3">
                    {/* the agents themselves */}
                    <div className="flex items-center justify-between px-3 pb-0.5 pt-1">
                      <span className="text-[10px] font-semibold uppercase tracking-wide text-mute">Agents</span>
                      <button onClick={startNewAgent} title="New agent"
                        className="-mr-1 rounded p-0.5 text-mute hover:text-ink"><PlusIcon /></button>
                    </div>
                    {agents.map((a) => {
                      const on = agentsSub === "agents" && !creatingAgent && selectedAgentId === a.id;
                      return (
                        <button
                          key={a.id}
                          onClick={() => selectAgent(a.id)}
                          title={a.name}
                          className={`truncate rounded-lg px-3 py-1.5 text-left text-sm transition-colors ${
                            on ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                          }`}
                        >{a.name || "Untitled"}</button>
                      );
                    })}
                    {creatingAgent && (
                      <span className="rounded-lg bg-soft px-3 py-1.5 text-sm font-semibold text-ink">New agent…</span>
                    )}

                    {/* shared destinations */}
                    <div className="mx-3 my-1.5 h-px bg-pressed" />
                    {AGENT_SUBS.filter((s) => s.id !== "agents").map((s) => {
                      const on = agentsSub === s.id;
                      return (
                        <button
                          key={s.id}
                          onClick={() => setAgentsSub(s.id)}
                          className={`rounded-lg px-3 py-1.5 text-left text-sm transition-colors ${
                            on ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                          }`}
                        >{s.label}</button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        {/* Chat history (only on the Chat tab, when expanded) */}
        {tab === "playground" && !collapsed ? (
          <div className="mt-3 flex min-h-0 flex-1 flex-col border-t border-pressed pt-3">
            <div className="px-2">
              <button
                onClick={() => openChat(null)}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft hover:text-ink"
              ><PlusIcon /> New chat</button>
            </div>
            <div className="mt-2 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
              {sessions.length === 0 && <p className="px-2 py-3 text-xs text-mute">No saved chats yet.</p>}
              {sessions.map((s) => (
                <div key={s.id} className={`group flex items-center gap-1 rounded-xl px-2.5 py-2 text-sm ${s.id === currentId ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft"}`}>
                  <button onClick={() => openChat(s.id)} className="min-w-0 flex-1 truncate text-left" title={s.title}>{s.title || "Untitled"}</button>
                  <button onClick={() => deleteChat(s.id)} className="shrink-0 text-mute opacity-0 hover:text-ink group-hover:opacity-100" title="Delete">✕</button>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="mt-auto flex items-center gap-2.5 px-4 py-4">
            <img src={appIcon} alt="" className="h-[30px] w-[30px] shrink-0 rounded-[9px]" />
            {!collapsed && (
              <p className="text-[11px] font-semibold leading-snug text-mute">
                Any model · your files<br />your memory · runs local
              </p>
            )}
          </div>
        )}

        {/* Team mode: current user + logout (only when Team mode is on). */}
        {teamMode && user && (
          <div className={`mt-auto flex items-center gap-2.5 border-t border-pressed px-3 py-3 ${collapsed ? "justify-center" : ""}`}>
            <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-bold uppercase text-primary-deep"
              title={`${user.username} · ${user.role}`}>
              {user.username.slice(0, 2)}
            </div>
            {!collapsed && (
              <>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-ink">{user.username}</p>
                  <p className="text-[10px] uppercase tracking-wide text-mute">{user.role}</p>
                </div>
                <button onClick={() => logout()} title="Log out"
                  className="rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink">
                  <I><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></I>
                </button>
              </>
            )}
          </div>
        )}
      </aside>

      {/* Content. Chat + Models stay mounted (just hidden) so an in-progress
          conversation or a running download survives switching tabs. Connect +
          Debug remount each visit (cheap, and they want fresh data). */}
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className={`h-full ${tab === "playground" ? "" : "hidden"}`}>
          <PlaygroundPage openRequest={openReq} agentRequest={agentReq} onSessionId={onSessionId} onSaved={loadSessions} />
        </div>
        <div className={`h-full ${tab === "hub" ? "" : "hidden"}`}>
          <HubPage />
        </div>
        {tab === "agents" && (
          <AgentsPage
            onChat={chatWithAgent}
            sub={agentsSub}
            panel={{
              agents,
              selectedId: selectedAgentId,
              creating: creatingAgent,
              onChanged: loadAgents,
              onSelectedChange: setSelectedAgentId,
              onCreatingChange: setCreatingAgent,
            }}
          />
        )}
        {tab === "knowledge" && <KnowledgePage />}
        {tab === "team" && <TeamPage />}
        {tab === "channels" && <ChannelsPage />}
        {tab === "templates" && <TemplatesPage onDeployed={onDeployed} />}
        {tab === "connect" && <ConnectPage />}
        {tab === "debug" && <DebugPage />}
        {tab === "monitor" && <MonitoringPage />}
        {tab === "analytics" && <AnalyticsPage />}
        {tab === "docs" && <DocsPage />}
        {tab === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}

export default App;
