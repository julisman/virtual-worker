// Docs — in-app user guide for Virtual Worker. Monochrome (Uber-inspired) theme.
//
// Structure mirrors the platform Help page: sections are grouped into
// CATEGORIES (the left TOC). Each section's body lives in CONTENT keyed by id,
// so the TOC, the scroll-spy, and the rendered body stay in sync. A search box
// filters both the nav and the body by title + keywords + category, so every
// feature is findable by name.
//
// To document a new feature: add a SectionMeta to the right category and a
// matching entry to CONTENT (and optionally richer `keywords` for search).

import { useEffect, useMemo, useRef, useState } from "react";

import appLogo from "@/assets/logo.svg";

// --------------------------------------------------------------------------- //
// Icons (inline, stroke-based to inherit currentColor — matches App.tsx)
// --------------------------------------------------------------------------- //

type IconProps = { className?: string };
const I = (p: { children: React.ReactNode } & IconProps) => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={p.className}>
    {p.children}
  </svg>
);
const BookIcon = (p: IconProps) => <I {...p}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></I>;
const RocketIcon = (p: IconProps) => <I {...p}><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" /><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" /><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" /></I>;
const ChatIcon = (p: IconProps) => <I {...p}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></I>;
const ChannelsIcon = (p: IconProps) => <I {...p}><path d="M4 4h16v11H8l-4 4z" /><path d="M8 8h8M8 11h5" /></I>;
const AgentsIcon = (p: IconProps) => <I {...p}><rect x="5" y="8" width="14" height="11" rx="2" /><path d="M12 3v5M9 13h.01M15 13h.01" /></I>;
const SkillsIcon = (p: IconProps) => <I {...p}><path d="M14 2l1.5 4.5L20 8l-4.5 1.5L14 14l-1.5-4.5L8 8l4.5-1.5z" /><path d="M5 14l1 3 3 1-3 1-1 3-1-3-3-1 3-1z" /></I>;
const McpIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="2.5" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></I>;
const MemoryIcon = (p: IconProps) => <I {...p}><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" /><rect x="9" y="9" width="6" height="6" rx="1" /></I>;
const ClockIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></I>;
const HubIcon = (p: IconProps) => <I {...p}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></I>;
const ConnectIcon = (p: IconProps) => <I {...p}><path d="M8 12h8M8 12a4 4 0 1 1-4-4M16 12a4 4 0 1 0 4-4" /></I>;
const TokenIcon = (p: IconProps) => <I {...p}><circle cx="8" cy="12" r="4" /><path d="M11 12h9M17 12v4M20 12v3" /></I>;
const DebugIcon = (p: IconProps) => <I {...p}><path d="M4 6h16M4 12h16M4 18h10" /></I>;
const MonitorIcon = (p: IconProps) => <I {...p}><path d="M3 12h4l2 6 4-14 2 8h6" /></I>;
const GatewayIcon = (p: IconProps) => <I {...p}><path d="M4 4h16v6H4zM4 14h16v6H4z" /><path d="M8 7h.01M8 17h.01" /></I>;
const SettingsIcon = (p: IconProps) => <I {...p}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></I>;
const ArchIcon = (p: IconProps) => <I {...p}><path d="M3 21h18M5 21V7l7-4 7 4v14M9 21v-6h6v6" /></I>;
const WrenchIcon = (p: IconProps) => <I {...p}><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.1 2.1-2.4-.6-.6-2.4z" /></I>;
const SearchIcon = (p: IconProps) => <I {...p}><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></I>;
const XIcon = (p: IconProps) => <I {...p}><path d="M18 6 6 18M6 6l12 12" /></I>;

// --------------------------------------------------------------------------- //
// Section + content structure
// --------------------------------------------------------------------------- //

interface SectionMeta {
  id: string;
  title: string;
  icon: (p: IconProps) => React.ReactNode;
  /** Extra search terms (synonyms, UI labels) beyond the title. */
  keywords?: string;
}
interface Category { title: string; sections: SectionMeta[]; }

const CATEGORIES: Category[] = [
  {
    title: "Get Started",
    sections: [
      { id: "overview", title: "What is Virtual Worker", icon: BookIcon, keywords: "virtual worker supply chain intro overview local llm studio offline serve debug finetune monochrome" },
      { id: "quickstart", title: "Quick Start", icon: RocketIcon, keywords: "run dev.sh vw start stop status logs run install-service service daemon setup install bootstrap uv node homebrew llama.cpp first model activate ports 5173 8888 8080" },
      { id: "architecture", title: "How It Works", icon: ArchIcon, keywords: "architecture gateway backend fastapi frontend react upstream model server data app.db managed mode" },
    ],
  },
  {
    title: "Chat & Agents",
    sections: [
      { id: "chat", title: "Chat", icon: ChatIcon, keywords: "playground streaming model picker system prompt temperature attachments files images sessions history search web tool loop" },
      { id: "agents", title: "Agents", icon: AgentsIcon, keywords: "persona system prompt model create edit delete chat with agent roster sidebar intern worker" },
      { id: "providers", title: "External Models", icon: GatewayIcon, keywords: "external provider byok subscription deepseek openai chatgpt anthropic claude api key oauth sign in codex paid remote model per-agent gpt-5 claude-sonnet" },
      { id: "skills", title: "Skills", icon: SkillsIcon, keywords: "skillpack skill.md procedural knowledge enable disable load agentskills author create factory meta skill-creator" },
      { id: "mcp", title: "MCP Servers", icon: McpIcon, keywords: "model context protocol tools servers stdio sse connect external tools" },
      { id: "memory", title: "Memory", icon: MemoryIcon, keywords: "agent memory remember facts persistent recall context" },
      { id: "wiki", title: "Knowledge Wiki", icon: BookIcon, keywords: "wiki okf open knowledge format markdown pages ingest query lint orphans broken links stale graph export import compounding knowledge base karpathy wiki_search wiki_write wiki_read wiki_lint" },
      { id: "schedule", title: "Schedules", icon: ClockIcon, keywords: "cron scheduled task timer recurring daily weekly hourly one-time at 2pm 5pm reminder summary autonomous fire run now next run" },
    ],
  },
  {
    title: "Channels",
    sections: [
      { id: "deliveries", title: "Deliveries", icon: ChannelsIcon, keywords: "deliveries audit log sent failed dropped outbound email webhook desktop notification obsidian schedule result deliver tool retention" },
      { id: "channels", title: "Chat Channels", icon: ChannelsIcon, keywords: "whatsapp telegram botfather bot token discord gateway slack socket mode channel pair qr code linked device cloud api webhook dm policy pairing allowlist group mention binding route conversation channel_send" },
    ],
  },
  {
    title: "Models",
    sections: [
      { id: "models", title: "Models (Hub)", icon: HubIcon, keywords: "huggingface unsloth discover download gguf mlx fits my device on device activate cache parallel chunked resume cancel hf_token gated" },
      { id: "engines", title: "Inference Engines", icon: WrenchIcon, keywords: "llama.cpp llama-server mlx mlx_lm mlx_vlm vision managed mode upstream apple silicon ngl ctx port" },
    ],
  },
  {
    title: "Connect & Serve",
    sections: [
      { id: "connect", title: "Connect Agents", icon: ConnectIcon, keywords: "claude code codex antigravity gemini anthropic messages responses generatecontent gateway endpoints copy paste setup offline" },
      { id: "channel", title: "chat channel", icon: ConnectIcon, keywords: "channel chat bot message dm group websocket relay webhook event callback app_id app_secret signing secret laptop no public host dial out reply integration" },
      { id: "gateway", title: "Gateway & Proxy", icon: GatewayIcon, keywords: "openai compatible v1 chat completions embeddings models translate protocol url path streaming tool calls model map default model upstream" },
      { id: "tokens", title: "API Tokens", icon: TokenIcon, keywords: "sk-vw token key auth x-api-key bearer expire ttl revoke hash gateway key" },
    ],
  },
  {
    title: "Operate",
    sections: [
      { id: "monitoring", title: "Monitoring", icon: MonitorIcon, keywords: "memory ram vram unified usage cpu load disk free model process rss uptime runtime health error log inference server resource pressure oom killed" },
      { id: "debug", title: "API Debugging", icon: DebugIcon, keywords: "trace api calls request response tool calls tokens cost latency errors session chat anthropic capture app.db middleware" },
      { id: "settings", title: "Settings", icon: SettingsIcon, keywords: "engine versions update preferences config settings.json active model" },
      { id: "troubleshooting", title: "Troubleshooting", icon: WrenchIcon, keywords: "help problem model won't load download stuck gateway 401 mlx not installed reset support faq" },
    ],
  },
];

