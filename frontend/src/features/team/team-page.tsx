// Team — the org view (PRD-02). An org grid of every agent with its role, duty
// state and board counts, plus the combined delegation feed (who handed off /
// asked whom). Reads /api/team/overview. Virtual Worker brand (navy on warm cream).

import { useCallback, useEffect, useState } from "react";
import { apiJson } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types (mirror routes/team.py)
// --------------------------------------------------------------------------- //

interface Counts { [status: string]: number }
interface Member {
  id: string;
  name: string;
  role: string;
  dutyStatus: string;
  counts: Counts;
}
interface Delegation {
  id: string;
  type: string;
  summary: string;
  createdAt: string;
  kind: string | null;
  fromAgentId: string | null;
  toAgentId: string | null;
  fromName: string | null;
  toName: string | null;
}
interface Overview { agents: Member[]; delegations: Delegation[] }

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

const OPEN = ["todo", "in_progress", "blocked", "review"] as const;

function openCount(c: Counts): number {
  return OPEN.reduce((n, s) => n + (c[s] || 0), 0);
}

function dutyTone(d: string): string {
  if (d === "working") return "bg-success-soft text-ink";
  if (d === "idle" || d === "paused") return "bg-warn-soft text-ink";
  return "bg-soft text-mute";
}

function monogram(name: string): string {
  return (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]?.toUpperCase() || "").join("") || "?";
}

function ago(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.round(ms / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function TeamPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiJson<Overview>("/api/team/overview"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 8000); // the feed is live-ish; refresh gently
    return () => clearInterval(t);
  }, [load]);

  const agents = data?.agents ?? [];
  const delegations = data?.delegations ?? [];

  return (
    <div className="mx-auto h-full max-w-5xl overflow-y-auto px-8 py-8">
      <header className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Team</h1>
        <p className="mt-1 text-sm text-body">
          Your virtual workers as an org. Agents delegate to each other with{" "}
          <code className="rounded bg-soft px-1 py-0.5 text-[12px] text-ink">assign_to</code> and{" "}
          <code className="rounded bg-soft px-1 py-0.5 text-[12px] text-ink">ask_agent</code>; every
          hand-off lands here. Runs serialize on one local model.
        </p>
      </header>

      {error && (
        <div className="mb-6 rounded-2xl border border-danger bg-danger-soft px-4 py-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* Org grid */}
      <section className="mb-8">
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-mute">
          {agents.length} agent{agents.length === 1 ? "" : "s"}
        </h2>
        {agents.length === 0 ? (
          <p className="rounded-2xl border border-line bg-canvas px-4 py-6 text-center text-sm text-mute">
            No agents yet. Create one on the Agents page.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {agents.map((a) => (
              <div key={a.id} className="rounded-2xl border border-line bg-canvas p-4">
                <div className="flex items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-soft text-[13px] font-bold text-primary-deep">
                    {monogram(a.name)}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-ink">{a.name}</p>
                    <p className="truncate text-xs text-mute">{a.role || "agent"}</p>
                  </div>
                </div>
                <div className="mt-3 flex items-center justify-between">
                  <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${dutyTone(a.dutyStatus)}`}>
                    {a.dutyStatus}
                  </span>
                  <span className="text-xs text-body">
                    {openCount(a.counts)} open · {a.counts["done"] || 0} done
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Delegation feed */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-mute">
          Recent delegations
        </h2>
        {delegations.length === 0 ? (
          <p className="rounded-2xl border border-line bg-canvas px-4 py-6 text-center text-sm text-mute">
            No hand-offs yet. When one agent assigns work to or asks another, it shows up here.
          </p>
        ) : (
          <ul className="overflow-hidden rounded-2xl border border-line bg-canvas">
            {delegations.map((d, i) => (
              <li
                key={d.id}
                className={`flex items-start gap-3 px-4 py-3 ${i > 0 ? "border-t border-line" : ""}`}
              >
                <span className="mt-0.5 shrink-0 rounded-full bg-soft px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-body">
                  {d.kind || "task"}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-ink">{d.summary}</p>
                  {(d.fromName || d.toName) && (
                    <p className="mt-0.5 text-xs text-mute">
                      {d.fromName ?? "?"}
                      <span className="px-1">→</span>
                      {d.toName ?? "?"}
                    </p>
                  )}
                </div>
                <span className="shrink-0 text-xs text-mute">{ago(d.createdAt)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export default TeamPage;
