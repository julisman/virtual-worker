// Chat — talk to the active local model via the OpenAI-compatible /v1 gateway.
//
// Unsloth-inspired: centered greeting on an empty thread, a large rounded
// composer ("Ask anything") with an attach menu, system prompt, temperature,
// and an up-arrow send. Monochrome (Uber-inspired) theme; plain React.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch as authFetch, apiJson } from "@/lib/api";
import type { Agent } from "@/features/agents/agents-page";
import appIcon from "@/assets/icon.svg";

const AGENT_KEY = "vw:chat-agent";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

interface ChatStep { name: string; args: Record<string, unknown>; done: boolean; result?: string }

interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  images?: string[]; // data URLs (multimodal — needs a vision model)
  steps?: ChatStep[]; // tool activity (agentic / Search)
  meta?: { ms: number; tokens?: number; tokSec?: number; tools?: string[] };
}

interface Pending {
  kind: "text" | "image";
  name: string;
  text?: string;
  dataUrl?: string;
}

interface OpenRequest { id: string | null; nonce: number }
interface AgentRequest { id: string; nonce: number }
interface ChatProps {
  openRequest: OpenRequest;          // sidebar asks to open a session (id) or start new (null)
  agentRequest: AgentRequest;        // "Chat with this agent" asks to select an agent
  onSessionId: (id: string | null) => void;  // report the active session id back up
  onSaved: () => void;               // a turn was saved → refresh the sidebar list
}

const newSessionId = () =>
  "chat:" + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));

// --------------------------------------------------------------------------- //
// Icons
// --------------------------------------------------------------------------- //

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}
const Up = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
);
const Plus = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14" /></svg>
);
const Stop = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
);
const Paperclip = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
);
const Globe = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18" /></svg>
);
const Sliders = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg>
);

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

// mlx_lm.server reports the model's full snapshot path as its id; show a clean
// name (the real id is still what we send in requests).
function prettyModel(id: string): string {
  if (!id) return id;
  if (id.toLowerCase().endsWith(".gguf")) return id.split("/").pop() || id;
  const m = id.match(/models--[^/]+--([^/]+)/);
  if (m) return m[1];
  return id.split("/").pop() || id;
}

