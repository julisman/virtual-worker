// The intern's "work" surfaces — Overview (duty + standup), Board (Kanban),
// and Activity (the work journal). Monochrome cockpit language.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types
// --------------------------------------------------------------------------- //

export interface TaskT {
  id: string; title: string; description: string; status: string; priority: string;
  orderKey: number; origin: string; resultSummary: string;
  dueAt: string | null; startedAt: string | null; completedAt: string | null; createdAt: string;
}
interface ActivityT { id: string; type: string; summary: string; taskId: string | null; createdAt: string }
interface Standup {
  day: string; headline: string; byType: Record<string, number>;
  taskCounts: Record<string, number>; waitingOnMentor: number; highlights: string[]; totalActivities: number;
}
interface Comment { id: string; author: string; text: string; createdAt: string }

const COLUMNS: { id: string; label: string }[] = [
  { id: "backlog", label: "Backlog" },
  { id: "todo", label: "To do" },
  { id: "in_progress", label: "In progress" },
  { id: "blocked", label: "Blocked" },
  { id: "review", label: "In review" },
  { id: "done", label: "Done" },
];
const PRIORITIES = ["low", "medium", "high", "urgent"];
const PRIORITY_DOT: Record<string, string> = {
  low: "bg-pressed", medium: "bg-info", high: "bg-warn", urgent: "bg-danger",
};

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";

function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// --------------------------------------------------------------------------- //
// Overview
// --------------------------------------------------------------------------- //

interface Duty { dutyStatus: string; running: boolean; current: { taskId: string; title: string } | null }

