// Deliveries — the audit log of everything Virtual Worker sent out (PRD §8.8,
// §9). Every outbound send — schedule results, the deliver tool, chat-channel
// replies, email, webhooks, test sends — writes one row to `deliveries`; this
// page lists them newest-first with filters. Read-only: nothing here sends.

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiJson } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types (mirror storage/channel_store.py:_del_row)
// --------------------------------------------------------------------------- //

interface Delivery {
  id: string;
  ts: string;
  channel: string;
  target: string | null;
  source: string;
  agentId: string | null;
  scheduleId: string | null;
  status: string;
  error: string | null;
  payloadText: string;
  payloadBytes: number;
}

interface AgentLite { id: string; name: string }

const CHANNELS = ["in_app", "desktop", "webhook", "email", "chat", "obsidian"] as const;
const STATUSES = ["sent", "failed", "dropped"] as const;
const SOURCES = ["schedule", "tool", "reply", "ack", "command", "pairing", "policy", "test"] as const;

const CHANNEL_LABEL: Record<string, string> = {
  in_app: "Chat thread", desktop: "Desktop", webhook: "Webhook", email: "Email",
  chat: "Chat channel", obsidian: "Obsidian",
};

function statusTone(s: string): string {
  if (s === "sent") return "bg-success-soft text-success";
  if (s === "dropped") return "bg-warn-soft text-warn";
  return "bg-danger-soft text-danger";
}