// Flat list for content rendering, scroll-spy, and search.
const SECTIONS: (SectionMeta & { category: string })[] = CATEGORIES.flatMap((c) =>
  c.sections.map((s) => ({ ...s, category: c.title })),
);

// --------------------------------------------------------------------------- //
// Small presentational helpers (monochrome)
// --------------------------------------------------------------------------- //

function Section({ id, title, icon: Icon, children }: SectionMeta & { children: React.ReactNode }) {
  return (
    <section id={id} className="scroll-mt-6">
      <div className="rounded-2xl border border-line bg-canvas p-6 sm:p-7">
        <div className="mb-4 flex items-center gap-3">
          <div className="font-display flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary-soft text-primary">
            <Icon />
          </div>
          <h2 className="font-display text-lg font-bold tracking-tight text-ink">{title}</h2>
        </div>
        <div className="space-y-4 text-sm leading-relaxed text-body">{children}</div>
      </div>
    </section>
  );
}

function P({ children }: { children: React.ReactNode }) {
  return <p className="text-body">{children}</p>;
}
function Term({ children }: { children: React.ReactNode }) {
  return <span className="font-semibold text-ink">{children}</span>;
}
function Code({ children }: { children: React.ReactNode }) {
  return <code className="rounded-md bg-soft px-1.5 py-0.5 font-mono text-[12px] text-ink">{children}</code>;
}
function Pre({ children }: { children: string }) {
  return <pre className="overflow-x-auto rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink whitespace-pre">{children}</pre>;
}
function Steps({ items }: { items: React.ReactNode[] }) {
  return (
    <ol className="space-y-2">
      {items.map((it, i) => (
        <li key={i} className="flex gap-3">
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-white">{i + 1}</span>
          <span className="text-body">{it}</span>
        </li>
      ))}
    </ol>
  );
}
function Bullets({ items }: { items: React.ReactNode[] }) {
  return (
    <ul className="space-y-1.5">
      {items.map((it, i) => (
        <li key={i} className="flex gap-2.5">
          <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-hairline" />
          <span className="text-body">{it}</span>
        </li>
      ))}
    </ul>
  );
}
function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-pressed bg-softer px-3.5 py-2.5 text-[13px] text-body">
      <span className="font-semibold text-ink">Tip — </span>{children}
    </div>
  );
}
/** Two-column key/value table — used for env vars and endpoint maps. */
function KV({ rows }: { rows: [React.ReactNode, React.ReactNode][] }) {
  return (
    <div className="overflow-hidden rounded-xl border border-pressed">
      <table className="w-full text-[12px]">
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={i % 2 ? "bg-base" : "bg-canvas"}>
              <td className="w-1/3 border-b border-pressed px-3 py-2 align-top font-mono text-ink">{r[0]}</td>
              <td className="border-b border-pressed px-3 py-2 align-top text-body">{r[1]}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Content — keyed by section id (CONTENT is defined below the page for length)
// --------------------------------------------------------------------------- //

const CONTENT: Record<string, React.ReactNode> = {
  // ─── Get Started ──────────────────────────────────────────────────────────
  overview: (
    <>
      <P><Term>Virtual Worker</Term> is a local-first platform for running autonomous AI workers on your own machine: agents with a persona, tools, skills, memory, a task board and schedules, served by a local model by default and reachable from the chat apps you already use. It runs a single React app on top of a FastAPI backend that acts as an OpenAI-compatible gateway in front of any model server (llama.cpp, MLX, vLLM, or a remote provider).</P>
      <P>Everything is local by default — your chats, agents, memory, downloaded models, and API traces all stay on your machine. Nothing leaves it unless you explicitly use web search, a remote MCP server, or a remote upstream.</P>
      <P>Use the left sidebar to move between the workspaces:</P>
      <Bullets items={[
        <><Term>Chat</Term> — talk to the active model, as a plain assistant or as one of your agents.</>,
        <><Term>Agents</Term> — build personas with their own tools, skills, memory, and (optionally) an autonomous task board. Includes the <Term>Skills</Term> and <Term>MCP</Term> sub-tabs.</>,
        <><Term>Models</Term> — discover, download, and activate local models from Hugging Face.</>,
        <><Term>Connect</Term> — point coding agents (Claude Code, Codex, Gemini) at your local model.</>,
        <><Term>Debug</Term> — inspect every LLM API call: request, response, tokens, cost, latency, tool calls.</>,
        <><Term>Settings</Term> — keep your inference engines up to date.</>,
      ]} />
      <Tip>Where to start: open <Term>Models</Term>, download a model, click <Term>Activate</Term>, then head to <Term>Chat</Term>.</Tip>
    </>
  ),
  quickstart: (
    <>
      <P>From a fresh clone, the launcher bootstraps everything on first run (backend virtualenv + deps, frontend deps, llama.cpp via Homebrew, and <Code>mlx-lm</Code> / <Code>mlx-vlm</Code> on Apple Silicon), then starts the backend and frontend together:</P>
      <Pre>{`./dev.sh                      # hot-reload dev profile
#   web   → http://localhost:5173
#   api   → http://127.0.0.1:8889   (docs: /docs)
#   llama → http://127.0.0.1:8081/v1   (backend-managed)

./vw start                    # always-on service (built UI + API on one port)
#   app   → http://127.0.0.1:8888   (model server on :8080)
./vw status · ./vw logs -f · ./vw restart · ./vw stop
./vw install-service          # start at login/boot (launchd / systemd), keep alive
./vw run "Support Rep" "summarize today's tickets"   # headless one-shot run`}</Pre>
      <P><Term>Service mode</Term> is how you run a worker that answers on WhatsApp, Telegram, Discord or Slack all day: the backend runs detached with automatic restarts and keeps running after you close the terminal. Dev and service use different default ports so both can run at once.</P>
      <P>Prerequisites it can't install for you — it prints the exact command if any is missing:</P>
      <Bullets items={[
        <><Term>uv</Term> (Python toolchain), <Term>Node / npm</Term>, and <Term>Homebrew</Term> (for llama.cpp).</>,
        <>Run <Code>./setup.sh</Code> any time to (re)bootstrap — it's idempotent.</>,
      ]} />
      <P><Term>First launch</Term> — the web UI opens a guided setup: pick a model (download a recommended one for this machine, use one already on disk, or add a provider key), deploy a starter worker from a template, and optionally connect Telegram or Discord. Skip it any time; re-run it from <Term>Settings → First-run setup</Term>.</P>
      <P><Term>First model by hand:</Term></P>
      <Steps items={[
        <>Open <Term>Models</Term> → <Term>Discover</Term> and search (e.g. a small Unsloth GGUF or MLX model).</>,
        <>Pick a file that shows <Term>Fits</Term> for your device and click <Term>Download</Term> — progress shows live and resumes if interrupted.</>,
        <>Switch to <Term>On Device</Term> and click <Term>Activate</Term>. Each model's context size defaults to the largest that <Term>likely fits your machine</Term> (up to the model's trained max) — adjust it per model from the dropdown next to <Term>Activate</Term> if you want more or less.</>,
        <>Go to <Term>Chat</Term> and start talking. The header shows the active model.</>,
      ]} />
      <Tip>Skip the managed server entirely by pointing Virtual Worker at your own: set <Code>VW_UPSTREAM_URL=http://127.0.0.1:11434</Code> (Ollama) or any OpenAI-compatible URL.</Tip>
    </>
  ),
  architecture: (
    <>
      <P>Virtual Worker is three local processes that <Code>./dev.sh</Code> starts together: the <Term>frontend</Term> (React + Vite), the <Term>backend</Term> (FastAPI on <Code>:8889</Code> in dev, <Code>:8888</Code> as a service), and an <Term>upstream model server</Term> (llama.cpp / MLX on <Code>:8080</Code> by default).</P>
      <Pre>{`Browser (:5173)
  React app — sidebar nav + feature pages
        │  Vite proxies /api, /v1, /v1beta → :8888
        ▼
Backend — FastAPI (:8888)
  ApiDebugMiddleware  ── tees a bounded copy of every LLM call → app.db
  /v1/* /v1beta/*     ── OpenAI · Anthropic · Responses · Gemini gateway
  /api/*              ── chat, hub, runtime, agents, finetune, tokens …
        │  VW_UPSTREAM_URL (default :8080)
        ▼
Upstream model server — llama.cpp / MLX / vLLM / Ollama / remote API`}</Pre>
      <P><Term>Managed mode</Term> (the default) means the backend itself starts, stops, and switches the inference server when you activate a model — so there's one server at a time and it survives backend reloads. Set <Code>VW_MANAGED_LLAMA=0</Code> (or point <Code>VW_UPSTREAM_URL</Code> at your own server) to manage it yourself.</P>
      <P><Term>Where local state lives</Term> — under <Code>~/.virtual-worker/</Code> (override with <Code>VW_HOME</Code>):</P>
      <KV rows={[
        [<>app.db</>, "SQLite — chat sessions, agents, memory, tasks/activity, API tokens, and the API-debug traces."],
        [<>settings.json</>, "App settings, including the persisted active model."],
        [<>skills/&lt;slug&gt;/</>, "Skill packs (SKILL.md + bundled resources)."],
        [<>~/.cache/huggingface</>, "Downloaded base models (the real HF cache, shared with other tools)."],
      ]} />
    </>
  ),

  // ─── Chat & Agents ────────────────────────────────────────────────────────
  chat: (
    <>
      <P><Term>Chat</Term> is the main place to talk to the active model — streaming, with a model badge, system prompt, temperature, attachments, and an optional web-search loop. Every chat runs <Term>as an agent</Term>; with none selected it uses the built-in <Term>Assistant</Term>.</P>
      <P><Term>Composer controls:</Term></P>
      <Bullets items={[
        <><Term>Send / Stop</Term> — Enter sends, Shift+Enter is a newline; the square button aborts a running turn.</>,
        <><Term>+ (Plus)</Term> — attach photos &amp; files, or reveal a per-chat <Term>System prompt</Term> box (appended to the agent's persona).</>,
        <><Term>Sliders</Term> — set <Term>temperature</Term> (0.0 deterministic → 2.0 very creative; default 0.7 or the agent's value).</>,
        <><Term>🌐 Search</Term> — when on, grants <Code>browser_search</Code> + <Code>browser_fetch</Code> for that turn so the model can look things up; tool steps show inline. Search runs through a self-hosted <Term>SearXNG</Term> that Virtual Worker auto-starts (no API key) — its health shows on the Monitor page.</>,
        <><Term>as [Agent]</Term> — switch which agent (persona + tools + memory) runs the chat.</>,
        <><Term>/ commands</Term> — type <Code>/</Code> for built-in commands and the agent's skills. <Code>/compact</Code> summarizes older turns into a short recap so the chat keeps fitting the model's window; <Code>/clear</Code> starts a fresh session (the old one stays saved).</>,
        <><Term>Context meter</Term> — a chip by the composer shows how full the window is (<Code>~tokens / window · %</Code>), turning amber past 70% and red past 90%. Near <Term>80%</Term> Virtual Worker <Term>auto-compacts</Term> the older turns before your next send so you don't hit the limit; a summary card marks where it folded.</>,
      ]} />
      <P><Term>Attachments:</Term> images are sent as inline vision content (needs a vision-capable model); text files are inlined into your message under a <Code>----- filename -----</Code> header. Files are read in the browser — nothing is uploaded to a server.</P>
      <P><Term>Sessions &amp; history:</Term> every turn auto-saves locally. Use <Term>+ New chat</Term> in the sidebar to start fresh; past conversations live on each agent's <Term>Conversations</Term> tab (open the agent → Conversations → click one to reopen). The title comes from your first message. Below each reply you'll see response time, tokens/sec, total tokens, and which tools ran.</P>
      <Tip>If a conversation exceeds the model's context window, Virtual Worker automatically starts a fresh session (the old one stays saved) and keeps your last message in the box so you can resend it.</Tip>
      <P>The active model is read-only here — switch it from <Term>Models</Term>. The badge polls every few seconds and updates on its own.</P>
      <P><Term>Feedback</Term>: under every reply there is a thumbs up, a thumbs down and a pencil. Rate a reply or rewrite it into the answer you wanted. The signals are listed under <Term>Analytics → feedback</Term>, where you can review, delete, and export the up-rated and corrected turns as a chat-format JSONL file for fine-tuning with any trainer. Down-rated turns stay out of the export.</P>
    </>
  ),
  agents: (
    <>
      <P><Term>Agents</Term> are local personas you create once and reuse. An agent bundles a persona (its system prompt), a model + sampling settings, a set of tools, attached skills and MCP servers, and its own persistent memory. Give it a <Term>Job role</Term> and it also gains an autonomous task board.</P>
      <P><Term>Create one</Term> from the Agents tab (sidebar sub-menu → <Term>+ New agent</Term>) and fill in:</P>
      <Bullets items={[
        <><Term>Name</Term> &amp; <Term>Description</Term> — shown in the roster.</>,
        <><Term>Persona / system prompt</Term> — used verbatim as the system prompt when you chat as this agent.</>,
        <><Term>Job role</Term> (optional) — e.g. "Software Engineering Intern". Leaving it blank makes a plain assistant; setting it unlocks the <Term>task board, activity feed, and shell access</Term> (see Memory/Tasks below).</>,
        <><Term>Model</Term> — run on the local active model (default) or an external provider via BYOK / subscription. See <Term>External Models</Term> below.</>,
        <><Term>Temperature / top_p / top_k</Term> — sampling controls; blanks fall back to the model defaults. top_k is hidden for external providers (it's local-only).</>,
        <><Term>Tight context (faster)</Term> — caps tool outputs and trims history harder, so each turn re-reads less context. Noticeably faster on local models and MCP-heavy chats; the trade-off is less context carried per turn.</>,
        <><Term>Files</Term> — set a working folder to grant scoped file tools (<Code>read_file</Code>, <Code>write_file</Code>, <Code>edit_file</Code>, <Code>list_dir</Code>, <Code>glob</Code>, <Code>grep</Code>), confined to that folder.</>,
        <><Term>Tools / Skills / MCP servers</Term> — toggle web tools, attach skill packs, and connect MCP servers.</>,
        <><Term>Browser control</Term> — the <Code>browser_open</Code> / <Code>browser_snapshot</Code> / <Code>browser_click</Code> / <Code>browser_type</Code> / <Code>browser_read</Code> tools drive a <Term>real, visible</Term> browser with a <Term>persistent profile</Term>: log into a site once in the window and that session sticks across runs, so a skill can then click and type through it. Pop the window open to sign in from the agent's page ("Open browser to log in"), or just ask the agent to <Code>browser_open</Code> a URL. Separate from the headless <Code>browser_fetch</Code>; opt-in per agent.</>,
        <><Term>Guardrails</Term> (interns) — shell mode (auto / readonly / workspace / off), verify-finished-tasks, max steps per task, daily task cap, extra blocked commands, and an operator allow-list of commands that may run even when the deny-list would refuse them.</>,
      ]} />
      <P><Term>Detail tabs:</Term> an agent opens to tabs for <Term>Overview</Term>, <Term>Board</Term>, <Term>Activity</Term>, <Term>Schedule</Term>, <Term>Triggers</Term>, <Term>Conversations</Term>, and <Term>Settings</Term>. <Term>Conversations</Term> lists every chat you've run as that agent (newest first) — click one to reopen it in Chat. (There's no global chat-history list in the sidebar; history lives per-agent here.)</P>
      <P><Term>Tasks (board):</Term> each task has a <Term>title</Term>, a <Term>description</Term>, and a <Term>definition of done</Term> (the acceptance criteria). Set them in the board's <Term>+ New task</Term> form, or have an agent file them in chat with the <Code>task_create</Code> tool. When the agent works a task it must satisfy every criterion, and the <Term>Verify</Term> pass checks the result against them before the task counts as done.</P>
      <P><Term>Using an agent:</Term> open it and click <Term>Chat with this agent →</Term> (starts a fresh chat as that agent), or pick it from the <Term>as […]</Term> selector in Chat. Its persona, model, sampling, tools, skills, and memory all come along.</P>
      <P><Term>Plain chat vs. agent:</Term> plain chat uses the default Assistant with no persistent memory and basic tools; a custom agent has a distinct identity, memory that carries across sessions, optional file/shell access, skills/MCP, and — with a role — autonomous work.</P>
    </>
  ),
  providers: (
    <>
      <P>By default every agent runs on your machine's <Term>active local model</Term> — nothing leaves the device. When you need a frontier model, an individual agent can be pointed at an external provider instead. This is the only thing that causes network egress, and it's always explicit and per-agent.</P>
      <P>In the agent editor's <Term>Model</Term> card, pick a source:</P>
      <Bullets items={[
        <><Term>Local</Term> (default) — the active model from the <Term>Models</Term> tab.</>,
        <><Term>BYOK</Term> (bring your own key) — paste a provider API key. <Term>DeepSeek</Term> is first-class (<Code>deepseek-chat</Code>, <Code>deepseek-reasoner</Code>); OpenAI and Anthropic keys work too.</>,
        <><Term>Subscription</Term> — sign in with a paid <Term>ChatGPT</Term> plan (Plus/Pro), no API key. (Claude subscription is intentionally not offered — see below.)</>,
      ]} />
      <P><Term>Credentials are shared, not per-agent.</Term> A key or sign-in is stored once (in <Code>~/.virtual-worker/settings.json</Code>) and reused by every agent that selects that provider. Secrets are write-only — the UI only shows whether one is set, never the value.</P>
      <P><Term>ChatGPT subscription</Term> — click <Term>Sign in with ChatGPT</Term>; a browser tab opens and the loopback finishes the login automatically. Calls go through the Codex backend, so use a current Codex model id (e.g. <Code>gpt-5.4</Code>).</P>
      <P><Term>Claude — BYOK only.</Term> Use an Anthropic API key (<Code>sk-ant-…</Code> from console.anthropic.com) under <Term>BYOK</Term> with a Claude model id (e.g. <Code>claude-sonnet-4-6</Code>). Claude <Term>subscription</Term> sign-in is intentionally not offered: Anthropic's terms prohibit using a Pro/Max subscription token in third-party apps (it gets detected, rate-limited, and can put your account at risk), so Virtual Worker supports Claude only via a proper API key.</P>
      <P><Term>BYOK</Term> — choose the provider, enter the model id, paste the key, and Save. That's it.</P>
      <Tip>The chat header shows the agent's effective model (e.g. <Code>gpt-5.4 · ChatGPT</Code>) so you can tell at a glance which provider a reply came from. The <Term>Debug</Term> tab tags each run with its provider and model.</Tip>
      <P>Model ids rotate (especially Codex/Claude), so the dropdown entries are just suggestions — free-text is always accepted. Provider errors (bad key, retired model, rate limit) surface verbatim in the chat.</P>
    </>
  ),
  skills: (
    <>
      <P><Term>Skills</Term> are procedural knowledge packs in the agentskills.io <Code>SKILL.md</Code> format — a markdown file (with optional YAML frontmatter) that teaches an agent how to do something. They use <Term>progressive disclosure</Term>, so they don't bloat the system prompt:</P>
      <Bullets items={[
        <><Term>L1 (catalog)</Term> — only the name, description, and "when to use" go into the system prompt, so the model knows the skill exists.</>,
        <><Term>L2 (body)</Term> — the agent calls <Code>load_skill</Code> to pull the full instructions only when it's relevant.</>,
        <><Term>L3 (resources)</Term> — bundled files (references, templates, scripts) load on demand via <Code>load_skill_resource</Code>.</>,
      ]} />
      <P><Term>Add a skill</Term> from the Agents → <Term>Skills</Term> tab: drag-and-drop (or choose) a <Code>.md</Code> file or a <Code>.zip</Code> bundle containing <Code>SKILL.md</Code> plus any resource files. A simple skill looks like:</P>
      <Pre>{`---
name: My Skill
description: What this skill teaches
when_to_use: When the user asks for …
version: 1.0.0
---

## Instructions
Step-by-step markdown the agent follows…`}</Pre>
      <P>Skills are stored on disk under <Code>~/.virtual-worker/skills/&lt;slug&gt;/</Code>. <Term>Enable</Term> a skill on an agent by toggling it in the agent's <Term>Skills</Term> section — only attached skills appear in that agent's catalog.</P>
      <P><Term>Skill factory (let an agent write its own skills).</Term> Turn on <Term>Skill authoring</Term> in an agent's <Term>Guardrails</Term> and it gains <Code>create_skill</Code>, <Code>update_skill</Code>, and <Code>list_all_skills</Code> tools — so it can capture a repeatable procedure as a new <Code>SKILL.md</Code> pack at runtime, saved to your library like any upload. It's <Term>off by default</Term>: authored skills go into the shared library (they aren't auto-attached), and you review/attach them from the Agents → Skills tab. Attach the bundled <Code>skill-creator</Code> meta-skill to that agent for spec-guided authoring — it teaches the format and hands over the spec + a worked example on demand.</P>
      <Tip>Skill authoring is self-modifying — enable it only on a trusted "author" or "librarian" agent, then attach the good results to the agents that need them.</Tip>
    </>
  ),
  mcp: (
    <>
      <P><Term>MCP servers</Term> (Model Context Protocol) are external tool providers you can plug into an agent. Their tools appear to the model as <Code>mcp__&lt;server&gt;__&lt;tool&gt;</Code> and are called during the chat loop like any other tool.</P>
      <P>Two transports, configured in Agents → <Term>MCP</Term>:</P>
      <Bullets items={[
        <><Term>stdio (local command)</Term> — the backend spawns a command on this machine (e.g. <Code>npx -y @modelcontextprotocol/server-filesystem /path</Code>). Set the name, command, arguments, and any environment variables. Everything stays local.</>,
        <><Term>http (remote URL)</Term> — connect to a streamable-HTTP MCP endpoint. Traffic leaves your machine.</>,
      ]} />
      <P><Term>Use it:</Term></P>
      <Steps items={[
        <>Click <Term>Add MCP server</Term>, choose the transport, fill in the fields, and save.</>,
        <>Click <Term>Test</Term> to connect and list the server's tools (verifies it works).</>,
        <><Term>Enable</Term> the server, then attach it on an agent under <Term>MCP servers</Term>.</>,
      ]} />
      <P>When that agent chats, Virtual Worker opens a session per enabled server, discovers its tools, and adds them to the registry; sessions close when the chat ends. MCP support needs the optional <Code>mcp</Code> Python package — run <Code>./setup.sh</Code> to install it (agents still work without it, just with no MCP tools).</P>
    </>
  ),
  memory: (
    <>
      <P><Term>Memory</Term> is a per-agent set of durable notes that survives across chats and model switches — it's always on. The agent writes facts it learns about you, your projects, and your preferences, and recalls them later.</P>
      <P>On every turn, the agent's most recent notes (up to ~25, ~3500 chars) are injected into its system prompt as established context. Older notes aren't shown but stay searchable. The agent manages memory with three tools:</P>
      <KV rows={[
        [<>memory_write</>, "Save a fact (with optional comma-separated tags). Identical content bumps the timestamp instead of duplicating."],
        [<>memory_search</>, "Find older notes by substring in content or tags."],
        [<>memory_forget</>, "Delete a note that's no longer true."],
      ]} />
      <P><Term>You can edit it too:</Term> in the agent editor's <Term>Memory</Term> section, read the notes (newest first), add your own, or delete any. Notes live per-agent in <Code>app.db</Code>.</P>
      <Tip>Memory is one of Virtual Worker's pillars — pair it with a working folder and skills so an agent accumulates real context about a project over time.</Tip>
    </>
  ),

  wiki: (
    <>
      <P>A <Term>Knowledge Wiki</Term> is a compounding, agent-maintained knowledge base written in the <Term>Open Knowledge Format (OKF)</Term> — plain markdown pages with YAML frontmatter, cross-linked with <Code>[[wikilinks]]</Code>, stored as real files on disk under <Code>~/.virtual-worker/wikis/</Code>. Unlike a <Term>Knowledge Base</Term> (which retrieves verbatim passages from your documents), a wiki is where an agent <Term>synthesizes</Term> what it reads into cross-referenced pages that get richer over time.</P>
      <Steps items={[
        <>Open the <Term>Wiki</Term> tab → <Term>+ New</Term> (or <Term>Import</Term> a <Code>.zip</Code> bundle exported earlier).</>,
        <>Attach it to an agent: <Term>Agents → (agent) → Wiki</Term> card → toggle it on. That grants the agent the <Code>wiki_*</Code> tools.</>,
        <>In chat, tell the agent to <Term>ingest</Term>: e.g. "read https://… and add it to your wiki." It fetches the page (<Code>browser_fetch</Code>), finds related pages, and writes/updates them.</>,
        <>Ask it questions: "what do we know about X?" — it searches the wiki and answers, citing the pages it used.</>,
      ]} />
      <P>The agent maintains the wiki with these tools:</P>
      <KV rows={[
        [<>wiki_search</>, "Find the pages relevant to a query (ranked, with a snippet)."],
        [<>wiki_read</>, "Read one page's full markdown (frontmatter + body)."],
        [<>wiki_write</>, "Create or update a page — kept OKF-conformant (a type + timestamp are added if omitted) and logged."],
        [<>wiki_list</>, "List the pages that already exist."],
        [<>wiki_lint</>, "Health-check: orphaned pages, broken [[links]], and stale pages to repair."],
      ]} />
      <P><Term>In the Wiki tab</Term> you can browse pages, read the <Code>log.md</Code> history, run a test search, <Term>Lint</Term> (see orphans / broken links / stale pages), view the link <Term>Graph</Term>, and <Term>Export</Term> the whole bundle as a zip (it's just files, so it round-trips through git or any editor too).</P>
      <P><Term>Ingest a source once:</Term> the agent reads it, folds it into pages, and links them — so the cross-references and contradictions are already there next time you ask. You can also schedule a periodic "lint your wiki and fix issues" run from the agent's <Term>Schedule</Term> tab.</P>
      <Tip>Wiki vs. Knowledge Base: use a <Term>Knowledge Base</Term> for verbatim recall over a fixed document set; use a <Term>Wiki</Term> when you want the agent to learn and remember a topic over time. A KB can even be an ingest source for a wiki.</Tip>
    </>
  ),

  schedule: (
    <>
      <P><Term>Schedules</Term> let an agent run a prompt at a time — a 2pm daily summary, a weekday 9am check-in, a one-off reminder. Open an agent and go to its <Term>Schedule</Term> tab. Times use your computer's local time.</P>
      <Steps items={[
        <>Give it a <Term>title</Term> and a <Term>prompt</Term> — what the agent should do when it fires.</>,
        <>Pick <Term>when</Term>: Every day / Weekdays / Weekly / Hourly (with a time), <Term>One-time</Term> (a date &amp; time), or <Term>Custom cron</Term> (a raw 5-field expression like <Code>0 14 * * *</Code>).</>,
        <>Click <Term>Add schedule</Term>. Each row shows its timing, next run, last run + status, and a result snippet.</>,
      ]} />
      <P>Toggle a schedule on/off with the dot, <Term>Run now</Term> to fire it immediately, or ✕ to delete. A recurring schedule keeps going until you disable it; a one-time schedule disables itself after it runs.</P>
      <P><Term>What a fired schedule does:</Term> it runs your prompt through the agent exactly like a chat turn — same model, tools, skills, and memory — and records the result, which also shows in the agent's <Term>Activity</Term> feed. The output is <Term>not</Term> auto-sent anywhere, so if the task is to message or email someone, tell the agent to use a tool that can (e.g. an MCP server) in the prompt.</P>
      <P>Agents can also manage their own schedules in chat via the <Code>schedule_create</Code>, <Code>schedule_list</Code>, and <Code>schedule_delete</Code> tools — just ask, e.g. "every weekday at 9am, summarize my open tasks."</P>
      <Tip>The scheduler runs whenever the backend is up. If your machine was asleep at the scheduled minute, the run fires once on the next tick (catch-up) rather than being skipped.</Tip>
    </>
  ),

  // ─── Channels ─────────────────────────────────────────────────────────────
  channels: (
    <>
      <P>A <Term>channel</Term> connects a chat app to your agents. Someone messages the connected account, the bound agent runs a normal turn (persona, tools, skills, memory), and the reply goes back into the same chat. Channels <Term>dial out</Term>, so your machine is never exposed; every reply is recorded under <Term>Deliveries</Term> and each turn appears in the agent's Activity and in Debug.</P>
      <P><Term>WhatsApp (linked device)</Term> — the default. It links a number the way WhatsApp Web does:</P>
      <Steps items={[
        <>Open <Term>Channels → Connect a channel</Term>, pick <Term>WhatsApp (linked device)</Term>, choose the default agent, and click <Term>Connect</Term>.</>,
        <>Click <Term>Pair</Term>. On the phone: <Term>Settings → Linked devices → Link a device</Term>, then scan the QR (or enter the pairing code if you typed a phone number).</>,
        <>When the badge shows <Term>Connected</Term>, send yourself a test message or just write to the number from another phone.</>,
      ]} />
      <P>This transport uses an unofficial, open-source client library (a small Node sidecar the backend manages). Use a dedicated number and accept WhatsApp's terms. If the card says the bridge isn't installed, run <Code>npm install</Code> inside <Code>backend/channels/whatsapp/bridge</Code> (setup.sh does this for you).</P>
      <P><Term>Telegram</Term> — message <Term>@BotFather</Term> in Telegram, send <Code>/newbot</Code>, copy the token, then <Term>Channels → Connect a channel → Telegram</Term> and paste it. The bot long-polls the Bot API, so no public URL is needed. In groups, add the bot and either @mention it or reply to one of its messages (Telegram's privacy mode hides other messages unless the bot is a group admin).</P>
      <P><Term>Discord</Term> — in the Developer Portal create an application, add a bot, enable the <Term>Message Content</Term> intent, invite the bot to your server, then paste the bot token under <Term>Channels → Connect a channel → Discord</Term>. The bot answers DMs and, in servers, replies when @mentioned (or when someone replies to it).</P>
      <P><Term>Slack</Term> — create an app, enable <Term>Socket Mode</Term> (this creates the app-level token), subscribe to <Code>message.im</Code>, <Code>message.channels</Code> and <Code>app_mention</Code>, grant <Code>chat:write</Code>, <Code>app_mentions:read</Code>, <Code>im:history</Code>, <Code>channels:history</Code> and <Code>files:read</Code>, install it, and paste the app-level and bot tokens. Replies in channels land in the thread of the message they answer.</P>
      <P><Term>WhatsApp (Cloud API)</Term> — the official Business Platform. You need a Meta app, a business phone number id, an access token, the app secret, and a <Term>public HTTPS URL</Term>. Paste the webhook URL shown on the card into Meta's webhook settings with your verify token; signatures are checked on every event.</P>
      <P><Term>Who may talk to the agent</Term></P>
      <KV rows={[
        [<>Direct messages</>, "open — anyone · pairing — an unknown number receives a 6-digit code and waits in “Waiting for approval” until you approve it · allowlist — only listed numbers."],
        [<>Groups</>, "mention — reply only when the account is @mentioned (default) · open — reply to everything · allowlist — only listed senders/groups · off."],
        [<>Allowed senders</>, "Phone numbers (country code, digits only) or group ids. Approving a pairing request adds the number here."],
        [<>Bindings</>, "Route one chat (a number or a group id) to a specific agent; everything else uses the default agent."],
      ]} />
      <P><Term>In the chat</Term> — <Code>/reset</Code> starts the conversation over, <Code>/status</Code> shows which agent and model answer, <Code>/help</Code> lists commands. Each chat keeps its own history (the last turns are replayed to the model). Reasoning is never sent; long answers are split; a short “Working on it…” is sent if a turn takes a while.</P>
      <P><Term>Proactive messages</Term> — turn on <Term>Message people on channels</Term> in an agent's Guardrails to give it the <Code>channel_send</Code> tool (for reminders or follow-ups). Schedules and the <Code>deliver</Code> tool can also target a chat with channel <Code>chat</Code> and target <Code>&lt;accountId&gt;:&lt;number&gt;</Code>. Every send is audited and the account never messages itself.</P>
      <Tip>Attachments people send (images, documents) are saved under the account's media folder and described to the agent, which can open PDF, Word, Excel, PowerPoint, CSV and text files with its <Code>document_read</Code> tool. Enable “Pass received images to the model” only with a vision-capable model.</Tip>
    </>
  ),

  // ─── Models ───────────────────────────────────────────────────────────────
  models: (
    <>
      <P>The <Term>Models</Term> (Hub) page discovers, downloads, and activates local models. Downloads land in the real Hugging Face cache (<Code>~/.cache/huggingface</Code>), so they're shared with other tools. It has two tabs.</P>
      <P><Term>Discover</Term> — search Hugging Face (including the Unsloth org):</P>
      <Bullets items={[
        <>Filter by <Term>format</Term> (GGUF · llama.cpp, or MLX · Apple), <Term>capability</Term> (text, embeddings, speech, image…), and <Term>sort</Term> (newest, downloads, likes, trending).</>,
        <>By default the list hides models too big for your machine ("Fits my device"). Open a model to see metadata and, for GGUF, a per-file picker with a <Term>Fits</Term> / <Term>Tight</Term> / <Term>Too large</Term> verdict for each quantization.</>,
        <>Click <Term>Download</Term>; an <Term>Active downloads</Term> strip shows progress, bytes, and a <Term>Cancel</Term> button. Downloads are parallel/chunked and resume after an interruption. The button becomes <Term>On device ✓</Term> when done.</>,
      ]} />
      <P><Term>On Device</Term> — manage what's cached: each model shows its backend (GGUF / MLX), a vision badge if multimodal, a trainable / inference-only verdict, and size. Pick a context window under <Term>Activate with</Term> (8K–128K, default 32K) and click <Term>Activate</Term> to load it; <Term>Delete</Term> frees disk (disabled while active).</P>
      <P>After activating, the header badge verifies the model with a one-token probe: <Term>Loading… → ● Active</Term>, or <Term>Load failed</Term> with the error. Only one model runs at a time — activating another stops the previous one. Click the ✕ on the badge to stop the server.</P>
      <KV rows={[
        [<>HF_TOKEN</>, "Needed for gated/private repos (or HUGGING_FACE_HUB_TOKEN)."],
        [<>VW_DL_CONNECTIONS</>, "Parallel download connections (default 16)."],
        [<>VW_DL_CHUNK_MB</>, "Chunk size in MB for parallel downloads (default 16)."],
      ]} />
    </>
  ),
  engines: (
    <>
      <P>Virtual Worker picks the right <Term>inference engine</Term> automatically from the model artifact — you don't choose it:</P>
      <KV rows={[
        [<>llama.cpp</>, "GGUF models — runs llama-server."],
        [<>MLX (mlx-lm)</>, "Text MLX models on Apple Silicon."],
        [<>MLX-VLM</>, "Multimodal (vision/audio) MLX models on Apple Silicon."],
      ]} />
      <P>In <Term>managed mode</Term> (default), the backend starts/stops/switches the server when you activate a model, runs it detached + pid-tracked so it survives backend hot-reloads, and re-adopts it on restart. One model runs at a time. If another process holds the port, activation offers to take it over.</P>
      <P>Keep the engines updated from <Term>Settings</Term>. Tune or override behavior with env vars:</P>
      <KV rows={[
        [<>VW_MANAGED_LLAMA</>, '"1" (default) backend-managed; "0" use your own server.'],
        [<>VW_UPSTREAM_URL</>, "Point at any OpenAI-compatible server (disables managed mode)."],
        [<>LLAMA_PORT / LLAMA_CTX / LLAMA_NGL</>, "Port (8080), context (32768), GPU layers (99 = all)."],
        [<>LLAMA_MODEL</>, "A GGUF to load on startup (optional)."],
        [<>VW_SKIP_MLX</>, "Skip auto-installing the MLX engines."],
      ]} />
    </>
  ),
  // ─── Connect & Serve ──────────────────────────────────────────────────────
  connect: (
    <>
      <P>The <Term>Connect</Term> page lets you use your local model from coding agents — fully offline. The backend speaks each vendor's native protocol and translates it to the OpenAI shape your upstream understands (and back, including streaming and tool calls). The protocol is chosen by <Term>URL path</Term>, never by sniffing:</P>
      <KV rows={[
        [<>Claude Code</>, "Anthropic Messages → POST /v1/messages (+ /count_tokens)"],
        [<>Codex</>, "OpenAI Responses → POST /v1/responses"],
        [<>Antigravity / Gemini SDK</>, "Gemini → POST /v1beta/models/{model}:generateContent"],
      ]} />
      <P>The page shows live <Term>upstream status</Term> and the resolved model, plus a copy-paste setup card for each client. If it says the upstream isn't detected, activate a model in <Term>Models</Term> first (or start your own server).</P>
      <P><Term>Pointing a client</Term> generally means setting its base URL to <Code>http://localhost:8888</Code> and a token (see API Tokens). For Claude Code that's <Code>ANTHROPIC_BASE_URL</Code>; for Codex it's a <Code>~/.codex/config.toml</Code> snippet with <Code>wire_api="responses"</Code>; for Gemini it's the SDK's <Code>base_url</Code>. Each card has a <Term>Copy</Term> button.</P>
      <Tip>Map a client's vendor model id to your local model with <Code>VW_MODEL_MAP</Code> (JSON) or a single <Code>VW_DEFAULT_MODEL</Code> — so a client asking for "claude-3-5-sonnet" transparently hits your local model.</Tip>
    </>
  ),
  channel: (
    <>
      <P>Turn any agent into a <Term>chat channel</Term>: a user DMs the bot (or @mentions it in a group), the message runs that agent, and the reply comes back in the same chat — with the agent's persona, tools, skills, MCP, and memory. Each agent is its own bot with its own credentials and connection.</P>
      <P><Term>Why WebSocket (the default).</Term> Virtual Worker runs on your machine with no public address. A classic webhook needs chat channel to reach <Term>into</Term> your laptop at a public URL (a tunnel/port-forward). Instead Virtual Worker uses chat channel's WebSocket "relay": it dials <Term>out</Term> and holds the connection, so <Term>nothing inbound has to be reachable</Term> — no host, no tunnel, no DNS.</P>
      <KV rows={[
        [<>WebSocket (relay)</>, "Virtual Worker connects out to chat channel. No public host — recommended for a laptop."],
        [<>Webhook</>, "chat channel POSTs to /api/channel/callback (SHA-256 signed). Needs a public URL."],
      ]} />
      <P><Term>Each agent is its own bot.</Term> Configure chat channel <Term>per agent</Term>, in the agent's editor: open <Term>Agents → (your agent)</Term> and find the <Term>chat channel</Term> card. Create a bot on the chat channel Open Platform, paste <Code>app_id</Code> / <Code>app_secret</Code> (Basic Info &amp; Credentials) and <Code>signing_secret</Code> (Event Callback), choose WebSocket, and toggle it <Term>On</Term> — <Term>no URL to paste</Term>, Virtual Worker dials chat channel automatically. When the status reads <Term>Connected</Term>, go back to chat channel's <span className="italic">Event Callback → WebSocket</span> dialog and click <Term>Re-verify</Term>. Credentials are <Term>write-only</Term> on-device. The <Term>chat channel</Term> page in the sidebar is a read-only overview of every agent's bot + live status. An optional <Term>allow-list</Term> (employee codes / email domains) limits who can message the bot.</P>
      <P><Term>Continuity &amp; debugging:</Term> each chat channel conversation is saved as a chat session (keyed <Code>channel:&lt;id&gt;</Code>) — the agent sees prior turns for real back-and-forth, and the whole thread shows up in the agent's <Term>Conversations</Term> tab (reopenable in Chat). Every turn is also a <Term>Debug</Term> trace under the same key, and each reply links to its exact run — so you can inspect any message end-to-end.</P>
      <P><Term>Reset a conversation:</Term> send <Code>/clear</Code> (or <Code>/reset</Code> / <Code>/new</Code>) as a message to the bot to wipe that conversation's memory and start fresh — the bot forgets the earlier turns and confirms. In a group, @mention the bot with the command. (Clearing the chat in the web UI does not affect the chat channel conversation — reset it from chat channel with <Code>/clear</Code>.)</P>
      <Tip>Local-first: messages are handled on your machine by the selected agent; the only egress is the secure connection to chat channel itself.</Tip>
    </>
  ),
  gateway: (
    <>
      <P>Under the Connect page is an OpenAI-compatible <Term>gateway</Term>. Anything you can do with an OpenAI client works against <Code>http://localhost:8888/v1</Code>:</P>
      <KV rows={[
        [<>/v1/chat/completions</>, "Chat — byte-for-byte proxy to the upstream (no translation)."],
        [<>/v1/completions</>, "Legacy text completions."],
        [<>/v1/embeddings</>, "Embeddings."],
        [<>/v1/models</>, "List models available at the upstream."],
      ]} />
      <P>The Anthropic, Responses, and Gemini endpoints are <Term>pure translators</Term> on top of this: they map the inbound request to OpenAI Chat Completions, call the upstream once, and map the reply back — streaming events are forwarded immediately, never buffered.</P>
      <P><Term>Model resolution</Term> picks which local model handles a request, first match wins: an entry in <Code>VW_MODEL_MAP</Code> → <Code>VW_DEFAULT_MODEL</Code> → the runtime's active model → the first model the upstream lists → the requested name unchanged.</P>
      <Pre>{`export VW_MODEL_MAP='{"claude-3-5-sonnet-20241022":"local-qwen","gpt-4o":"local-qwen"}'
export VW_DEFAULT_MODEL="local-qwen"`}</Pre>
      <P>Every call through the gateway is captured into <Term>Debug</Term> — so this is also how you get real traffic to inspect.</P>
    </>
  ),
  tokens: (
    <>
      <P><Term>API tokens</Term> are named, expirable keys (<Code>sk-vw-…</Code>) that clients use to authenticate to the gateway. Create them from the Connect page: give a name and a TTL (<Term>Never</Term> / <Term>7</Term> / <Term>30</Term> / <Term>90 days</Term>).</P>
      <P>The plaintext token is shown <Term>once</Term> at creation — copy it then, because only a hash is stored and a reload clears it. Clients send it as <Code>x-api-key</Code>, <Code>Authorization: Bearer …</Code>, <Code>x-goog-api-key</Code>, or <Code>?key=</Code>.</P>
      <P><Term>Auth policy:</Term></P>
      <Bullets items={[
        <><Term>Open</Term> — until you create the first token, any key is accepted (localhost convenience).</>,
        <><Term>Token required</Term> — once any active token exists, a valid one is required.</>,
        <><Term>Static key</Term> — setting <Code>VW_GATEWAY_KEY</Code> requires exactly that key and takes precedence.</>,
      ]} />
      <P>The token list shows each token's name, prefix, expiry, and last-used time. <Term>Revoke</Term> stops a token immediately; lost tokens can't be recovered (revoke and make a new one). Token routes are unauthenticated and meant for localhost — don't expose the backend to the network without adding auth.</P>
    </>
  ),

  // ─── Operate ──────────────────────────────────────────────────────────────
  monitoring: (
    <>
      <P><Term>Monitoring</Term> shows live system health while you run models locally — where resource pressure (out of memory, full disk) is the most common reason a model gets killed or wedged. It polls every few seconds; toggle <Term>Auto 4s</Term> off to pause, or <Term>Refresh</Term> on demand.</P>
      <P><Term>Resource gauges</Term> across the top:</P>
      <Bullets items={[
        <><Term>Memory</Term> — system RAM used / total, plus what's available. On Apple Silicon it's labeled "unified" because the GPU shares this pool.</>,
        <><Term>CPU load</Term> (or <Term>GPU memory</Term> on NVIDIA) — 1-minute load as a percentage of your cores.</>,
        <><Term>Disk</Term> — usage on the volume holding the model cache; watch this, since GGUF/MLX models are large.</>,
        <><Term>Model process</Term> — the inference server's resident memory (RSS), uptime, and pid.</>,
      ]} />
      <P><Term>Inference server</Term> shows the active model, engine, context size, port, and a health badge (Healthy / Loading / Load failed / Stopped) — with the error inline if it failed. A <Term>Search up/down</Term> chip appears when the managed SearXNG (web search) is installed, so you can tell at a glance whether <Code>browser_search</Code> will work. <Term>API traffic</Term> summarizes captured calls, errors, sessions, and tokens (mirrors Debug; local models are $0).</P>
      <P><Term>Recent errors</Term> lists the latest failed LLM API calls with their status code and model — click one to read the full message (e.g. a 503 "Loading model", a 502 unreachable upstream, or "exceeds the available context size"). The <Term>Inference server log</Term> tails the live <Code>llama-server.log</Code> so you can watch a model load or diagnose a crash.</P>
      <Tip>If a model keeps failing to load, check the Memory and Disk gauges first — then lower the context size or pick a smaller quantization in <Term>Models</Term>.</Tip>
    </>
  ),
  debug: (
    <>
      <P><Term>Debug</Term> traces every LLM API call that passes through the gateway — local and external providers alike — so you can see exactly what was sent and returned. A middleware tees a bounded copy of each request/response without buffering the client stream, then parses it on a background thread. Capture never slows or breaks inference.</P>
      <P>The page summarizes <Term>sessions, runs, tool calls, errors, tokens, and cost</Term> at the top, with a sessions list on the left and a detail view on the right.</P>
      <P><Term>Sessions</Term> group related calls. A chat conversation is one trail (<Code>chat:&lt;id&gt;</Code>); external agents that don't send a session id get a rolling per-client trail keyed by provider + model (e.g. <Code>anthropic:local-qwen</Code>). Filter by errors, <Term>filter by agent</Term> (every run records which agent produced it — across chat, worker, schedules, and triggers), and search by session / model / provider. Note: only runs captured after this feature shipped carry an agent tag; older traces show under “all agents”.</P>
      <P>Expand a <Term>run</Term> to see its endpoint, duration, model, token usage, estimated cost, and a per-tool breakdown, then drill into each entry — system / user / assistant / tool calls — with thinking, arguments, results, and usage. Prices for known hosted models are built in; <Term>local models cost $0</Term>.</P>
      <Bullets items={[
        <><Term>Auto 10s</Term> refreshes the view; <Term>Refresh</Term> updates on demand.</>,
        <>Clear traces with the delete buttons (one session, or all).</>,
        <>Disable capture entirely with <Code>VW_DEBUG_CAPTURE=0</Code>.</>,
      ]} />
    </>
  ),
  deliveries: (
    <>
      <P><Term>Deliveries</Term> is the audit log of everything Virtual Worker sent out of the app: schedule results, replies on chat channels, emails, webhooks, desktop notifications and Obsidian notes. Every attempt is one row with its outcome, so you can always answer "did that go out, and where?".</P>
      <Bullets items={[
        <><Term>sent</Term> — the target accepted it. <Term>failed</Term> — the send errored (open the row for the error). <Term>dropped</Term> — refused before sending, for example a webhook URL or email address that is not on the allow-list.</>,
        <>Filter by agent, kind, source (<Code>schedule</Code>, the <Code>deliver</Code> tool, a channel <Code>reply</Code>, a <Code>test</Code> send) or status, and search the target or text. Click a row to read exactly what was sent.</>,
        <>Chat-channel replies also appear under the agent's Activity; schedule results show their latest delivery status next to the schedule.</>,
      ]} />
      <P>Rows are kept for <Code>VW_DELIVERY_RETENTION_DAYS</Code> (90 by default) and pruned by the hourly maintenance loop. Nothing on this page sends anything; it only reads the log.</P>
    </>
  ),
  settings: (
    <>
      <P><Term>Settings</Term> keeps your inference engines current. It lists each engine — <Term>llama.cpp</Term>, <Term>MLX-LM</Term>, <Term>MLX-VLM</Term>, and the core <Term>MLX</Term> framework — with the installed and latest versions.</P>
      <Steps items={[
        <>Click <Term>Check for updates</Term> to fetch the latest versions (PyPI / Homebrew).</>,
        <>If an engine has an update, an <Term>Update</Term> button appears — click it to install or upgrade.</>,
      ]} />
      <P>An update takes effect the next time you activate a model; a model that's already running keeps its version until you re-activate it. Your active-model choice persists in <Code>~/.virtual-worker/settings.json</Code> across restarts. Most other behavior is configured through environment variables (see the relevant sections above).</P>
      <P><Term>Backup &amp; Restore</Term> exports your whole studio — agents, memory, sessions, skills, settings — as one <Code>.vwbak</Code> file and restores it on any machine (a rollback snapshot is written before every import). Turn on <Term>Scheduled snapshots</Term> to have the maintenance loop write a snapshot under <Code>backups/</Code> in your data directory every N hours and keep only the newest few; download or delete them from the same card. Snapshots stay local — copy the folder to a drive or sync it yourself.</P>
    </>
  ),
  troubleshooting: (
    <div className="space-y-4">
      <div>
        <P><Term>"Upstream model server not detected" on Connect / no active model in Chat</Term></P>
        <Bullets items={[
          <>Open <Term>Models → On Device</Term> and <Term>Activate</Term> a model (or download one first under Discover).</>,
          <>Running your own server? Make sure <Code>VW_UPSTREAM_URL</Code> points at it and it's up.</>,
        ]} />
      </div>
      <div>
        <P><Term>A model won't load ("Load failed")</Term></P>
        <Bullets items={[
          <>Pick a smaller quantization — check the per-file <Term>Fits / Tight / Too large</Term> verdict in Discover.</>,
          <>Lower the <Term>Activate with</Term> context size; large contexts need more memory.</>,
          <>Some 4-bit MoE models only fail on first generation, not at startup — the message will say so.</>,
        ]} />
      </div>
      <div>
        <P><Term>Chatting on a local model feels slow</Term></P>
        <Bullets items={[
          <>Turn on <Term>Tight context (faster)</Term> on the agent — caps tool outputs and trims history so each turn re-reads less (biggest win for MCP-heavy chats).</>,
          <>Use a <Term>smaller model</Term> for tool/agent work (e.g. a 3-4B), and keep a big model only for deep reasoning.</>,
          <>Or point the agent at a fast external provider (BYOK DeepSeek/Claude, or a ChatGPT subscription) — see <Term>External Models</Term>.</>,
          <>Spawning more agents won't help a single local model — they share one GPU and queue.</>,
        ]} />
      </div>
      <div>
        <P><Term>A download is stuck or failed</Term></P>
        <Bullets items={[
          <>Cancel and retry — downloads resume from where they stopped.</>,
          <>Gated/private repo? Set <Code>HF_TOKEN</Code> (or <Code>HUGGING_FACE_HUB_TOKEN</Code>) and retry.</>,
        ]} />
      </div>
      <div>
        <P><Term>A client gets 401 from the gateway</Term></P>
        <Bullets items={[
          <>Once any token exists, a valid one is required — create a token on <Term>Connect</Term> and send it as <Code>x-api-key</Code> / Bearer.</>,
          <>If <Code>VW_GATEWAY_KEY</Code> is set, the client must send exactly that key.</>,
        ]} />
      </div>
      <div>
        <P><Term>An MCP server's tools aren't showing up</Term></P>
        <Bullets items={[
          <>Click <Term>Test</Term> on the server to confirm it connects and lists tools, ensure it's <Term>enabled</Term>, and attach it on the agent.</>,
          <>If MCP is unavailable entirely, install the <Code>mcp</Code> package via <Code>./setup.sh</Code>.</>,
        ]} />
      </div>
    </div>
  ),
};

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function DocsPage() {
  const [activeId, setActiveId] = useState<string>(SECTIONS[0].id);
  const [query, setQuery] = useState("");
  const observerRef = useRef<IntersectionObserver | null>(null);

  const q = query.trim().toLowerCase();
  const matches = (s: (typeof SECTIONS)[number]) =>
    !q ||
    s.title.toLowerCase().includes(q) ||
    s.category.toLowerCase().includes(q) ||
    (s.keywords?.toLowerCase().includes(q) ?? false);

  const visible = useMemo(() => SECTIONS.filter(matches), [q]); // eslint-disable-line react-hooks/exhaustive-deps
  const visibleIds = visible.map((s) => s.id).join(",");

  useEffect(() => {
    observerRef.current?.disconnect();
    const obs = new IntersectionObserver(
      (entries) => {
        const inView = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (inView[0]) setActiveId(inView[0].target.id);
      },
      { rootMargin: "-10% 0px -75% 0px", threshold: 0 },
    );
    visible.forEach((s) => {
      const el = document.getElementById(s.id);
      if (el) obs.observe(el);
    });
    observerRef.current = obs;
    return () => obs.disconnect();
  }, [visibleIds]); // eslint-disable-line react-hooks/exhaustive-deps

  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="mx-auto max-w-6xl px-5 py-6 sm:px-8">
      {/* Brand banner */}
      <img src={appLogo} alt="Virtual Worker — Virtual Worker · Local. Private. Powerful."
        className="mb-5 h-auto w-full max-w-md" />

      {/* Header */}
      <div className="mb-5 flex items-center gap-3">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-primary-soft text-primary">
          <BookIcon className="h-5 w-5" />
        </div>
        <div className="flex-1">
          <h1 className="font-display text-2xl font-bold tracking-tight text-ink">Docs</h1>
          <p className="mt-0.5 text-sm text-body">Everything in Virtual Worker — what each feature does and how to use it.</p>
        </div>
      </div>

      {/* Search */}
      <div className="mb-6 max-w-2xl lg:ml-[15rem]">
        <div className="relative">
          <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-mute"><SearchIcon /></span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search docs — e.g. download, gateway, LoRA, token, MCP…"
            className="w-full rounded-full border border-pressed bg-canvas py-2.5 pl-10 pr-10 text-sm text-ink outline-none placeholder:text-mute focus:border-primary"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-mute hover:text-ink"
              aria-label="Clear search"
            ><XIcon /></button>
          )}
        </div>
        {q && (
          <p className="mt-2 text-xs text-mute">
            {visible.length} {visible.length === 1 ? "result" : "results"} for “{query}”
          </p>
        )}
      </div>

      <div className="flex gap-8">
        {/* TOC */}
        <aside className="hidden lg:block w-56 shrink-0">
          <nav className="sticky top-4 max-h-[calc(100vh-2rem)] space-y-4 overflow-y-auto pr-1">
            {CATEGORIES.map((cat) => {
              const secs = cat.sections.filter((s) => matches({ ...s, category: cat.title }));
              if (!secs.length) return null;
              return (
                <div key={cat.title}>
                  <p className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-wider text-mute">{cat.title}</p>
                  <div className="space-y-0.5">
                    {secs.map((s) => {
                      const active = activeId === s.id;
                      return (
                        <button
                          key={s.id}
                          onClick={() => scrollTo(s.id)}
                          className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-1.5 text-left text-[13px] transition-colors ${
                            active ? "bg-primary text-white" : "text-body hover:bg-soft hover:text-ink"
                          }`}
                        >
                          <s.icon className="shrink-0" />
                          {s.title}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </nav>
        </aside>

        {/* Content */}
        <div className="min-w-0 max-w-3xl flex-1 space-y-6 pb-24">
          {visible.length === 0 ? (
            <div className="rounded-2xl border border-line bg-canvas p-8 text-center">
              <p className="font-medium text-ink">No results for “{query}”</p>
              <p className="mt-1 text-sm text-mute">Try a feature name like “gateway”, “download”, “LoRA”, or “token”.</p>
            </div>
          ) : (
            visible.map((s) => (
              <Section key={s.id} id={s.id} title={s.title} icon={s.icon}>
                {CONTENT[s.id]}
              </Section>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

export default DocsPage;