export function OverviewView({ agentId, onChanged }: { agentId: string; onChanged?: () => void }) {
  const [standup, setStandup] = useState<Standup | null>(null);
  const [duty, setDutyState] = useState<Duty | null>(null);
  const [busy, setBusy] = useState(false);

  const loadStandup = useCallback(() => {
    apiJson<Standup>(`/api/agents/${encodeURIComponent(agentId)}/standup`).then(setStandup).catch(() => {});
  }, [agentId]);
  const loadDuty = useCallback(() => {
    apiJson<Duty>(`/api/agents/${encodeURIComponent(agentId)}/duty`).then(setDutyState).catch(() => {});
  }, [agentId]);

  useEffect(() => {
    loadStandup(); loadDuty();
    // Poll while on this tab so the worker's progress shows live.
    const t = setInterval(() => { loadDuty(); loadStandup(); }, 5000);
    return () => clearInterval(t);
  }, [loadStandup, loadDuty]);

  const toggle = async (status: "on" | "off") => {
    setBusy(true);
    try { await apiPost(`/api/agents/${encodeURIComponent(agentId)}/duty`, { status }); }
    catch { /* */ } finally { setBusy(false); }
    loadDuty(); onChanged?.();
  };

  const status = duty?.dutyStatus || "off";
  const on = status !== "off";
  const working = status === "working" && !!duty?.current;
  const label = status === "working" ? "Working" : status === "paused" ? "Paused (daily cap)"
    : status === "idle" ? "On duty — idle" : "Off duty";

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <div className="flex items-center gap-3">
          <span className={`h-2.5 w-2.5 rounded-full ${working ? "animate-pulse bg-success" : on ? "bg-success" : "bg-mute"}`} />
          <div className="min-w-0">
            <h3 className="font-display text-sm font-bold text-ink">{label}</h3>
            <p className="truncate text-xs text-mute">
              {working ? `Working on: ${duty!.current!.title}`
                : status === "idle" ? "Waiting for the next task on the board."
                : status === "paused" ? "Hit the daily work cap — Stop then Start to resume."
                : "Press Start and the intern pulls tasks from its board and works them on its own."}
            </p>
          </div>
          <button onClick={() => toggle(on ? "off" : "on")} disabled={busy}
            className={`ml-auto shrink-0 rounded-xl px-4 py-2 text-sm font-semibold transition-colors disabled:opacity-40 ${
              on ? "border border-pressed text-body hover:border-ink hover:text-ink" : "bg-primary text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep"
            }`}>{busy ? "…" : on ? "Stop" : "Start working"}</button>
        </div>
      </section>

      <section className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h3 className="mb-2 text-[11px] font-extrabold uppercase tracking-wide text-mute">Standup — today</h3>
        {!standup || standup.totalActivities === 0 ? (
          <p className="text-sm text-mute">No activity recorded today yet.</p>
        ) : (
          <>
            <p className="text-sm text-ink">{standup.headline}</p>
            {standup.highlights.length > 0 && (
              <ul className="mt-3 space-y-1">
                {standup.highlights.map((h, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-body">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-mute" />{h}
                  </li>
                ))}
              </ul>
            )}
            {standup.waitingOnMentor > 0 && (
              <p className="mt-3 rounded-xl bg-warn-soft px-3 py-2 text-xs text-ink">
                {standup.waitingOnMentor} item(s) waiting on you (review / blocked).
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Board (Kanban)
// --------------------------------------------------------------------------- //

export function BoardView({ agentId }: { agentId: string }) {
  const [tasks, setTasks] = useState<TaskT[]>([]);
  const [dragId, setDragId] = useState<string | null>(null);
  const [open, setOpen] = useState<TaskT | null>(null);
  const [adding, setAdding] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftPriority, setDraftPriority] = useState("medium");

  const load = useCallback(async () => {
    try { setTasks((await apiJson<{ tasks: TaskT[] }>(`/api/agents/${encodeURIComponent(agentId)}/tasks`)).tasks); }
    catch { /* */ }
  }, [agentId]);
  useEffect(() => { load(); }, [load]);

  const move = async (id: string, status: string) => {
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, status } : t)));  // optimistic
    await apiJson(`/api/agents/${encodeURIComponent(agentId)}/tasks/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
    }).catch(() => {});
    load();
  };
  const add = async () => {
    if (!draftTitle.trim()) return;
    await apiPost(`/api/agents/${encodeURIComponent(agentId)}/tasks`, { title: draftTitle.trim(), priority: draftPriority, status: "todo" }).catch(() => {});
    setDraftTitle(""); setDraftPriority("medium"); setAdding(false); load();
  };
  const remove = async (id: string) => {
    await apiJson(`/api/agents/${encodeURIComponent(agentId)}/tasks/${id}`, { method: "DELETE" }).catch(() => {});
    setOpen(null); load();
  };

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Board</h3>
        <span className="text-xs text-mute">Drag a card between columns, or open it for details.</span>
        <button onClick={() => setAdding((a) => !a)}
          className="ml-auto rounded-xl bg-primary px-3 py-1.5 text-xs font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">+ New task</button>
      </div>

      {adding && (
        <div className="mb-3 flex items-center gap-2 rounded-2xl border border-line bg-canvas p-3 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
          <input autoFocus className={`${inputCls} flex-1`} value={draftTitle} placeholder="Task title…"
            onChange={(e) => setDraftTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") add(); if (e.key === "Escape") setAdding(false); }} />
          <select className="rounded-xl border border-pressed bg-canvas px-2 py-2 text-sm text-ink" value={draftPriority}
            onChange={(e) => setDraftPriority(e.target.value)}>
            {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <button onClick={add} className="rounded-xl bg-primary px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-primary-deep">Add</button>
        </div>
      )}

      <div className="flex gap-3 overflow-x-auto pb-2">
        {COLUMNS.map((col) => {
          const items = tasks.filter((t) => t.status === col.id);
          return (
            <div key={col.id}
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => { if (dragId) move(dragId, col.id); setDragId(null); }}
              className="flex w-60 shrink-0 flex-col rounded-2xl border border-line bg-soft p-2">
              <div className="flex items-center justify-between px-2 py-1.5">
                <span className="text-xs font-semibold text-ink">{col.label}</span>
                <span className="font-mono text-[10px] text-mute">{items.length}</span>
              </div>
              <div className="flex min-h-[40px] flex-col gap-2">
                {items.map((t) => (
                  <button key={t.id} draggable onDragStart={() => setDragId(t.id)} onClick={() => setOpen(t)}
                    className="group rounded-xl border border-line bg-canvas p-2.5 text-left shadow-[0_10px_28px_-24px_rgba(40,51,92,0.5)] transition-colors hover:border-primary-line">
                    <div className="flex items-start gap-2">
                      <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${PRIORITY_DOT[t.priority] || "bg-mute"}`} title={t.priority} />
                      <span className="min-w-0 flex-1 text-sm text-ink">{t.title}</span>
                    </div>
                    <div className="mt-1.5 flex items-center gap-1.5 pl-3.5">
                      {t.origin === "intern" && <span className="rounded bg-soft px-1.5 py-0.5 font-mono text-[9px] text-mute">self</span>}
                      <span className="font-mono text-[9px] text-mute">{ago(t.createdAt)}</span>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {open && <TaskDetail agentId={agentId} task={open} onClose={() => setOpen(null)} onMove={move} onDelete={remove} onChanged={load} />}
    </div>
  );
}