function fmtDate(iso: string): string {
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const selectCls = "rounded-xl border border-line bg-canvas px-3 py-1.5 text-sm text-ink focus:border-primary focus:outline-none";

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function DeliveriesPage() {
  const [rows, setRows] = useState<Delivery[]>([]);
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  const [channel, setChannel] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [agentId, setAgentId] = useState("");
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    const q = new URLSearchParams({ limit: "500" });
    if (channel) q.set("channel", channel);
    if (status) q.set("status", status);
    if (source) q.set("source", source);
    if (agentId) q.set("agentId", agentId);
    try {
      setRows((await apiJson<{ deliveries: Delivery[] }>(`/api/deliveries?${q}`)).deliveries);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load");
    } finally {
      setLoading(false);
    }
  }, [channel, status, source, agentId]);

  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    apiJson<{ agents: AgentLite[] }>("/api/agents")
      .then((r) => setAgents(r.agents ?? []))
      .catch(() => { /* agent names are a nicety */ });
  }, []);

  const agentName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents) m[a.id] = a.name;
    return m;
  }, [agents]);

  const visible = useMemo(() => {
    const s = search.trim().toLowerCase();
    if (!s) return rows;
    return rows.filter((d) =>
      (d.target || "").toLowerCase().includes(s) ||
      (d.payloadText || "").toLowerCase().includes(s) ||
      (d.error || "").toLowerCase().includes(s));
  }, [rows, search]);

  const counts = useMemo(() => {
    const c = { sent: 0, failed: 0, dropped: 0 };
    for (const d of rows) if (d.status in c) c[d.status as keyof typeof c] += 1;
    return c;
  }, [rows]);

  return (
    <div className="mx-auto h-full max-w-6xl overflow-y-auto px-8 py-8">
      <header className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Deliveries</h1>
        <p className="mt-1 text-sm text-body">
          Everything your workers sent out: schedule results, replies on chat channels, emails,
          webhooks and notifications. Each row is one attempt with its outcome. Nothing leaves
          this machine without a row here.
        </p>
      </header>

      {error && (
        <div className="mb-6 rounded-2xl border border-danger bg-danger-soft px-4 py-3 text-sm text-ink">{error}</div>
      )}

      {/* Summary + filters */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-success-soft px-2.5 py-0.5 text-[11px] font-semibold text-success">{counts.sent} sent</span>
        <span className="rounded-full bg-danger-soft px-2.5 py-0.5 text-[11px] font-semibold text-danger">{counts.failed} failed</span>
        {counts.dropped > 0 && (
          <span className="rounded-full bg-warn-soft px-2.5 py-0.5 text-[11px] font-semibold text-warn">{counts.dropped} dropped</span>
        )}
        <span className="ml-auto" />
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)} className={selectCls} aria-label="Agent">
          <option value="">All agents</option>
          {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select value={channel} onChange={(e) => setChannel(e.target.value)} className={selectCls} aria-label="Kind">
          <option value="">All kinds</option>
          {CHANNELS.map((c) => <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>)}
        </select>
        <select value={source} onChange={(e) => setSource(e.target.value)} className={selectCls} aria-label="Source">
          <option value="">All sources</option>
          {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className={selectCls} aria-label="Status">
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search target or text"
          className="w-48 rounded-xl border border-line bg-canvas px-3 py-1.5 text-sm text-ink placeholder:text-mute focus:border-primary focus:outline-none" />
      </div>

      {/* Table */}
      {loading ? (
        <p className="text-sm text-mute">Loading…</p>
      ) : visible.length === 0 ? (
        <p className="rounded-2xl border border-line bg-canvas px-4 py-10 text-center text-sm text-mute">
          {rows.length === 0
            ? "Nothing sent yet. Add a delivery target to a schedule, connect a chat channel, or let a worker use the deliver tool."
            : "No deliveries match these filters."}
        </p>
      ) : (
        <div className="overflow-hidden rounded-2xl border border-line bg-canvas">
          <table className="w-full text-left text-sm">
            <thead className="bg-softer text-[11px] uppercase tracking-wide text-mute">
              <tr>
                <th className="px-4 py-2 font-semibold">When</th>
                <th className="px-3 py-2 font-semibold">Status</th>
                <th className="px-3 py-2 font-semibold">Kind</th>
                <th className="px-3 py-2 font-semibold">Target</th>
                <th className="px-3 py-2 font-semibold">Agent</th>
                <th className="px-3 py-2 font-semibold">Source</th>
                <th className="px-3 py-2 text-right font-semibold">Size</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((d) => {
                const isOpen = open === d.id;
                return (
                  <DeliveryRow key={d.id} d={d} isOpen={isOpen} agentName={agentName[d.agentId || ""]}
                    onToggle={() => setOpen(isOpen ? null : d.id)} />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-4 text-xs text-mute">
        Rows older than the delivery retention window are pruned by the maintenance loop
        (<span className="font-mono">VW_DELIVERY_RETENTION_DAYS</span>, 90 days by default).
      </p>
    </div>
  );
}

function DeliveryRow({ d, isOpen, agentName, onToggle }: {
  d: Delivery; isOpen: boolean; agentName?: string; onToggle: () => void;
}) {
  return (
    <>
      <tr onClick={onToggle} className="cursor-pointer border-t border-line hover:bg-soft">
        <td className="whitespace-nowrap px-4 py-2 text-xs text-body">{fmtDate(d.ts)}</td>
        <td className="px-3 py-2">
          <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${statusTone(d.status)}`} title={d.error || ""}>
            {d.status}
          </span>
        </td>
        <td className="px-3 py-2 text-xs text-ink">{CHANNEL_LABEL[d.channel] || d.channel}</td>
        <td className="max-w-[220px] truncate px-3 py-2 font-mono text-[11px] text-body" title={d.target || ""}>{d.target || "—"}</td>
        <td className="max-w-[140px] truncate px-3 py-2 text-xs text-body">{agentName || (d.agentId ? d.agentId.slice(0, 8) : "—")}</td>
        <td className="px-3 py-2 text-xs text-mute">{d.source}{d.scheduleId ? " · schedule" : ""}</td>
        <td className="whitespace-nowrap px-3 py-2 text-right text-xs text-mute">{fmtBytes(d.payloadBytes)}</td>
      </tr>
      {isOpen && (
        <tr className="border-t border-line bg-softer">
          <td colSpan={7} className="px-4 py-3">
            {d.error && (
              <p className="mb-2 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{d.error}</p>
            )}
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-xl bg-canvas p-3 font-mono text-[11px] text-body">
              {d.payloadText || "(no text)"}
            </pre>
            <p className="mt-2 font-mono text-[10px] text-mute">
              id {d.id}{d.scheduleId ? ` · schedule ${d.scheduleId}` : ""}{d.agentId ? ` · agent ${d.agentId}` : ""}
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

export default DeliveriesPage;
