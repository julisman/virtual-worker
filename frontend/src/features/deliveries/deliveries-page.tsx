// Deliveries — the audit log of everything Virtual Worker sent out (PRD §8.8,
// §9). Every outbound send — schedule results, the deliver tool, chat-channel
// replies, email, webhooks, test sends — writes one row to `deliveries`; this
// page lists them newest-first with filters, paged on the server (limit/offset,
// search included). Read-only: nothing here sends.

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

interface DeliveryPage {
  deliveries: Delivery[];
  total: number;
  byStatus: Record<string, number>;
}

const PAGE_SIZES = [25, 50, 100] as const;
const PAGE_SIZE_KEY = "vw:deliveries-page-size";

function loadPageSize(): number {
  try {
    const n = Number(localStorage.getItem(PAGE_SIZE_KEY));
    return (PAGE_SIZES as readonly number[]).includes(n) ? n : 50;
  } catch { return 50; }
}

const CHANNELS = ["in_app", "desktop", "webhook", "email", "chat", "obsidian"] as const;
const STATUSES = ["sent", "failed", "dropped"] as const;
const SOURCES = ["schedule", "tool", "reply", "ack", "command", "pairing", "policy", "test"] as const;

const CHANNEL_LABEL: Record<string, string> = {
  in_app: "Chat thread", desktop: "Desktop", webhook: "Webhook", email: "Email",
  chat: "Chat channel", obsidian: "Obsidian",
};

// Chat-channel rows are logged as `chat:<kind>` (chat:whatsapp, chat:telegram, …).
function channelLabel(c: string): string {
  if (c.startsWith("chat:")) return `Chat · ${c.slice(5)}`;
  return CHANNEL_LABEL[c] || c;
}

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
  const [total, setTotal] = useState(0);
  const [byStatus, setByStatus] = useState<Record<string, number>>({});
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(loadPageSize);
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  const [channel, setChannel] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [agentId, setAgentId] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");          // the debounced search sent to the server

  useEffect(() => {
    const t = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Any filter change starts over at the first page.
  useEffect(() => { setPage(0); setOpen(null); }, [channel, status, source, agentId, query, pageSize]);
  useEffect(() => { try { localStorage.setItem(PAGE_SIZE_KEY, String(pageSize)); } catch { /* private mode */ } }, [pageSize]);

  const load = useCallback(async () => {
    const q = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) });
    if (channel) q.set("channel", channel);
    if (status) q.set("status", status);
    if (source) q.set("source", source);
    if (agentId) q.set("agentId", agentId);
    if (query) q.set("q", query);
    try {
      const r = await apiJson<DeliveryPage>(`/api/deliveries?${q}`);
      setRows(r.deliveries);
      setTotal(r.total ?? r.deliveries.length);
      setByStatus(r.byStatus ?? {});
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load");
    } finally {
      setLoading(false);
    }
  }, [channel, status, source, agentId, query, page, pageSize]);

  // Old rows get pruned: if the page we're on no longer exists, step back to the last one.
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  useEffect(() => { if (page > pageCount - 1) setPage(pageCount - 1); }, [page, pageCount]);

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

  const counts = { sent: byStatus.sent ?? 0, failed: byStatus.failed ?? 0, dropped: byStatus.dropped ?? 0 };
  const filtered = !!(channel || status || source || agentId || query);

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
      ) : rows.length === 0 ? (
        <p className="rounded-2xl border border-line bg-canvas px-4 py-10 text-center text-sm text-mute">
          {!filtered
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
              {rows.map((d) => {
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

      {!loading && total > 0 && (
        <Pager page={page} pageCount={pageCount} pageSize={pageSize} total={total} shown={rows.length}
          onPage={(p) => { setPage(p); setOpen(null); }} onPageSize={setPageSize} />
      )}

      <p className="mt-4 text-xs text-mute">
        Rows older than the delivery retention window are pruned by the maintenance loop
        (<span className="font-mono">VW_DELIVERY_RETENTION_DAYS</span>, 90 days by default).
      </p>
    </div>
  );
}

// Page numbers to show: first, last, and a window around the current page, with gaps.
function pageWindow(page: number, count: number): (number | "gap")[] {
  const keep = new Set([0, count - 1, page - 1, page, page + 1].filter((p) => p >= 0 && p < count));
  const out: (number | "gap")[] = [];
  let prev = -1;
  for (const p of [...keep].sort((a, b) => a - b)) {
    if (prev >= 0 && p - prev > 1) out.push("gap");
    out.push(p);
    prev = p;
  }
  return out;
}

function Pager({ page, pageCount, pageSize, total, shown, onPage, onPageSize }: {
  page: number; pageCount: number; pageSize: number; total: number; shown: number;
  onPage: (p: number) => void; onPageSize: (n: number) => void;
}) {
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = page * pageSize + shown;
  const btn = "min-w-8 rounded-lg px-2.5 py-1 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40";
  return (
    <nav aria-label="Deliveries pages" className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
      <span className="text-xs text-mute">
        Showing <span className="font-medium text-body">{from}–{to}</span> of <span className="font-medium text-body">{total}</span>
      </span>
      <label className="flex items-center gap-2 text-xs text-mute">
        Rows per page
        <select value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))} className={selectCls}>
          {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
      {pageCount > 1 && (
        <div className="ml-auto flex items-center gap-1">
          <button type="button" onClick={() => onPage(page - 1)} disabled={page === 0}
            className={`${btn} text-body hover:bg-soft hover:text-ink`} aria-label="Previous page">‹ Prev</button>
          {pageWindow(page, pageCount).map((p, i) => p === "gap"
            ? <span key={`gap${i}`} className="px-1 text-mute">…</span>
            : (
              <button key={p} type="button" onClick={() => onPage(p)} aria-current={p === page ? "page" : undefined}
                className={`${btn} ${p === page ? "bg-primary font-semibold text-white" : "text-body hover:bg-soft hover:text-ink"}`}>
                {p + 1}
              </button>
            ))}
          <button type="button" onClick={() => onPage(page + 1)} disabled={page >= pageCount - 1}
            className={`${btn} text-body hover:bg-soft hover:text-ink`} aria-label="Next page">Next ›</button>
        </div>
      )}
    </nav>
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
        <td className="px-3 py-2 text-xs text-ink">{channelLabel(d.channel)}</td>
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