function TaskDetail({ agentId, task, onClose, onMove, onDelete, onChanged }: {
  agentId: string; task: TaskT; onClose: () => void; onMove: (id: string, s: string) => void;
  onDelete: (id: string) => void; onChanged: () => void;
}) {
  const [comments, setComments] = useState<Comment[]>([]);
  const [text, setText] = useState("");
  const load = useCallback(async () => {
    try { setComments((await apiJson<{ comments: Comment[] }>(`/api/agents/${encodeURIComponent(agentId)}/tasks/${task.id}/comments`)).comments); }
    catch { /* */ }
  }, [agentId, task.id]);
  useEffect(() => { load(); }, [load]);

  const send = async () => {
    if (!text.trim()) return;
    await apiPost(`/api/agents/${encodeURIComponent(agentId)}/tasks/${task.id}/comments`, { text: text.trim(), author: "mentor" }).catch(() => {});
    setText(""); load(); onChanged();
  };

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-ink/30 p-6" onClick={onClose}>
      <div className="flex max-h-[82vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-line bg-canvas shadow-[0_24px_60px_-30px_rgba(40,51,92,0.55)]" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-2 border-b border-pressed p-4">
          <div className="min-w-0 flex-1">
            <h3 className="font-display text-base font-bold text-ink">{task.title}</h3>
            <div className="mt-1 flex items-center gap-2">
              <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-body">{task.priority}</span>
              {task.origin === "intern" && <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-mute">self-filed</span>}
            </div>
          </div>
          <button onClick={onClose} className="text-mute hover:text-ink">✕</button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {task.description && <p className="whitespace-pre-wrap text-sm text-body">{task.description}</p>}
          {task.resultSummary && (
            <div className="mt-3 rounded-xl bg-softer p-3">
              <p className="text-[10px] font-extrabold uppercase tracking-wide text-mute">Result summary</p>
              <p className="mt-1 text-sm text-ink">{task.resultSummary}</p>
            </div>
          )}

          <div className="mt-4">
            <p className="mb-1.5 text-[10px] font-extrabold uppercase tracking-wide text-mute">Move to</p>
            <div className="flex flex-wrap gap-1.5">
              {COLUMNS.map((c) => (
                <button key={c.id} onClick={() => { onMove(task.id, c.id); onClose(); }}
                  className={`rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
                    task.status === c.id ? "border-primary bg-primary text-white" : "border-pressed text-body hover:border-primary-line hover:text-ink"
                  }`}>{c.label}</button>
              ))}
            </div>
          </div>

          <div className="mt-5">
            <p className="mb-1.5 text-[10px] font-extrabold uppercase tracking-wide text-mute">Thread</p>
            {comments.length === 0 ? (
              <p className="text-xs text-mute">No comments yet.</p>
            ) : (
              <div className="space-y-2">
                {comments.map((c) => (
                  <div key={c.id} className={`rounded-xl px-3 py-2 text-sm ${c.author === "mentor" ? "bg-primary text-white" : "bg-soft text-ink"}`}>
                    <span className="font-mono text-[9px] opacity-70">{c.author} · {ago(c.createdAt)}</span>
                    <p className="mt-0.5">{c.text}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 border-t border-pressed p-3">
          <input className={`${inputCls} flex-1`} value={text} placeholder="Reply to your intern…"
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <button onClick={send} className="rounded-xl bg-primary px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-primary-deep">Send</button>
          <button onClick={() => onDelete(task.id)} title="Delete task"
            className="rounded-lg border border-pressed px-2.5 py-2 text-xs text-body hover:border-ink hover:text-ink">Delete</button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Activity
// --------------------------------------------------------------------------- //

const ACTIVITY_GLYPH: Record<string, string> = {
  task_created: "✚", task_moved: "↦", task_claimed: "▶", tool_call: "⚙", file_change: "✎",
  note: "✎", review_requested: "?", review_resolved: "✓", status_change: "◔", error: "!", standup: "▦",
};

const ACTIVITY_PAGE = 50;

export function ActivityView({ agentId }: { agentId: string }) {
  const [acts, setActs] = useState<ActivityT[]>([]);
  const [limit, setLimit] = useState(ACTIVITY_PAGE);
  const load = useCallback(async () => {
    try { setActs((await apiJson<{ activities: ActivityT[] }>(`/api/agents/${encodeURIComponent(agentId)}/activity?limit=${limit}`)).activities); }
    catch { /* */ }
  }, [agentId, limit]);
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, [load]);

  // Reset the window when switching agents.
  useEffect(() => { setLimit(ACTIVITY_PAGE); }, [agentId]);

  // We requested `limit` rows; getting that many back means there are likely more.
  const hasMore = acts.length >= limit;

  return (
    <div>
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Activity</h3>
        {acts.length > 0 && <span className="font-mono text-[10px] text-mute">{acts.length}{hasMore ? "+" : ""} entries</span>}
      </div>
      {acts.length === 0 ? (
        <p className="text-sm text-mute">No activity yet. As your intern works, every tool call, file change, and task move shows up here.</p>
      ) : (
        <>
          <div className="space-y-px">
            {acts.map((a) => (
              <div key={a.id} className="flex items-start gap-3 rounded-lg px-2 py-2 hover:bg-soft">
                <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full font-mono text-[11px] ${
                  a.type === "error" ? "bg-danger-soft text-danger" : a.type === "review_resolved" ? "bg-success-soft text-success" : "bg-soft text-body"
                }`} title={a.type}>{ACTIVITY_GLYPH[a.type] || "·"}</span>
                <span className="min-w-0 flex-1 text-sm text-body">{a.summary}</span>
                <span className="shrink-0 font-mono text-[10px] text-mute">{ago(a.createdAt)}</span>
              </div>
            ))}
          </div>
          {hasMore && (
            <div className="mt-3 flex justify-center">
              <button onClick={() => setLimit((l) => l + ACTIVITY_PAGE)}
                className="rounded-full border border-pressed px-4 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink">
                Load more
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Schedule — timed agent runs (cron / one-time)
// --------------------------------------------------------------------------- //

interface DeliverTarget { channel: string; target: string }
interface ScheduleT {
  id: string; title: string; prompt: string; kind: string;
  cron: string | null; cronText: string | null; runAt: string | null;
  enabled: boolean; nextRunAt: string | null; lastRunAt: string | null;
  lastStatus: string | null; lastResult: string | null; lastError: string | null; runCount: number;
  deliverTo: DeliverTarget[];
}
interface DeliveryT {
  id: string; ts: string; channel: string; target: string | null;
  scheduleId: string | null; status: string; error: string | null;
}

// Channels offered in the delivery picker. in_app + desktop are local & always
// available; webhook is opt-in network egress (the URL is registered on submit).
const DELIVERY_CHANNELS = [
  { kind: "in_app", label: "Chat thread", needsTarget: true, placeholder: "chat session id / alias (e.g. daily-summary)" },
  { kind: "desktop", label: "Desktop notification", needsTarget: false, placeholder: "" },
  { kind: "webhook", label: "Webhook", needsTarget: true, placeholder: "https://your-automation/hook" },
  { kind: "email", label: "Email", needsTarget: true, placeholder: "name@example.com (must be allow-listed in Settings → Email)" },
  { kind: "chat", label: "Chat channel", needsTarget: true, placeholder: "<channel account id>:<phone or chat id>" },
] as const;
const CHANNEL_LABEL: Record<string, string> = {
  in_app: "Chat", desktop: "Desktop", webhook: "Webhook", email: "Email", chat: "Chat channel",
};

const REPEATS = [
  { id: "daily", label: "Every day" },
  { id: "weekdays", label: "Weekdays" },
  { id: "weekly", label: "Weekly" },
  { id: "hourly", label: "Hourly" },
  { id: "once", label: "One-time" },
  { id: "cron", label: "Custom cron" },
] as const;
const DOW = [["1", "Mon"], ["2", "Tue"], ["3", "Wed"], ["4", "Thu"], ["5", "Fri"], ["6", "Sat"], ["0", "Sun"]] as const;

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 16).replace("T", " ");
}

export function ScheduleView({ agentId }: { agentId: string }) {
  const [rows, setRows] = useState<ScheduleT[]>([]);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [repeat, setRepeat] = useState<(typeof REPEATS)[number]["id"]>("daily");
  const [time, setTime] = useState("09:00");
  const [dow, setDow] = useState("1");
  const [minute, setMinute] = useState("0");
  const [onceAt, setOnceAt] = useState("");
  const [rawCron, setRawCron] = useState("0 9 * * *");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [openResult, setOpenResult] = useState<string | null>(null);
  // Delivery picker (PRD-01): which channels a fired run should push its result to.
  const [delOn, setDelOn] = useState<Record<string, boolean>>({});
  const [delTarget, setDelTarget] = useState<Record<string, string>>({});
  // Latest delivery per schedule, for the status badge on each row.
  const [deliveries, setDeliveries] = useState<Record<string, DeliveryT>>({});

  const load = useCallback(async () => {
    try { setRows((await apiJson<{ schedules: ScheduleT[] }>(`/api/agents/${encodeURIComponent(agentId)}/schedules`)).schedules); }
    catch { /* */ }
    try {
      const ds = (await apiJson<{ deliveries: DeliveryT[] }>(
        `/api/deliveries?agentId=${encodeURIComponent(agentId)}&source=schedule&limit=200`)).deliveries;
      const latest: Record<string, DeliveryT> = {};
      for (const d of ds) if (d.scheduleId && !latest[d.scheduleId]) latest[d.scheduleId] = d;  // ds is newest-first
      setDeliveries(latest);
    } catch { /* */ }
  }, [agentId]);
  useEffect(() => { load(); }, [load]);

  // Build the explicit deliverTo array from the picker, registering any webhook
  // URL into the opt-in allow-list first (so network egress is consciously enabled).
  const buildDeliverTo = async (): Promise<DeliverTarget[] | null> => {
    const out: DeliverTarget[] = [];
    for (const c of DELIVERY_CHANNELS) {
      if (!delOn[c.kind]) continue;
      const target = (delTarget[c.kind] || "").trim();
      if (c.needsTarget && !target) {
        setErr(`Add a target for the ${c.label} delivery channel.`);
        return null;
      }
      if (c.kind === "webhook") {
        try {
          const chans = (await apiJson<{ channels: { kind: string; config: { urls?: string[] } }[] }>(`/api/delivery-targets`)).channels;
          const known = chans.some((ch) => ch.kind === "webhook" && (ch.config.urls || []).includes(target));
          if (!known) await apiPost(`/api/delivery-targets`, { kind: "webhook", name: target, enabled: true, config: { urls: [target] } });
        } catch { setErr("Could not register the webhook URL."); return null; }
      }
      out.push({ channel: c.kind, target });
    }
    return out;
  };

  const buildBody = (): Record<string, unknown> | null => {
    const [h, m] = time.split(":").map((x) => parseInt(x, 10));
    if (repeat === "once") {
      if (!onceAt) { setErr("Pick a date & time for a one-time run."); return null; }
      return { kind: "at", runAt: onceAt };
    }
    if (repeat === "cron") return { kind: "cron", cron: rawCron.trim() };
    const cron =
      repeat === "daily" ? `${m} ${h} * * *`
        : repeat === "weekdays" ? `${m} ${h} * * 1-5`
          : repeat === "weekly" ? `${m} ${h} * * ${dow}`
            : `${minute || 0} * * * *`; // hourly
    return { kind: "cron", cron };
  };

  const create = async () => {
    if (!prompt.trim()) { setErr("Add a prompt — what should run."); return; }
    const sched = buildBody();
    if (!sched) return;
    setErr(null);
    const deliverTo = await buildDeliverTo();
    if (deliverTo === null) return;  // a target was missing / webhook failed
    setBusy(true);
    try {
      await apiPost(`/api/agents/${encodeURIComponent(agentId)}/schedules`, { title, prompt, ...sched, deliverTo });
      setTitle(""); setPrompt(""); setDelOn({}); setDelTarget({});
      await load();
    } catch (e) { setErr(e instanceof Error ? e.message : "Could not create schedule"); }
    finally { setBusy(false); }
  };

  const toggle = async (s: ScheduleT) => {
    await apiJson(`/api/agents/${encodeURIComponent(agentId)}/schedules/${s.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !s.enabled }),
    }).catch(() => {});
    load();
  };
  const runNow = async (s: ScheduleT) => {
    setRunning((r) => new Set(r).add(s.id));
    try { await apiPost(`/api/agents/${encodeURIComponent(agentId)}/schedules/${s.id}/run`, {}); }
    catch { /* the run records its own error */ }
    finally {
      setRunning((r) => { const n = new Set(r); n.delete(s.id); return n; });
      setOpenResult(s.id);  // reveal the result once it's back
      load();
    }
  };
  const remove = async (s: ScheduleT) => {
    await apiJson(`/api/agents/${encodeURIComponent(agentId)}/schedules/${s.id}`, { method: "DELETE" }).catch(() => {});
    load();
  };

  return (
    <div className="mx-auto max-w-3xl">
      {/* Create */}
      <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h3 className="mb-3 text-[11px] font-extrabold uppercase tracking-wide text-mute">New schedule</h3>
        <div className="space-y-3">
          <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (e.g. daily-summary)" />
          <textarea className={`${inputCls} min-h-[64px]`} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder="What should the agent do? e.g. Summarize today's activity and message it to me." />
          <div className="flex flex-wrap items-center gap-2">
            <select className={`${inputCls} w-auto`} value={repeat} onChange={(e) => setRepeat(e.target.value as typeof repeat)}>
              {REPEATS.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            </select>
            {(repeat === "daily" || repeat === "weekdays" || repeat === "weekly") && (
              <input type="time" className={`${inputCls} w-auto`} value={time} onChange={(e) => setTime(e.target.value)} />
            )}
            {repeat === "weekly" && (
              <select className={`${inputCls} w-auto`} value={dow} onChange={(e) => setDow(e.target.value)}>
                {DOW.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            )}
            {repeat === "hourly" && (
              <div className="flex items-center gap-1.5 text-sm text-body">at minute
                <input type="number" min={0} max={59} className={`${inputCls} w-20`} value={minute} onChange={(e) => setMinute(e.target.value)} />
              </div>
            )}
            {repeat === "once" && (
              <input type="datetime-local" className={`${inputCls} w-auto`} value={onceAt} onChange={(e) => setOnceAt(e.target.value)} />
            )}
            {repeat === "cron" && (
              <input className={`${inputCls} w-auto flex-1 font-mono`} value={rawCron} onChange={(e) => setRawCron(e.target.value)} placeholder="0 9 * * *" />
            )}
            <button onClick={create} disabled={busy}
              className="ml-auto rounded-full bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-50">
              {busy ? "Adding…" : "Add schedule"}
            </button>
          </div>
          {/* Deliver result to — explicit, opt-in destinations (PRD-01). */}
          <div className="rounded-xl border border-line bg-soft p-3">
            <p className="text-[10px] font-extrabold uppercase tracking-wide text-mute">Deliver result to</p>
            <p className="mb-2 mt-0.5 text-[11px] text-mute">Push this run's output to a destination. Empty = Activity only (default). Targets are explicit — never a guessed recipient.</p>
            <div className="space-y-2">
              {DELIVERY_CHANNELS.map((c) => (
                <div key={c.kind} className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={() => setDelOn((d) => ({ ...d, [c.kind]: !d[c.kind] }))}
                    className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                      delOn[c.kind] ? "border-primary bg-primary text-white" : "border-pressed text-body hover:border-primary-line hover:text-ink"
                    }`}>{c.label}</button>
                  {delOn[c.kind] && c.needsTarget && (
                    <input className={`${inputCls} h-8 flex-1 py-1 text-xs`} value={delTarget[c.kind] || ""} placeholder={c.placeholder}
                      onChange={(e) => setDelTarget((t) => ({ ...t, [c.kind]: e.target.value }))} />
                  )}
                  {delOn[c.kind] && (c.kind === "webhook" || c.kind === "email" || c.kind === "chat") && (
                    <span className="text-[10px] text-warn">data leaves your machine</span>
                  )}
                </div>
              ))}
            </div>
          </div>
          {err && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{err}</p>}
          <p className="text-[11px] text-mute">Times use this computer's local time. A scheduled run executes the agent with its tools; with no delivery target the text stays in Activity — add one above to have the result pushed to you.</p>
        </div>
      </div>

      {/* List */}
      <div className="mt-4 space-y-2">
        {rows.length === 0 && <p className="px-1 py-6 text-center text-sm text-mute">No schedules yet.</p>}
        {rows.map((s) => (
          <div key={s.id} className="rounded-2xl border border-line bg-canvas p-3.5 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
            <div className="flex items-center gap-2">
              <button onClick={() => toggle(s)} title={s.enabled ? "Disable" : "Enable"}
                className={`h-2.5 w-2.5 shrink-0 rounded-full ${s.enabled ? "bg-success" : "bg-mute"}`} />
              <span className="truncate text-sm font-semibold text-ink">{s.title}</span>
              <span className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[11px] text-body">
                {s.cronText || (s.runAt ? `Once · ${fmtWhen(s.runAt)}` : s.kind)}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-1">
                <button onClick={() => runNow(s)} disabled={running.has(s.id)}
                  className="rounded-full border border-pressed px-2.5 py-1 text-[11px] font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-60">
                  {running.has(s.id) ? "Running…" : "Run now"}
                </button>
                <button onClick={() => remove(s)} className="rounded-full border border-pressed px-2 py-1 text-[11px] text-mute hover:bg-soft hover:text-ink">✕</button>
              </div>
            </div>
            <p className="mt-1.5 line-clamp-2 pl-[18px] text-xs text-body">{s.prompt}</p>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 pl-[18px] text-[11px] text-mute">
              {s.enabled && s.nextRunAt && <span>next {fmtWhen(s.nextRunAt)}</span>}
              {!s.enabled && <span>paused</span>}
              {s.lastRunAt && (
                <span className={s.lastStatus === "error" ? "font-semibold text-danger" : "font-semibold text-success"}>
                  last run {fmtWhen(s.lastRunAt)} · {s.lastStatus === "error" ? "failed" : "ok"}
                </span>
              )}
              {s.runCount > 0 && <span>{s.runCount} run{s.runCount === 1 ? "" : "s"}</span>}
            </div>
            {(s.deliverTo?.length > 0) && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5 pl-[18px] text-[11px]">
                <span className="text-mute">delivers to</span>
                {s.deliverTo.map((d, i) => (
                  <span key={i} className="rounded-full border border-pressed bg-soft px-2 py-0.5 text-[10px] text-body">
                    {CHANNEL_LABEL[d.channel] || d.channel}{d.target ? ` · ${d.target}` : ""}
                  </span>
                ))}
                {deliveries[s.id] && (
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                    deliveries[s.id].status === "sent" ? "bg-success-soft text-success" : "bg-danger-soft text-danger"
                  }`} title={deliveries[s.id].error || ""}>
                    {deliveries[s.id].status === "sent" ? "delivered ✓" : `delivery ${deliveries[s.id].status}`}
                  </span>
                )}
              </div>
            )}
            {running.has(s.id) && (
              <p className="mt-2 ml-[18px] text-[11px] text-body">Running now… the agent is working (this can take a moment). Result + Activity update when it finishes.</p>
            )}
            {(s.lastError || s.lastResult) && (
              <div className="mt-2 ml-[18px]">
                <button onClick={() => setOpenResult(openResult === s.id ? null : s.id)}
                  className="text-[11px] font-medium text-body hover:text-ink">
                  {openResult === s.id ? "▾ Hide last result" : "▸ Show last result"}
                </button>
                {openResult === s.id && (
                  <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-panel p-2.5 font-mono text-[10.5px] text-panel-ink">{s.lastError || s.lastResult}</pre>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Triggers — event-driven agent runs (file watch / inbound webhook), PRD-09
// --------------------------------------------------------------------------- //

interface TriggerT {
  id: string; agentId: string; name: string; type: "file" | "webhook";
  path: string | null; hookToken: string | null; promptTemplate: string;
  enabled: boolean; lastFiredAt: string | null; lastStatus: string | null;
  lastResult: string | null; lastError: string | null; lastEvent: string | null; fireCount: number;
}

export function TriggersView({ agentId }: { agentId: string }) {
  const [rows, setRows] = useState<TriggerT[]>([]);
  const [type, setType] = useState<"file" | "webhook">("file");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [prompt, setPrompt] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [openResult, setOpenResult] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setRows((await apiJson<{ triggers: TriggerT[] }>(`/api/triggers?agentId=${encodeURIComponent(agentId)}`)).triggers); }
    catch { /* */ }
  }, [agentId]);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    if (!prompt.trim()) { setErr("Add a prompt — what the agent should do when the event fires."); return; }
    if (type === "file" && !path.trim()) { setErr("Pick a folder (or file) path to watch."); return; }
    setBusy(true); setErr(null);
    try {
      await apiPost(`/api/triggers`, {
        agentId, name, type, promptTemplate: prompt,
        ...(type === "file" ? { path: path.trim() } : {}),
      });
      setName(""); setPath(""); setPrompt("");
      await load();
    } catch (e) { setErr(e instanceof Error ? e.message : "Could not create trigger"); }
    finally { setBusy(false); }
  };

  const toggle = async (t: TriggerT) => {
    await apiJson(`/api/triggers/${t.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !t.enabled }),
    }).catch(() => {});
    load();
  };
  const runNow = async (t: TriggerT) => {
    setRunning((r) => new Set(r).add(t.id));
    try { await apiPost(`/api/triggers/${t.id}/run`, {}); }
    catch { /* the run records its own error */ }
    finally {
      setRunning((r) => { const n = new Set(r); n.delete(t.id); return n; });
      setOpenResult(t.id);
      load();
    }
  };
  const remove = async (t: TriggerT) => {
    await apiJson(`/api/triggers/${t.id}`, { method: "DELETE" }).catch(() => {});
    load();
  };

  const hookUrl = (t: TriggerT) => `${window.location.origin}/api/triggers/hook/${t.hookToken}`;
  const copyHook = (t: TriggerT) => {
    navigator.clipboard?.writeText(hookUrl(t)).then(() => {
      setCopied(t.id); setTimeout(() => setCopied((c) => (c === t.id ? null : c)), 1500);
    }).catch(() => {});
  };

  return (
    <div className="mx-auto max-w-3xl">
      {/* Create */}
      <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <h3 className="mb-3 text-[11px] font-extrabold uppercase tracking-wide text-mute">New trigger</h3>
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <select className={`${inputCls} w-auto`} value={type} onChange={(e) => setType(e.target.value as typeof type)}>
              <option value="file">Watch a folder</option>
              <option value="webhook">Inbound webhook</option>
            </select>
            <input className={`${inputCls} flex-1`} value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (e.g. invoice-watcher)" />
          </div>
          {type === "file" && (
            <input className={`${inputCls} font-mono`} value={path} onChange={(e) => setPath(e.target.value)}
              placeholder="/Users/you/Inbox/invoices — the folder to watch" />
          )}
          <textarea className={`${inputCls} min-h-[64px]`} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={type === "file"
              ? "What should the agent do on a new file? Use {{event}} for the file path. e.g. Read the file at {{event}} and summarize it."
              : "What should the agent do with the payload? Use {{event}} for the request body. e.g. Triage this webhook: {{event}}"} />
          <div className="flex items-center">
            <p className="text-[11px] text-mute">
              {type === "file"
                ? "Polls the folder for new or changed files. Local & offline; pre-existing files don't fire."
                : "Generates an unguessable URL. Opt-in inbound surface — only fires while enabled."}
            </p>
            <button onClick={create} disabled={busy}
              className="ml-auto rounded-full bg-primary px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-50">
              {busy ? "Adding…" : "Add trigger"}
            </button>
          </div>
          {err && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{err}</p>}
        </div>
      </div>

      {/* List */}
      <div className="mt-4 space-y-2">
        {rows.length === 0 && <p className="px-1 py-6 text-center text-sm text-mute">No triggers yet.</p>}
        {rows.map((t) => (
          <div key={t.id} className="rounded-2xl border border-line bg-canvas p-3.5 shadow-[0_14px_40px_-30px_rgba(40,51,92,0.4)]">
            <div className="flex items-center gap-2">
              <button onClick={() => toggle(t)} title={t.enabled ? "Disable" : "Enable"}
                className={`h-2.5 w-2.5 shrink-0 rounded-full ${t.enabled ? "bg-success" : "bg-mute"}`} />
              <span className="truncate text-sm font-semibold text-ink">{t.name}</span>
              <span className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[11px] text-body">
                {t.type === "file" ? "📁 folder watch" : "🪝 webhook"}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-1">
                <button onClick={() => runNow(t)} disabled={running.has(t.id)}
                  className="rounded-full border border-pressed px-2.5 py-1 text-[11px] font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-60">
                  {running.has(t.id) ? "Running…" : "Test fire"}
                </button>
                <button onClick={() => remove(t)} className="rounded-full border border-pressed px-2 py-1 text-[11px] text-mute hover:bg-soft hover:text-ink">✕</button>
              </div>
            </div>
            {t.type === "file" && t.path && (
              <p className="mt-1.5 pl-[18px] font-mono text-[11px] text-body">{t.path}</p>
            )}
            {t.type === "webhook" && t.hookToken && (
              <div className="mt-1.5 flex items-center gap-2 pl-[18px]">
                <code className="min-w-0 flex-1 truncate rounded-lg bg-panel px-2 py-1 font-mono text-[10.5px] text-panel-ink">{hookUrl(t)}</code>
                <button onClick={() => copyHook(t)}
                  className="shrink-0 rounded-full border border-pressed px-2 py-0.5 text-[10.5px] text-body hover:bg-soft hover:text-ink">
                  {copied === t.id ? "Copied" : "Copy"}
                </button>
              </div>
            )}
            <p className="mt-1.5 line-clamp-2 pl-[18px] text-xs text-body">{t.promptTemplate}</p>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 pl-[18px] text-[11px] text-mute">
              {!t.enabled && <span>paused</span>}
              {t.lastFiredAt && (
                <span className={t.lastStatus === "error" ? "font-semibold text-danger" : "font-semibold text-success"}>
                  last fired {fmtWhen(t.lastFiredAt)} · {t.lastStatus === "error" ? "failed" : "ok"}
                </span>
              )}
              {t.fireCount > 0 && <span>{t.fireCount} fire{t.fireCount === 1 ? "" : "s"}</span>}
            </div>
            {running.has(t.id) && (
              <p className="mt-2 ml-[18px] text-[11px] text-body">Firing now… the agent is working. Result + Activity update when it finishes.</p>
            )}
            {(t.lastError || t.lastResult) && (
              <div className="mt-2 ml-[18px]">
                <button onClick={() => setOpenResult(openResult === t.id ? null : t.id)}
                  className="text-[11px] font-medium text-body hover:text-ink">
                  {openResult === t.id ? "▾ Hide last result" : "▸ Show last result"}
                </button>
                {openResult === t.id && (
                  <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-panel p-2.5 font-mono text-[10.5px] text-panel-ink">{t.lastError || t.lastResult}</pre>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
