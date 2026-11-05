// Settings — inference engine versions + one-click updates. Monochrome theme.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";
import { BackupCard } from "./backup-card";
import { EmailCard } from "./email-card";
import { TeamSettings } from "@/features/team/team-settings";

interface Engine {
  key: string;
  label: string;
  sub: string;
  manager: string;
  installed: string | null;
  available: boolean;
  canUpdate: boolean;
  latest?: string | null;
  updateAvailable?: boolean;
}

interface UpdateResult {
  ok: boolean;
  returncode?: number;
  installed?: string | null;
  output: string;
}

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

export function SettingsPage() {
  const [engines, setEngines] = useState<Engine[]>([]);
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState(false);
  const [updating, setUpdating] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, UpdateResult>>({});
  const [error, setError] = useState<string | null>(null);

  const loadLocal = useCallback(async () => {
    try { setEngines((await apiJson<{ engines: Engine[] }>("/api/engines")).engines); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { loadLocal(); }, [loadLocal]);

  const check = useCallback(async () => {
    setChecking(true); setError(null);
    try {
      setEngines((await apiJson<{ engines: Engine[] }>("/api/engines/check")).engines);
      setChecked(true);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setChecking(false); }
  }, []);

  const update = useCallback(async (key: string) => {
    setUpdating(key); setError(null);
    try {
      const res = await apiPost<UpdateResult>(`/api/engines/${encodeURIComponent(key)}/update`, {});
      setResult((r) => ({ ...r, [key]: res }));
      await check(); // refresh versions + update flags
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setUpdating(null); }
  }, [check]);

  const anyUpdate = engines.some((e) => e.updateAvailable);

  return (
    <div className="flex h-full min-w-0 flex-col bg-base">
      <div className="flex items-center gap-3 border-b border-pressed bg-softer px-6 py-3">
        <h1 className="font-display text-lg font-semibold tracking-tight text-ink">Settings</h1>
        <span className="ml-auto font-mono text-[10px] text-mute">Local-first · update checks reach PyPI / Homebrew</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-[680px]">
          <div className="mb-4 flex items-center gap-3">
            <div>
              <h2 className="font-display text-base font-semibold text-ink">Inference engines</h2>
              <p className="mt-0.5 text-sm text-body">The runtimes that serve your models. Update them to get new model support &amp; fixes.</p>
            </div>
            <button onClick={check} disabled={checking}
              className="ml-auto flex items-center gap-2 rounded-xl border border-pressed bg-canvas px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-soft disabled:opacity-40">
              {checking ? <><Spinner /> Checking…</> : "Check for updates"}
            </button>
          </div>

          {error && <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>}
          {checked && !anyUpdate && (
            <p className="mb-3 rounded-xl border border-success-soft bg-success-soft px-3 py-2 text-xs font-medium text-success">All engines are up to date.</p>
          )}

          <div className="space-y-2">
            {engines.map((e) => {
              const res = result[e.key];
              const busy = updating === e.key;
              return (
                <div key={e.key} className="rounded-2xl border border-line bg-canvas p-4">
                  <div className="flex items-center gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-display text-sm font-semibold text-ink">{e.label}</span>
                        <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-mute">{e.manager}</span>
                        {!e.available && <span className="rounded-full bg-danger-soft px-2 py-0.5 font-mono text-[10px] font-medium text-danger">not installed</span>}
                      </div>
                      <p className="mt-0.5 text-xs text-mute">{e.sub}</p>
                    </div>

                    <div className="ml-auto flex items-center gap-3">
                      <div className="text-right">
                        <div className="font-mono text-xs text-ink">{e.installed ?? "—"}</div>
                        {e.latest != null && (
                          <div className="font-mono text-[10px] text-mute">
                            latest {e.latest}
                          </div>
                        )}
                      </div>
                      {e.updateAvailable ? (
                        <button onClick={() => update(e.key)} disabled={busy || !e.canUpdate}
                          className="flex items-center gap-1.5 rounded-xl bg-primary px-3 py-1.5 text-xs font-medium text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
                          {busy ? <><Spinner /> Updating…</> : "Update"}
                        </button>
                      ) : busy ? (
                        <span className="flex items-center gap-1.5 text-xs text-body"><Spinner /> Updating…</span>
                      ) : e.available ? (
                        <span className="rounded-full bg-success-soft px-3 py-1.5 text-xs font-medium text-success">
                          {e.latest != null ? "Up to date" : "Installed"}
                        </span>
                      ) : (
                        <span className="rounded-full bg-soft px-3 py-1.5 text-xs text-mute">—</span>
                      )}
                    </div>
                  </div>

                  {res && (
                    <div className="mt-3 border-t border-line pt-3">
                      <p className={`text-xs font-medium ${res.ok ? "text-success" : "text-danger"}`}>
                        {res.ok ? `✓ Updated${res.installed ? ` to ${res.installed}` : ""}` : "✕ Update failed"}
                      </p>
                      <pre className="mt-2 max-h-48 overflow-auto rounded-xl bg-softer p-3 font-mono text-[10px] leading-relaxed text-body whitespace-pre-wrap">{res.output}</pre>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <p className="mt-4 text-xs text-mute">
            Note: updating an engine takes effect the next time you activate a model —
            a model that's currently running keeps its loaded version until you re-activate it.
          </p>

          <EmailCard />
          <BackupCard />
          <TeamSettings />
        </div>
      </div>
    </div>
  );
}

export default SettingsPage;
