// Analytics — historical cost & usage over the captured debug facts (PRD-06).
// Monitoring shows "now"; this shows "over time": trends, breakdowns by model /
// agent, per-agent virtual worker scorecards, a budget alert, and export.
// Reads /api/analytics/*. Virtual Worker brand theme (navy on cream), CSS/SVG charts only.

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiJson } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types (mirror routes/analytics.py)
// --------------------------------------------------------------------------- //

interface Budget {
  enabled: boolean; costUsd: number; errorRatePct: number;
  spentUsd: number; spentPct: number | null; errorRateNow: number;
  costExceeded: boolean; errorRateExceeded: boolean; exceeded: boolean;
}
interface Summary {
  from: string | null; to: string | null; calls: number; sessions: number;
  inputTokens: number; outputTokens: number; totalTokens: number; cost: number;
  errors: number; errorRate: number; toolCalls: number; avgLatencyMs: number | null;
  budget: Budget;
}
interface TrendPoint {
  date: string; calls: number; totalTokens: number; cost: number;
  errors: number; errorRate: number; avgLatencyMs: number | null;
}
interface BreakdownRow {
  key: string; name?: string; calls: number; sessions: number; totalTokens: number;
  cost: number; errors: number; errorRate: number; toolCalls: number; avgLatencyMs: number | null;
}
interface Scorecard {
  agentId: string | null; name: string; calls: number; sessions: number;
  totalTokens: number; cost: number; errors: number; errorRate: number;
  toolCalls: number; avgLatencyMs: number | null; activeDays: number;
}

// --------------------------------------------------------------------------- //
// Formatting helpers
// --------------------------------------------------------------------------- //

function num(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}
function compact(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}
function usd(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}
function pct(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}
function ms(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}s`;
  return `${Math.round(n)}ms`;
}
/** Local YYYY-MM-DD, N days ago (0 = today). */
function dayOffset(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const RANGES = [
  { id: 7, label: "7d" },
  { id: 30, label: "30d" },
  { id: 90, label: "90d" },
] as const;

// --------------------------------------------------------------------------- //
// Presentational
// --------------------------------------------------------------------------- //

function Tile({ label, value, sub, alert }: {
  label: string; value: React.ReactNode; sub?: string; alert?: boolean;
}) {
  return (
    <div className={`rounded-2xl border p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)] ${
      alert ? "border-danger bg-danger-soft" : "border-line bg-canvas"
    }`}>
      <div className={`text-[10px] font-semibold uppercase tracking-wide ${alert ? "text-danger" : "text-mute"}`}>{label}</div>
      <div className={`font-display mt-1 text-3xl font-semibold tracking-tight ${alert ? "text-danger" : "text-ink"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-body">{sub}</div>}
    </div>
  );
}

