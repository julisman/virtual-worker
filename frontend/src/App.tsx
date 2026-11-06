// App shell — collapsible left sidebar nav + routed content pane.
// Virtual Worker brand theme (navy on warm cream); sidebar collapse state persists.

import { useCallback, useEffect, useState } from "react";
import { apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { LoginScreen } from "@/features/team/login-screen";
import appIcon from "@/assets/icon.svg";
import { PlaygroundPage } from "@/features/playground/playground-page";
import { DebugPage } from "@/features/debug/debug-page";
import { HubPage } from "@/features/hub/hub-page";
import { ConnectPage } from "@/features/connect/connect-page";
import { KnowledgePage } from "@/features/knowledge/knowledge-page";
import { WikiPage } from "@/features/wiki/wiki-page";
import { AgentsPage, type Agent } from "@/features/agents/agents-page";
import { TeamPage } from "@/features/team/team-page";
import { TemplatesPage, type Template } from "@/features/templates/templates-page";
import { ChannelsPage } from "@/features/channels/channels-page";
import { compareSemver } from "@/lib/semver";
import { SettingsPage } from "@/features/settings/settings-page";
import { DocsPage } from "@/features/docs/docs-page";
import { MonitoringPage } from "@/features/monitoring/monitoring-page";
import { AnalyticsPage } from "@/features/analytics/analytics-page";

type Tab = "playground" | "agents" | "channels" | "team" | "templates" | "knowledge" | "wiki" | "hub"   | "connect"   | "debug" | "monitor" | "analytics" | "docs" | "settings";

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
const ExpandIcon = (p: IconProps) => <I {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /><path d="M6 9l3 3-3 3" /></I>;
const PlusIcon = (p: IconProps) => <I {...p}><path d="M12 5v14M5 12h14" /></I>;
const DocsIcon = (p: IconProps) => <I {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></I>;
const MonitorIcon = (p: IconProps) => <I {...p}><path d="M3 12h4l2 6 4-14 2 8h6" /></I>;
const AnalyticsIcon = (p: IconProps) => <I {...p}><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" /><rect x="12" y="7" width="3" height="10" /><rect x="17" y="13" width="3" height="4" /></I>;
const KnowledgeIcon = (p: IconProps) => <I {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /><path d="M9 7h6M9 11h6" /></I>;
const WikiIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="9" /><path d="M8 9h8M8 12h8M8 15h5" /></I>;
const ChannelsIcon = (p: IconProps) => <I {...p}><path d="M4 4h16v11H8l-4 4z" /><path d="M8 8h8M8 11h5" /></I>;
const TemplatesIcon = (p: IconProps) => <I {...p}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /><path d="M6.5 14v7M3 17.5h7" /></I>;
const ModelGroupIcon = (p: IconProps) => <I {...p}><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" /></I>;
const SunIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></I>;
const MoonIcon = (p: IconProps) => <I {...p}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></I>;

type NavItem = { id: Tab; label: string; icon: (p: IconProps) => React.ReactNode };
// A collapsible sidebar group — future plugins land under "Plugins" the same way.
type NavGroup = { group: string; icon: (p: IconProps) => React.ReactNode; items: NavItem[] };

const NAV: (NavItem | NavGroup)[] = [
  { id: "playground", label: "Chat", icon: ChatIcon },
  { id: "agents", label: "Agents", icon: AgentsIcon },
  { id: "team", label: "Team", icon: TeamIcon },
  { id: "templates", label: "Templates", icon: TemplatesIcon },
  { id: "knowledge", label: "Knowledge", icon: KnowledgeIcon },
  { id: "wiki", label: "Wiki", icon: WikiIcon },
  { id: "channels", label: "Channels", icon: ChannelsIcon },
  { group: "Model", icon: ModelGroupIcon, items: [
    { id: "hub", label: "Models", icon: HubIcon },
    { id: "connect", label: "Connect", icon: ConnectIcon },
  ] },
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
  const { theme, toggle: toggleTheme } = useTheme();
  const [tab, setTab] = useState<Tab>("playground");
  const [agentsSub, setAgentsSub] = useState<AgentsSub>("agents");
  // Nav group toggles (Model, Plugins). Unset = open only while a child is active.
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  // The Agents sub-list (roster + Skills/MCP/Tools) — re-clicking Agents toggles
  // it, matching the group behavior above.
  const [agentsNavOpen, setAgentsNavOpen] = useState(true);
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem("vw:sidebar-collapsed") === "1",
  );

  useEffect(() => {
    localStorage.setItem("vw:sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  // ---- chat session (restore the last one on reload; per-agent history now
  // lives on each agent's Conversations tab, not a global sidebar list) ----
  const [openReq, setOpenReq] = useState<OpenRequest>(() => {
    const last = localStorage.getItem("vw:chat-session");
    return { id: last, nonce: last ? 1 : 0 };
  });

  const openChat = useCallback((id: string | null) => setOpenReq((r) => ({ id, nonce: r.nonce + 1 })), []);

  // Chat reports its active session id back up so we can persist + restore it.
  const onSessionId = useCallback((id: string | null) => {
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

  // PRD-29 M3 §7.2 — installed template versions, for the sidebar roster's
  // subtle "update available" dot. GET-only: never triggers a registry sync.
  const [templates, setTemplates] = useState<Template[]>([]);
  const loadTemplates = useCallback(async () => {
    try { setTemplates((await apiJson<{ templates: Template[] }>("/api/templates")).templates); }
    catch { /* the dot is best-effort */ }
  }, []);
  useEffect(() => { loadTemplates(); }, [loadTemplates]);

  const agentHasUpdate = useCallback((a: Agent) => {
    const ref = a.templateRef;
    if (!ref) return false;
    const installed = templates.find((t) => t.sourceId === ref.sourceId && t.templateId === ref.templateId);
    return !!installed && compareSemver(installed.version, ref.version) > 0;
  }, [templates]);

  const selectAgent = useCallback((id: string) => {
    setSelectedAgentId(id); setCreatingAgent(false); setAgentsSub("agents"); setTab("agents"); setAgentsNavOpen(true);
  }, []);
  const startNewAgent = useCallback(() => {
    setCreatingAgent(true); setAgentsSub("agents"); setTab("agents"); setAgentsNavOpen(true);
  }, []);

  // "Chat with this agent" from the Agents page → start a fresh chat as that agent.
  const [agentReq, setAgentReq] = useState<AgentRequest>({ id: "", nonce: 0 });
  const chatWithAgent = useCallback((id: string) => {
    openChat(null);
    setAgentReq((r) => ({ id, nonce: r.nonce + 1 }));
    setTab("playground");
  }, [openChat]);

  // Open a past conversation (from an agent's Conversations tab) back in Chat.
  const openSessionFromAgent = useCallback((id: string) => {
    openChat(id);
    setTab("playground");
  }, [openChat]);

  // "Trace in Debug" from a chat turn or an activity row → open the Debug tab on
  // that session, and (when known) focus the exact run so it's not just the session.
  const [debugReq, setDebugReq] = useState<{ id: string; runId?: string; nonce: number }>({ id: "", nonce: 0 });
  const traceInDebug = useCallback((sessionId: string, runId?: string) => {
    setDebugReq((r) => ({ id: sessionId, runId, nonce: r.nonce + 1 }));
    setTab("debug");
  }, []);

  // After a template deploy, land on the freshly-created agent (FR-12).
  const onDeployed = useCallback(async (id: string) => {
    await Promise.all([loadAgents(), loadTemplates()]);
    setSelectedAgentId(id); setCreatingAgent(false); setAgentsSub("agents"); setTab("agents"); setAgentsNavOpen(true);
  }, [loadAgents, loadTemplates]);

  // PRD-29 M4: an in-page "Update all" (from the Templates tab) changes agents'
  // templateRef versions but stays on that tab — without this the sidebar's
  // "update available" dot goes stale. Same refresh as onDeployed, minus the
  // navigation (the operator stays put; Update all's own dialog reports results).
  const onTemplatesUpdatedInPage = useCallback(async () => {
    await Promise.all([loadAgents(), loadTemplates()]);
  }, [loadAgents, loadTemplates]);

  // Sidebar version badge: the installed desktop version (via preload) — a
  // plain-browser dev run has no desktop shell, so it shows "dev".
  const appVersion = window.vesDesktop?.version ? `v${window.vesDesktop.version}` : "dev";

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
        <div className={`flex items-center gap-2 px-4 py-4 ${collapsed ? "flex-col" : ""}`}>
          <img src={appIcon} alt="Virtual Worker" onClick={collapsed ? () => setCollapsed(false) : undefined}
            title={collapsed ? "Expand sidebar" : undefined}
            className={`h-[34px] w-[34px] shrink-0 rounded-[10px] ${collapsed ? "cursor-pointer" : ""}`} />
          {!collapsed && (
            <>
              <span className="font-display text-[21px] font-semibold text-ink" title="Virtual Worker">Virtual Worker</span>
              <span className="rounded-full bg-primary-soft px-2 py-0.5 text-[10px] font-bold text-primary-badge">
                {appVersion}
              </span>
              <button
                onClick={toggleTheme}
                title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                className="ml-auto rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink"
              >
                {theme === "dark" ? <SunIcon /> : <MoonIcon />}
              </button>
              <button
                onClick={() => setCollapsed((c) => !c)}
                title="Collapse sidebar"
                className="rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink"
              >
                <CollapseIcon />
              </button>
            </>
          )}
          {collapsed && (
            <>
              {/* Dedicated expand control — the collapse button (shown only when
                  expanded) has no visible counterpart here, so without this the
                  rail looks stuck. Clicking the logo above still expands too. */}
              <button
                onClick={() => setCollapsed(false)}
                title="Expand sidebar"
                aria-label="Expand sidebar"
                className="rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink"
              >
                <ExpandIcon />
              </button>
              <button
                onClick={toggleTheme}
                title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                className="rounded-lg p-1.5 text-mute hover:bg-soft hover:text-ink"
              >
                {theme === "dark" ? <SunIcon /> : <MoonIcon />}
              </button>
            </>
          )}
        </div>

        {/* Nav */}
        <nav className="flex flex-col gap-1 overflow-y-auto px-2">
          {NAV.map((n) => {
            if ("group" in n) {
              const containsActive = n.items.some((i) => i.id === tab);
              const open = openGroups[n.group] ?? containsActive;
              if (collapsed) {
                // No room for a header in the icon rail — show the children directly.
                return n.items.map((i) => (
                  <button key={i.id} onClick={() => setTab(i.id)} title={i.label}
                    className={`flex w-full items-center justify-center gap-3 rounded-xl px-3 py-2.5 text-sm font-bold transition-colors ${
                      tab === i.id ? "bg-user-bubble text-user-bubble-ink" : "text-body hover:bg-soft hover:text-ink"
                    }`}>
                    <i.icon className="shrink-0" />
                  </button>
                ));
              }
              return (
                <div key={n.group}>
                  <button
                    onClick={() => setOpenGroups((g) => ({ ...g, [n.group]: !open }))}
                    className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-bold transition-colors hover:bg-soft hover:text-ink ${
                      containsActive ? "text-ink" : "text-body"
                    }`}
                  >
                    <n.icon className="shrink-0" />
                    <span>{n.group}</span>
                    <span aria-hidden className={`ml-auto text-[10px] text-mute transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
                  </button>
                  {open && (
                    <div className="mb-1 ml-[1.35rem] mt-1 flex flex-col gap-0.5 border-l border-pressed pl-3">
                      {n.items.map((i) => {
                        const on = tab === i.id;
                        return (
                          <button key={i.id} onClick={() => setTab(i.id)}
                            className={`flex items-center gap-2.5 rounded-lg px-3 py-1.5 text-left text-sm transition-colors ${
                              on ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                            }`}>
                            <i.icon className="shrink-0" />
                            <span>{i.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            }
            const active = tab === n.id;
            const showSubs = n.id === "agents" && tab === "agents" && !collapsed && agentsNavOpen;
            return (
              <div key={n.id}>
                <button
                  onClick={() => {
                    // Re-clicking Agents while it's active toggles its sub-list
                    // (same affordance as the Model/Plugins groups).
                    if (n.id === "agents" && tab === "agents") setAgentsNavOpen((o) => !o);
                    else { setTab(n.id); if (n.id === "agents") setAgentsNavOpen(true); }
                  }}
                  title={collapsed ? n.label : undefined}
                  className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-bold transition-colors ${
                    collapsed ? "justify-center" : ""
                  } ${active ? "bg-user-bubble text-user-bubble-ink" : "text-body hover:bg-soft hover:text-ink"}`}
                >
                  <n.icon className="shrink-0" />
                  {!collapsed && <span>{n.label}</span>}
                  {n.id === "agents" && !collapsed && (
                    <span aria-hidden className={`ml-auto text-[10px] transition-transform ${
                      active ? "text-user-bubble-ink/70" : "text-mute"
                    } ${showSubs ? "rotate-90" : ""}`}>▸</span>
                  )}
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
                      const hasUpdate = agentHasUpdate(a);
                      return (
                        <button
                          key={a.id}
                          onClick={() => selectAgent(a.id)}
                          title={hasUpdate ? `${a.name} — template update available` : a.name}
                          className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-left text-sm transition-colors ${
                            on ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                          }`}
                        >
                          <span className="min-w-0 flex-1 truncate">{a.name || "Untitled"}</span>
                          {hasUpdate && (
                            <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn" />
                          )}
                        </button>
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

        {/* New chat (Chat tab, expanded). Per-agent history lives on each agent's
            Conversations tab now — no global history list here. */}
        {tab === "playground" && !collapsed && (
          <div className="mt-3 border-t border-pressed px-2 pt-3">
            <button
              onClick={() => openChat(null)}
              className="flex w-full items-center justify-center gap-2 rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft hover:text-ink"
            ><PlusIcon /> New chat</button>
          </div>
        )}

        <div className="mt-auto flex items-center gap-2.5 px-4 py-4">
          <img src={appIcon} alt="" className="h-[30px] w-[30px] shrink-0 rounded-[9px]" />
          {!collapsed && (
            <p className="text-[11px] font-semibold leading-snug text-mute">
              Any model · your files<br />your memory · runs local
            </p>
          )}
        </div>

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
          <PlaygroundPage openRequest={openReq} agentRequest={agentReq} onSessionId={onSessionId} onTrace={traceInDebug} />
        </div>
        <div className={`h-full ${tab === "hub" ? "" : "hidden"}`}>
          <HubPage />
        </div>
        <div className={`h-full ${tab === "channels" ? "" : "hidden"}`}>
          <ChannelsPage />
        </div>
        {tab === "agents" && (
          <AgentsPage
            onChat={chatWithAgent}
            onOpenSession={openSessionFromAgent}
            onTrace={traceInDebug}
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
        {tab === "wiki" && <WikiPage />}
        {tab === "team" && <TeamPage />}
        {tab === "templates" && <TemplatesPage onDeployed={onDeployed} onUpdated={onTemplatesUpdatedInPage} />}
        {tab === "connect" && <ConnectPage />}
        {tab === "debug" && <DebugPage openRequest={debugReq} />}
        {tab === "monitor" && <MonitoringPage />}
        {tab === "analytics" && <AnalyticsPage />}
        {tab === "docs" && <DocsPage />}
        {tab === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}

export default App;