function stepVerb(name: string): string {
  if (name === "web_fetch") return "Reading";
  if (name === "web_search") return "Searching";
  if (name === "load_skill") return "Loading skill";
  if (name === "load_skill_resource") return "Loading resource";
  if (name.startsWith("mcp__")) return "MCP: " + name.slice(5).replace("__", " · ");
  return name;
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

// Light renderer: split out ``` fenced ``` code blocks; leave the rest as text.
function RichText({ text }: { text: string }) {
  const parts = text.split(/```/);
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <pre key={i} className="my-2 overflow-x-auto rounded-xl bg-panel p-3 font-mono text-[12px] leading-relaxed text-panel-ink">
            {p.replace(/^[\w-]*\n/, "")}
          </pre>
        ) : (
          <span key={i} className="whitespace-pre-wrap">{p}</span>
        ),
      )}
    </>
  );
}

// Agentic stream (/api/chat): typed SSE events from the tool loop.
type AgentEvent =
  | { type: "text"; delta: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; result: string }
  | { type: "error"; message: string }
  | { type: "done"; outputTokens?: number };

async function streamAgent(body: object, sessionId: string, signal: AbortSignal, onEvent: (e: AgentEvent) => void): Promise<void> {
  const res = await authFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-vw-session": sessionId },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const evt = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of evt.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        try { onEvent(JSON.parse(t.slice(5).trim())); } catch { /* */ }
      }
    }
  }
}

async function readFile(file: File): Promise<Pending> {
  if (file.type.startsWith("image/")) {
    const dataUrl: string = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result as string);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
    return { kind: "image", name: file.name, dataUrl };
  }
  const text = await file.text();
  return { kind: "text", name: file.name, text };
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function PlaygroundPage({ openRequest, agentRequest, onSessionId, onSaved }: ChatProps) {
  // The chat always uses whatever model is active (switch it in the Models tab) —
  // no per-chat picker. `model` = the id we send; `modelLabel` = display name.
  const [model, setModel] = useState("");
  const [modelLabel, setModelLabel] = useState("");

  // Agents — the chat runs *as* one (persona + tools + skills). Default to the
  // last-used agent, else the seeded "Assistant".
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState<string | null>(() => localStorage.getItem(AGENT_KEY));
  const selectedAgent = agents.find((a) => a.id === agentId) || null;

  const [system, setSystem] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [temperature, setTemperature] = useState(0.7);
  const [searchOn, setSearchOn] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showAttachMenu, setShowAttachMenu] = useState(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<Pending[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // active session (list + selection live in the app sidebar; we own messages)
  const [sessionId, setSessionId] = useState<string | null>(null);
  const openNonce = useRef(0);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const loadModels = useCallback(async () => {
    // The runtime knows the single ACTIVE model. MLX's /v1/models lists several,
    // so it's NOT a reliable source — trust the runtime when it manages the
    // server, and only fall back to /v1/models for an external upstream.
    try {
      const st = await apiJson<{ managed: boolean; healthy: boolean; modelPath: string | null; modelName: string | null }>("/api/runtime/status");
      if (st.managed) {
        setModelLabel(st.modelName || "");
        setModel(st.healthy ? (st.modelPath || st.modelName || "") : "");
        return;
      }
    } catch { /* not managed / endpoint missing → fall back */ }
    try {
      const data = await (await authFetch("/v1/models")).json();
      const id = data?.data?.[0]?.id ?? "";
      setModel(id);
      setModelLabel(id ? prettyModel(id) : "");
    } catch { setModel(""); setModelLabel(""); }
  }, []);
  // Poll so the chat tracks the active model when you switch it in the Models tab.
  useEffect(() => {
    loadModels();
    const t = setInterval(loadModels, 4000);
    return () => clearInterval(t);
  }, [loadModels]);

  // Load agents; default the selection to the last-used (or first) agent.
  const loadAgents = useCallback(async () => {
    try {
      const { agents: list } = await apiJson<{ agents: Agent[] }>("/api/agents");
      setAgents(list);
      setAgentId((cur) => (cur && list.some((a) => a.id === cur) ? cur : list[0]?.id ?? null));
    } catch { /* */ }
  }, []);
  useEffect(() => { loadAgents(); }, [loadAgents]);

  // Persist the choice; track the agent's preferred temperature on switch.
  useEffect(() => {
    if (agentId) localStorage.setItem(AGENT_KEY, agentId);
    const a = agents.find((x) => x.id === agentId);
    if (a) setTemperature(a.temperature);
  }, [agentId, agents]);

  // "Chat with this agent" from the Agents page selects that agent here.
  const agentNonce = useRef(0);
  useEffect(() => {
    if (agentRequest.nonce === agentNonce.current) return;
    agentNonce.current = agentRequest.nonce;
    if (agentRequest.id) setAgentId(agentRequest.id);
  }, [agentRequest]);

  // ---- session open/new driven by the sidebar (openRequest) ---- //
  useEffect(() => {
    if (openRequest.nonce === openNonce.current) return;
    openNonce.current = openRequest.nonce;
    if (openRequest.nonce === 0) return; // initial empty state, nothing to open

    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setError(null); setNotice(null); setInput("");

    if (openRequest.id) {
      apiJson<{ messages: ChatMessage[]; agentId?: string | null }>(`/api/sessions/${encodeURIComponent(openRequest.id)}`)
        .then((s) => {
          setMessages(s.messages || []); setSessionId(openRequest.id); onSessionId(openRequest.id);
          if (s.agentId) setAgentId(s.agentId);
        })
        .catch(() => { setMessages([]); setSessionId(null); onSessionId(null); });
    } else {
      setMessages([]); setSessionId(null); onSessionId(null);
    }
  }, [openRequest, onSessionId]);

  const saveCurrent = useCallback(async (msgs: ChatMessage[], id: string) => {
    const firstUser = msgs.find((m) => m.role === "user")?.content || "New chat";
    try {
      await apiJson(`/api/sessions/${encodeURIComponent(id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: firstUser.slice(0, 80), model, agentId, messages: msgs }),
      });
      onSaved();
    } catch { /* */ }
  }, [model, agentId, onSaved]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streaming]);

  // auto-grow the textarea
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, []);

  const addFiles = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    const read = await Promise.all(Array.from(files).map(readFile));
    setPending((p) => [...p, ...read]);
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if ((!text && pending.length === 0) || streaming) return;
    if (!model) { setError("No model loaded — activate one in Hub."); return; }
    setError(null);

    const textBlocks = pending.filter((p) => p.kind === "text")
      .map((p) => `\n\n----- ${p.name} -----\n${p.text}`).join("");
    const images = pending.filter((p) => p.kind === "image").map((p) => p.dataUrl!);
    const userMsg: ChatMessage = {
      role: "user",
      content: (text + textBlocks).trim() || "(see attached)",
      images: images.length ? images : undefined,
    };

    const sid = sessionId || newSessionId();
    if (!sessionId) { setSessionId(sid); onSessionId(sid); }

    const history = [...messages, userMsg];
    setMessages([...history, { role: "assistant", content: "" }]);
    setInput("");
    setPending([]);
    setStreaming(true);
    setNotice(null);

    const controller = new AbortController();
    abortRef.current = controller;

    // Accumulate the assistant turn locally (for a deterministic save).
    let asstContent = "";
    let asstSteps: ChatStep[] = [];
    let asstTokens = 0;
    const t0 = Date.now();
    const patchAssistant = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") next[next.length - 1] = fn(last);
        return next;
      });
    const computeMeta = (): ChatMessage["meta"] => {
      const ms = Date.now() - t0;
      const tools = [...new Set(asstSteps.map((s) => s.name))];
      return {
        ms,
        tokens: asstTokens || undefined,
        tokSec: asstTokens && ms > 0 ? Math.round((asstTokens / ms) * 1000 * 10) / 10 : undefined,
        tools: tools.length ? tools : undefined,
      };
    };

    try {
      // The chat always runs as the selected agent (persona + tools + skills).
      // Search forces the web tools on top of the agent's own tools for this turn.
      const toolOverride = searchOn
        ? Array.from(new Set([...(selectedAgent?.tools ?? []), "web_search", "web_fetch"]))
        : undefined;
      const body: Record<string, unknown> = {
        model, temperature,
        agentId: agentId ?? undefined,
        messages: history.map((h) => ({ role: h.role, content: h.content, images: h.images })),
      };
      if (system.trim()) body.system = system.trim();
      if (toolOverride) body.tools = toolOverride;

      await streamAgent(body, sid, controller.signal, (e) => {
        if (e.type === "text") {
          asstContent += e.delta;
          patchAssistant((m) => ({ ...m, content: m.content + e.delta }));
        } else if (e.type === "tool_call") {
          asstSteps = [...asstSteps, { name: e.name, args: e.args, done: false }];
          patchAssistant((m) => ({ ...m, steps: [...(m.steps ?? []), { name: e.name, args: e.args, done: false }] }));
        } else if (e.type === "tool_result") {
          for (let i = asstSteps.length - 1; i >= 0; i--) {
            if (asstSteps[i].name === e.name && !asstSteps[i].done) { asstSteps[i] = { ...asstSteps[i], done: true, result: e.result }; break; }
          }
          patchAssistant((m) => {
            const steps = [...(m.steps ?? [])];
            for (let i = steps.length - 1; i >= 0; i--) {
              if (steps[i].name === e.name && !steps[i].done) { steps[i] = { ...steps[i], done: true, result: e.result }; break; }
            }
            return { ...m, steps };
          });
        } else if (e.type === "done") {
          if (e.outputTokens) asstTokens = e.outputTokens;
        } else if (e.type === "error") {
          throw new Error(e.message);
        }
      });
      // success — compute timing + token rate, persist
      const meta = computeMeta();
      patchAssistant((m) => ({ ...m, meta }));
      const finalMsgs: ChatMessage[] = [...history, {
        role: "assistant", content: asstContent,
        steps: asstSteps.length ? asstSteps : undefined,
        meta,
      }];
      saveCurrent(finalMsgs, sid);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") { /* user stopped */ }
      else {
        const msg = e instanceof Error ? e.message : String(e);
        // context window exceeded -> auto-start a fresh session, keep the old one saved
        if (/exceed.*context|context.*(size|window|length)|too many tokens/i.test(msg)) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            return last?.role === "assistant" && !last.content ? prev.slice(0, -1) : prev;
          });
          setMessages([]);
          setSessionId(null);
          onSessionId(null);
          setInput(text); // let them resend
          setNotice("Previous chat reached the model's context limit — started a new session. Your old chat is saved in history.");
          onSaved();
        } else if (asstContent) {
          // The stream errored *after* delivering an answer (e.g. the model
          // server closed the connection at the end). Keep the answer + its
          // timing/tokens so the turn isn't lost, and still surface the error.
          const meta = computeMeta();
          patchAssistant((m) => ({ ...m, meta }));
          saveCurrent([...history, {
            role: "assistant", content: asstContent,
            steps: asstSteps.length ? asstSteps : undefined, meta,
          }], sid);
          setError(msg);
        } else {
          // no content streamed — drop the empty bubble and show the error
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            return last?.role === "assistant" && !last.content ? prev.slice(0, -1) : prev;
          });
          setError(msg);
        }
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, pending, streaming, model, messages, system, temperature, searchOn, selectedAgent, agentId, sessionId, saveCurrent, onSessionId, onSaved]);

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const empty = messages.length === 0;

  // The composer is shared between the empty (centered) and active layouts.
  const composer = (
    <div className="rounded-[24px] border border-line bg-canvas p-2.5 shadow-[0_10px_30px_-16px_rgba(40,51,92,0.28)]">
      {/* attachments */}
      {pending.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2 px-1">
          {pending.map((p, i) => (
            <span key={i} className="flex items-center gap-1.5 rounded-full border border-pressed bg-softer px-2.5 py-1 text-xs text-body">
              <Paperclip />{p.name}
              <button onClick={() => setPending((arr) => arr.filter((_, j) => j !== i))} className="text-mute hover:text-ink">✕</button>
            </span>
          ))}
        </div>
      )}

      {showSystem && (
        <textarea
          value={system}
          onChange={(e) => setSystem(e.target.value)}
          placeholder="System prompt (optional)…"
          rows={2}
          className="mb-2 w-full resize-y rounded-2xl border border-pressed bg-softer px-3 py-2 font-mono text-xs text-ink outline-none placeholder:text-mute focus:border-primary-line"
        />
      )}

      <textarea
        ref={taRef}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKey}
        placeholder="Ask anything"
        rows={1}
        className="max-h-[200px] w-full resize-none bg-transparent px-3 py-2 text-[15px] text-body outline-none placeholder:text-mute"
      />

      {/* footer */}
      <div className="flex items-center gap-2 px-1">
        {/* attach menu */}
        <div className="relative">
          <button
            onClick={() => { setShowAttachMenu((v) => !v); setShowSettings(false); }}
            className="grid size-8 place-items-center rounded-full border border-pressed text-body hover:bg-soft hover:text-ink"
            title="Add"
          ><Plus /></button>
          {showAttachMenu && (
            <div className="absolute bottom-10 left-0 z-10 w-52 rounded-2xl border border-line bg-canvas p-1.5 shadow-lg">
              <button
                onClick={() => { fileRef.current?.click(); setShowAttachMenu(false); }}
                className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-body hover:bg-primary-soft hover:text-primary-deep"
              ><Paperclip /> Add photos &amp; files</button>
              <button
                onClick={() => { setShowSystem((v) => !v); setShowAttachMenu(false); }}
                className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-body hover:bg-primary-soft hover:text-primary-deep"
              ><Sliders /> System prompt {system ? "●" : ""}</button>
            </div>
          )}
        </div>

        {/* web search toggle */}
        <button
          onClick={() => setSearchOn((v) => !v)}
          title="Let the model search & read the web to answer"
          className={`flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors ${
            searchOn ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft hover:text-ink"
          }`}
        ><Globe /> Search</button>

        {/* settings (temperature) */}
        <div className="relative">
          <button
            onClick={() => { setShowSettings((v) => !v); setShowAttachMenu(false); }}
            className="flex h-8 items-center gap-1.5 rounded-full border border-pressed px-3 text-xs text-body hover:bg-soft hover:text-ink"
          ><Sliders /> temp {temperature.toFixed(1)}</button>
          {showSettings && (
            <div className="absolute bottom-10 left-0 z-10 w-60 rounded-2xl border border-line bg-canvas p-3 shadow-lg">
              <label className="flex items-center gap-2 text-xs text-body">
                Temperature
                <input type="range" min={0} max={2} step={0.1} value={temperature}
                  onChange={(e) => setTemperature(parseFloat(e.target.value))} className="flex-1 accent-primary" />
                <span className="w-6 text-right text-ink">{temperature.toFixed(1)}</span>
              </label>
            </div>
          )}
        </div>

        {/* active model — read-only (change it in the Models tab) */}
        <span
          className="ml-auto max-w-[220px] truncate rounded-full bg-soft px-3 py-1.5 font-mono text-xs text-mute"
          title={modelLabel ? `Active model: ${modelLabel}` : "No active model — activate one in the Models tab"}
        >{modelLabel || "no active model"}</span>

        {/* send / stop */}
        {streaming ? (
          <button onClick={stop} className="grid size-9 place-items-center rounded-full bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep" title="Stop"><Stop /></button>
        ) : (
          <button
            onClick={send}
            disabled={!input.trim() && pending.length === 0}
            className="grid size-9 place-items-center rounded-full bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-opacity hover:bg-primary-deep disabled:opacity-30"
            title="Send"
          ><Up /></button>
        )}
      </div>

      <input ref={fileRef} type="file" multiple hidden
        onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
    </div>
  );

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      {/* top bar */}
      <div className="flex items-center gap-3 border-b border-pressed bg-softer px-6 py-3">
        <h1 className="font-display text-lg font-semibold tracking-tight text-ink">Chat</h1>
        {agents.length > 0 && (
          <label className="flex items-center gap-1.5 rounded-full border border-pressed bg-soft px-2.5 py-1 text-xs text-mute" title="Run the chat as this agent (persona + tools + skills)">
            <span>as</span>
            <select
              value={agentId ?? ""}
              onChange={(e) => setAgentId(e.target.value || null)}
              className="rounded-full bg-transparent text-xs font-semibold text-ink outline-none"
            >
              {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
        )}
        {modelLabel && (
          <span className="ml-auto flex max-w-[40%] items-center gap-1.5 truncate rounded-full bg-success-soft px-3 py-1 font-mono text-xs text-success" title={modelLabel}>
            <span className="size-1.5 shrink-0 rounded-full bg-success" />
            <span className="truncate">{modelLabel}</span>
          </span>
        )}
      </div>

      {notice && (
        <div className="mx-4 mt-3 rounded-xl border border-primary-line bg-primary-soft px-3 py-2 text-xs text-primary-deep">{notice}</div>
      )}

      {empty ? (
        // Centered greeting + composer
        <div className="flex flex-1 flex-col items-center justify-center px-4">
          <div className="mb-6 text-center">
            <img src={appIcon} alt="" className="animate-float mx-auto mb-4 size-24 select-none" draggable={false} />
            <h2 className="font-display text-3xl font-semibold tracking-tight text-ink">{greeting()}</h2>
            <p className="mt-1 text-sm text-mute">
              {modelLabel ? (
                <>{selectedAgent ? <><span className="text-body">{selectedAgent.name}</span> · </> : null}
                  <span className="font-mono text-body">{modelLabel}</span></>
              ) : "Activate a model in Models to begin"}
            </p>
          </div>
          <div className="w-full max-w-2xl">
            {error && <p className="mb-2 rounded-xl border border-danger bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
            {composer}
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              {["Summarize a repo", "Write a unit test", "Explain this error"].map((s) => (
                <button
                  key={s}
                  onClick={() => setInput(s)}
                  className="rounded-full bg-soft px-3.5 py-1.5 text-xs font-medium text-body transition-colors hover:bg-primary-soft hover:text-primary-deep"
                >{s}</button>
              ))}
            </div>
          </div>
        </div>
      ) : (
        // Active thread
        <>
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
            <div className="mx-auto max-w-2xl space-y-5">
              {messages.map((m, i) => (
                <div key={i} className={`flex gap-2.5 ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  {m.role !== "user" && (
                    <img src={appIcon} alt="" className="mt-0.5 size-[30px] shrink-0 rounded-full" draggable={false} />
                  )}
                  <div className={`max-w-[88%] text-[15px] leading-relaxed ${
                    m.role === "user"
                      ? "rounded-[18px] rounded-br-md bg-primary px-4 py-2.5 text-white"
                      : "text-body"
                  }`}>
                    {m.images?.length ? (
                      <div className="mb-2 flex flex-wrap gap-2">
                        {m.images.map((src, k) => <img key={k} src={src} alt="" className="max-h-40 rounded-lg" />)}
                      </div>
                    ) : null}
                    {m.steps?.length ? (
                      <div className="mb-2 space-y-1 rounded-xl border border-pressed bg-softer p-2.5">
                        {m.steps.map((s, k) => (
                          <div key={k} className="flex items-center gap-1.5 text-xs text-body">
                            {s.done ? <span className="text-success">✓</span> : <Spinner />}
                            <span className="font-medium">{stepVerb(s.name)}</span>
                            <span className="truncate font-mono text-mute">{String(s.args.query ?? s.args.url ?? s.args.slug ?? s.args.path ?? s.args.name ?? "")}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {m.content ? <RichText text={m.content} /> : (!m.steps?.length && <span className="text-mute"><Spinner /></span>)}
                    {m.role === "assistant" && m.meta && (
                      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-mute">
                        <span>⏱ {(m.meta.ms / 1000).toFixed(1)}s</span>
                        {m.meta.tokSec ? <span>· {m.meta.tokSec} tok/s</span> : null}
                        {m.meta.tokens ? <span>· {m.meta.tokens} tokens</span> : null}
                        {m.meta.tools?.length ? (
                          <span className="rounded-full bg-primary-soft px-2 py-0.5 font-medium text-primary-deep">🧩 {m.meta.tools.join(", ")}</span>
                        ) : null}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="px-4 pb-4">
            <div className="mx-auto max-w-2xl">
              {error && <p className="mb-2 rounded-xl border border-danger bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
              {composer}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default PlaygroundPage;