/** Day/week bar chart. Brand fill (bg-primary) on a soft track; error days tinted. */
function TrendChart({ series, metric }: { series: TrendPoint[]; metric: TrendMetric }) {
  const values = series.map((p) => metricValue(p, metric));
  const max = Math.max(1, ...values);
  if (series.length === 0) {
    return <p className="py-10 text-center text-sm text-mute">No activity in this range.</p>;
  }
  return (
    <div className="flex h-48 items-end gap-1">
      {series.map((p, i) => {
        const v = values[i];
        const h = Math.max(2, (v / max) * 100);
        const hot = p.errors > 0 && metric === "calls";
        return (
          <div key={p.date} className="group relative flex flex-1 flex-col items-center justify-end">
            <div className="flex h-full w-full items-end justify-center rounded-t bg-soft">
              <div
                className={`w-full rounded-t transition-[height] duration-500 ${hot ? "bg-danger" : "bg-primary"}`}
                style={{ height: `${h}%` }}
              />
            </div>
            <div className="pointer-events-none absolute bottom-full z-10 mb-1 hidden whitespace-nowrap rounded-lg bg-panel px-2 py-1 text-[11px] text-panel-ink shadow group-hover:block">
              <div className="font-semibold">{p.date}</div>
              <div>{metricLabel(metric)}: {metricFormat(v, metric)}</div>
              <div>{p.calls} calls · {p.errors} err</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

type TrendMetric = "calls" | "totalTokens" | "cost" | "errors";
function metricValue(p: TrendPoint, m: TrendMetric): number {
  return m === "calls" ? p.calls : m === "totalTokens" ? p.totalTokens : m === "cost" ? p.cost : p.errors;
}
function metricLabel(m: TrendMetric): string {
  return m === "calls" ? "Calls" : m === "totalTokens" ? "Tokens" : m === "cost" ? "Cost" : "Errors";
}
function metricFormat(v: number, m: TrendMetric): string {
  return m === "cost" ? usd(v) : num(v);
}

/** A horizontal bar inside a breakdown table cell. */
function MiniBar({ value, max }: { value: number; max: number }) {
  const w = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-soft">
      <div className="h-full rounded-full bg-primary" style={{ width: `${w}%` }} />
    </div>
  );
}

function BreakdownTable({ title, rows }: { title: string; rows: BreakdownRow[] }) {
  const maxCalls = Math.max(1, ...rows.map((r) => r.calls));
  return (
    <div className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      <h2 className="mb-3 text-sm font-semibold text-ink">{title}</h2>
      {rows.length === 0 ? (
        <p className="text-sm text-body">No data.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] uppercase tracking-wide text-mute">
              <th className="pb-2 text-left font-semibold">Name</th>
              <th className="pb-2 text-right font-semibold">Calls</th>
              <th className="pb-2 text-right font-semibold">Tokens</th>
              <th className="pb-2 text-right font-semibold">Cost</th>
              <th className="pb-2 text-right font-semibold">Err</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-hairline">
                <td className="max-w-[180px] py-2">
                  <div className="truncate font-medium text-ink" title={r.name || r.key}>{r.name || r.key}</div>
                  <div className="mt-1"><MiniBar value={r.calls} max={maxCalls} /></div>
                </td>
                <td className="py-2 text-right font-mono text-ink">{num(r.calls)}</td>
                <td className="py-2 text-right font-mono text-body">{compact(r.totalTokens)}</td>
                <td className="py-2 text-right font-mono text-body">{usd(r.cost)}</td>
                <td className={`py-2 text-right font-mono ${r.errors > 0 ? "text-danger" : "text-mute"}`}>{pct(r.errorRate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ScorecardGrid({ cards }: { cards: Scorecard[] }) {
  if (cards.length === 0) return <p className="text-sm text-body">No agent activity yet.</p>;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map((c) => {
        const hot = c.errorRate > 0.1;
        return (
          <div key={c.agentId ?? "external"} className="rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary-soft font-display text-sm font-bold text-primary-deep">
                {(c.name || "?").slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-ink" title={c.name}>{c.name}</div>
                <div className="text-[11px] text-mute">{c.sessions} sessions · {c.activeDays} active days</div>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-center">
              <div>
                <div className="font-display text-xl font-semibold text-ink">{num(c.calls)}</div>
                <div className="text-[10px] uppercase tracking-wide text-mute">Calls</div>
              </div>
              <div>
                <div className="font-display text-xl font-semibold text-ink">{num(c.toolCalls)}</div>
                <div className="text-[10px] uppercase tracking-wide text-mute">Tools</div>
              </div>
              <div>
                <div className={`font-display text-xl font-semibold ${hot ? "text-danger" : "text-ink"}`}>{pct(c.errorRate)}</div>
                <div className="text-[10px] uppercase tracking-wide text-mute">Errors</div>
              </div>
            </div>
            <div className="mt-3 flex items-center justify-between border-t border-hairline pt-2 text-xs text-body">
              <span>{compact(c.totalTokens)} tokens</span>
              <span>{usd(c.cost)}</span>
              <span>{ms(c.avgLatencyMs)} avg</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function AnalyticsPage() {
  const [days, setDays] = useState<number>(30);
  const [bucket, setBucket] = useState<"day" | "week">("day");
  const [metric, setMetric] = useState<TrendMetric>("calls");
  const [auto, setAuto] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [summary, setSummary] = useState<Summary | null>(null);
  const [series, setSeries] = useState<TrendPoint[]>([]);
  const [models, setModels] = useState<BreakdownRow[]>([]);
  const [providers, setProviders] = useState<BreakdownRow[]>([]);
  const [agents, setAgents] = useState<BreakdownRow[]>([]);
  const [cards, setCards] = useState<Scorecard[]>([]);

  const range = useMemo(() => ({ from: dayOffset(days - 1), to: dayOffset(0) }), [days]);
  const qs = useMemo(() => `from=${range.from}&to=${range.to}`, [range]);

  const load = useCallback(async () => {
    try {
      const [s, t, m, p, a, sc] = await Promise.all([
        apiJson<Summary>(`/api/analytics/summary?${qs}`),
        apiJson<{ series: TrendPoint[] }>(`/api/analytics/trends?${qs}&bucket=${bucket}`),
        apiJson<{ rows: BreakdownRow[] }>(`/api/analytics/breakdown?dimension=model&${qs}`),
        apiJson<{ rows: BreakdownRow[] }>(`/api/analytics/breakdown?dimension=provider&${qs}`),
        apiJson<{ rows: BreakdownRow[] }>(`/api/analytics/breakdown?dimension=agent&${qs}`),
        apiJson<{ scorecards: Scorecard[] }>(`/api/analytics/scorecard?${qs}`),
      ]);
      setSummary(s); setSeries(t.series); setModels(m.rows);
      setProviders(p.rows); setAgents(a.rows); setCards(sc.scorecards); setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load analytics");
    }
  }, [qs, bucket]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [auto, load]);

  const exportUrl = (format: "csv" | "json") => `/api/analytics/export?format=${format}&${qs}`;
  const budget = summary?.budget;

  return (
    <div className="mx-auto max-w-5xl px-5 py-6 sm:px-8">
      {/* Header + filter bar */}
      <div className="mb-5 -mx-5 border-b border-pressed bg-softer px-5 py-4 sm:-mx-8 sm:px-8">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Analytics</h1>
            <p className="mt-0.5 text-sm text-body">Historical cost &amp; usage — the over-time half of Monitoring.</p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex overflow-hidden rounded-full border border-pressed">
              {RANGES.map((r) => (
                <button key={r.id} onClick={() => setDays(r.id)}
                  className={`px-3 py-1.5 text-xs font-medium ${days === r.id ? "bg-primary text-white" : "text-body hover:bg-soft hover:text-ink"}`}
                >{r.label}</button>
              ))}
            </div>
            <button onClick={() => setAuto((x) => !x)}
              className={`rounded-full border px-3 py-1.5 text-xs font-medium ${auto ? "border-primary bg-primary text-white" : "border-pressed text-body hover:bg-soft hover:text-ink"}`}
            >{auto ? "● Auto 15s" : "Paused"}</button>
            <button onClick={load}
              className="rounded-full border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink"
            >Refresh</button>
            <a href={exportUrl("csv")} download
              className="rounded-full border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink"
            >Export CSV</a>
            <a href={exportUrl("json")} download
              className="rounded-full border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink"
            >JSON</a>
          </div>
        </div>
      </div>

      {err && (
        <div className="mb-5 rounded-xl border border-danger bg-danger-soft px-4 py-3 text-sm text-danger">
          {err} — is the backend running on :8888?
        </div>
      )}

      {/* Budget breach banner */}
      {budget?.exceeded && (
        <div className="mb-5 rounded-xl border border-warn bg-warn-soft px-4 py-3 text-sm font-medium text-warn">
          Budget alert:
          {budget.costExceeded && ` external spend ${usd(budget.spentUsd)} exceeds your ${usd(budget.costUsd)} cap.`}
          {budget.errorRateExceeded && ` error rate ${budget.errorRateNow}% exceeds your ${budget.errorRatePct}% threshold.`}
        </div>
      )}

      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Tile label="Calls" value={compact(summary?.calls)} sub={`${num(summary?.sessions)} sessions`} />
        <Tile label="Tokens" value={compact(summary?.totalTokens)} sub={`${compact(summary?.inputTokens)} in · ${compact(summary?.outputTokens)} out`} />
        <Tile label="Est. cost" value={usd(summary?.cost)} sub="hosted only · local $0" alert={!!budget?.costExceeded} />
        <Tile label="Error rate" value={pct(summary?.errorRate)} sub={`${num(summary?.errors)} errors`} alert={!!summary && summary.errorRate > 0.1} />
        <Tile label="Avg latency" value={ms(summary?.avgLatencyMs)} sub={`${num(summary?.toolCalls)} tool calls`} />
      </div>

      {/* Trend chart */}
      <div className="mt-3 rounded-2xl border border-line bg-canvas p-4 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold text-ink">Trend</h2>
          <div className="ml-auto flex items-center gap-2">
            <div className="flex overflow-hidden rounded-lg border border-pressed text-xs">
              {(["calls", "totalTokens", "cost", "errors"] as TrendMetric[]).map((m) => (
                <button key={m} onClick={() => setMetric(m)}
                  className={`px-2.5 py-1 font-medium ${metric === m ? "bg-primary text-white" : "text-body hover:bg-soft"}`}
                >{metricLabel(m)}</button>
              ))}
            </div>
            <div className="flex overflow-hidden rounded-lg border border-pressed text-xs">
              {(["day", "week"] as const).map((b) => (
                <button key={b} onClick={() => setBucket(b)}
                  className={`px-2.5 py-1 font-medium capitalize ${bucket === b ? "bg-primary text-white" : "text-body hover:bg-soft"}`}
                >{b}</button>
              ))}
            </div>
          </div>
        </div>
        <TrendChart series={series} metric={metric} />
        <div className="mt-2 flex justify-between text-[11px] text-mute">
          <span>{series[0]?.date ?? range.from}</span>
          <span>{series[series.length - 1]?.date ?? range.to}</span>
        </div>
      </div>

      {/* Breakdowns */}
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <BreakdownTable title="By model" rows={models} />
        <BreakdownTable title="By agent" rows={agents} />
      </div>
      <div className="mt-3">
        <BreakdownTable title="By provider" rows={providers} />
      </div>

      {/* Scorecards */}
      <div className="mt-5">
        <h2 className="mb-3 font-display text-lg font-semibold tracking-tight text-ink">virtual worker scorecards</h2>
        <ScorecardGrid cards={cards} />
      </div>

      <p className="mt-6 text-center text-[11px] text-mute">
        {range.from} → {range.to} · read-only over captured Debug data · cost is an estimate (hosted models priced, local = $0)
      </p>
    </div>
  );
}

export default AnalyticsPage;
