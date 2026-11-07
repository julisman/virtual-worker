// Backup & Restore (PRD-11 M1) — export/import a portable .vwbak studio snapshot.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost, apiPut } from "@/lib/api";

interface Rollback {
  name: string;
  size: number;
  modifiedAt: string;
}

interface Schedule {
  enabled: boolean;
  intervalHours: number;
  keep: number;
  includeModels: boolean;
  redactSecrets: boolean;
  lastScheduledAt: string | null;
  lastScheduledError: string | null;
  nextDueAt: string | null;
}

interface BackupInfo {
  lastExportAt: string | null;
  rollbacks: Rollback[];
  scheduled: Rollback[];
  schedule: Schedule;
  homePath: string;
}

interface ImportResult {
  ok: boolean;
  restored: Record<string, boolean>;
  preImportBackup: string | null;
  manifest: { secretsNeeded?: { type: string; key: string; server?: string }[] };
}

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export function BackupCard() {
  const [info, setInfo] = useState<BackupInfo | null>(null);
  const [includeModels, setIncludeModels] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try { setInfo(await apiJson<BackupInfo>("/api/backup/info")); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const exportNow = useCallback(async () => {
    setExporting(true); setError(null);
    try {
      const res = await apiFetch(`/api/backup/export?includeModels=${includeModels}`);
      if (!res.ok) {
        const j = await res.json().catch(() => null);
        throw new Error(j?.detail || `export failed (${res.status})`);
      }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^"]+)"?/);
      const name = m?.[1] || `studio-${new Date().toISOString().slice(0, 10)}.vwbak`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name; document.body.appendChild(a); a.click();
      a.remove(); URL.revokeObjectURL(url);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setExporting(false); }
  }, [includeModels, load]);

  const importNow = useCallback(async (file: File) => {
    const ok = window.confirm(
      `Importing "${file.name}" will REPLACE this studio's database, settings and skills.\n\n` +
      "Your current state is automatically saved as a rollback backup first, but this is " +
      "destructive. Continue?",
    );
    if (!ok) return;
    setImporting(true); setError(null); setResult(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await apiFetch("/api/backup/import", { method: "POST", body: form });
      const j = await res.json().catch(() => null);
      if (!res.ok) throw new Error(j?.detail || `import failed (${res.status})`);
      setResult(j as ImportResult);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setImporting(false); if (fileRef.current) fileRef.current.value = ""; }
  }, [load]);

  const secretsNeeded = result?.manifest?.secretsNeeded ?? [];

  // Scheduled local snapshots (FR-X3): opt-in, written by the hourly maintenance
  // loop under <home>/backups, newest N kept.
  const [sched, setSched] = useState<Schedule | null>(null);
  const [savingSched, setSavingSched] = useState(false);
  const [runningNow, setRunningNow] = useState(false);
  useEffect(() => { if (info) setSched(info.schedule); }, [info]);

  const saveSchedule = useCallback(async (patch: Partial<Schedule>) => {
    if (!sched) return;
    const next = { ...sched, ...patch };
    setSched(next); setSavingSched(true); setError(null);
    try {
      const saved = await apiPut<Schedule>("/api/backup/schedule", {
        enabled: next.enabled, intervalHours: next.intervalHours, keep: next.keep,
        includeModels: next.includeModels, redactSecrets: next.redactSecrets,
      });
      setSched(saved);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSavingSched(false); }
  }, [sched]);

  const runNow = useCallback(async () => {
    setRunningNow(true); setError(null);
    try { await apiPost("/api/backup/schedule/run", {}); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setRunningNow(false); }
  }, [load]);

  const deleteSnapshot = useCallback(async (name: string) => {
    if (!window.confirm(`Delete snapshot ${name}? This cannot be undone.`)) return;
    try {
      const res = await apiFetch(`/api/backup/snapshots/${encodeURIComponent(name)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`delete failed (${res.status})`);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, [load]);

  return (
    <div className="mt-8">
      <div className="mb-4">
        <h2 className="font-display text-base font-semibold text-ink">Backup &amp; Restore</h2>
        <p className="mt-0.5 text-sm text-body">
          Export a portable snapshot of this studio — agents, memory, sessions, settings and skills —
          to a single <span className="font-mono text-xs">.vwbak</span> file you can keep on a drive
          or move to another Mac. Everything stays local; nothing is sent to a cloud.
        </p>
      </div>

      {error && (
        <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{error}</p>
      )}

      {/* Export */}
      <div className="rounded-2xl border border-line bg-canvas p-4">
        <div className="flex items-center gap-3">
          <div className="min-w-0">
            <div className="font-display text-sm font-semibold text-ink">Export snapshot</div>
            <p className="mt-0.5 text-xs text-mute">
              Secrets are redacted by default (API-token hashes dropped, MCP env keys masked).
              The HuggingFace model cache is never included.
            </p>
          </div>
          <button onClick={exportNow} disabled={exporting}
            className="ml-auto flex shrink-0 items-center gap-2 rounded-xl bg-primary px-4 py-2 text-sm font-medium text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
            {exporting ? <><Spinner /> Exporting…</> : "Export .vwbak"}
          </button>
        </div>
        <label className="mt-3 flex items-center gap-2 text-xs text-body">
          <input type="checkbox" checked={includeModels} onChange={(e) => setIncludeModels(e.target.checked)}
            className="size-4 rounded border-pressed accent-primary" />
          Include fine-tuned models (<span className="font-mono">models/</span>) — can be large
        </label>
        <p className="mt-3 text-[11px] text-mute">
          Last export: <span className="font-medium text-body">{fmtDate(info?.lastExportAt ?? null)}</span>
          {info?.homePath && <> · home <span className="font-mono">{info.homePath}</span></>}
        </p>
      </div>

      {/* Import */}
      <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
        <div className="flex items-center gap-3">
          <div className="min-w-0">
            <div className="font-display text-sm font-semibold text-ink">Restore from a snapshot</div>
            <p className="mt-0.5 text-xs text-mute">
              Replaces the current database, settings and skills. Your present state is saved as a
              rollback backup first.
            </p>
          </div>
          <button onClick={() => fileRef.current?.click()} disabled={importing}
            className="ml-auto flex shrink-0 items-center gap-2 rounded-xl border border-pressed bg-canvas px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-soft disabled:opacity-40">
            {importing ? <><Spinner /> Importing…</> : "Import .vwbak"}
          </button>
          <input ref={fileRef} type="file" accept=".vwbak" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) importNow(f); }} />
        </div>
        <p className="mt-3 rounded-xl border border-warn-soft bg-warn-soft px-3 py-2 text-[11px] font-medium text-warn">
          Warning: importing is destructive and replaces your current studio. A timestamped rollback
          backup is written before anything is overwritten.
        </p>

        {result?.ok && (
          <div className="mt-3 border-t border-line pt-3">
            <p className="text-xs font-medium text-success">✓ Restore complete — reload the app to see imported data.</p>
            {result.preImportBackup && (
              <p className="mt-1 text-[11px] text-mute">Rollback saved: <span className="font-mono">{result.preImportBackup}</span></p>
            )}
            {secretsNeeded.length > 0 && (
              <div className="mt-2 rounded-xl bg-softer p-3">
                <p className="text-[11px] font-medium text-body">Re-enter these secrets (redacted in the archive):</p>
                <ul className="mt-1 space-y-0.5">
                  {secretsNeeded.map((s, i) => (
                    <li key={i} className="font-mono text-[10px] text-mute">
                      {s.type === "mcp" ? `MCP ${s.server} · ${s.key}` : `${s.type} · ${s.key}`}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Scheduled local snapshots */}
      <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
        <div className="flex items-center gap-3">
          <div className="min-w-0">
            <div className="font-display text-sm font-semibold text-ink">Scheduled snapshots</div>
            <p className="mt-0.5 text-xs text-mute">
              Write a <span className="font-mono">.vwbak</span> under <span className="font-mono">backups/</span> in
              your data directory on a fixed interval and keep the newest few. Local only; copy the
              folder to a drive or sync it yourself.
            </p>
          </div>
          {sched && (
            <label className="ml-auto flex shrink-0 items-center gap-2 text-sm text-ink">
              <input type="checkbox" checked={sched.enabled} disabled={savingSched}
                onChange={(e) => saveSchedule({ enabled: e.target.checked })}
                className="size-4 rounded border-pressed accent-primary" />
              {sched.enabled ? "On" : "Off"}
            </label>
          )}
        </div>
        {sched && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="flex items-center justify-between gap-2 text-xs text-body">
              Every
              <span className="flex items-center gap-1">
                <input type="number" min={1} max={720} value={sched.intervalHours} disabled={savingSched}
                  onChange={(e) => setSched({ ...sched, intervalHours: Number(e.target.value) || 1 })}
                  onBlur={() => saveSchedule({})}
                  className="w-20 rounded-lg border border-line bg-canvas px-2 py-1 text-right text-sm text-ink" />
                hours
              </span>
            </label>
            <label className="flex items-center justify-between gap-2 text-xs text-body">
              Keep newest
              <span className="flex items-center gap-1">
                <input type="number" min={1} max={365} value={sched.keep} disabled={savingSched}
                  onChange={(e) => setSched({ ...sched, keep: Number(e.target.value) || 1 })}
                  onBlur={() => saveSchedule({})}
                  className="w-20 rounded-lg border border-line bg-canvas px-2 py-1 text-right text-sm text-ink" />
                snapshots
              </span>
            </label>
            <label className="flex items-center gap-2 text-xs text-body">
              <input type="checkbox" checked={sched.redactSecrets} disabled={savingSched}
                onChange={(e) => saveSchedule({ redactSecrets: e.target.checked })}
                className="size-4 rounded border-pressed accent-primary" />
              Redact secrets (a restore then needs keys re-entered)
            </label>
            <label className="flex items-center gap-2 text-xs text-body">
              <input type="checkbox" checked={sched.includeModels} disabled={savingSched}
                onChange={(e) => saveSchedule({ includeModels: e.target.checked })}
                className="size-4 rounded border-pressed accent-primary" />
              Include <span className="font-mono">models/</span>
            </label>
          </div>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button onClick={runNow} disabled={runningNow}
            className="flex items-center gap-2 rounded-xl border border-pressed bg-canvas px-3 py-1.5 text-xs font-medium text-ink transition-colors hover:bg-soft disabled:opacity-40">
            {runningNow ? <><Spinner /> Writing…</> : "Snapshot now"}
          </button>
          <span className="text-[11px] text-mute">
            Last: <span className="font-medium text-body">{fmtDate(sched?.lastScheduledAt ?? null)}</span>
            {sched?.enabled && sched.nextDueAt && <> · next due {fmtDate(sched.nextDueAt)}</>}
          </span>
        </div>
        {sched?.lastScheduledError && (
          <p className="mt-2 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">
            Last scheduled snapshot failed: {sched.lastScheduledError}
          </p>
        )}
        {info && info.scheduled.length > 0 && (
          <ul className="mt-3 space-y-1 border-t border-line pt-3">
            {info.scheduled.map((r) => (
              <li key={r.name} className="flex items-center gap-2 text-[11px]">
                <a href={`/api/backup/snapshots/${encodeURIComponent(r.name)}`} download
                  className="font-mono text-primary hover:underline">{r.name}</a>
                <span className="text-mute">{fmtBytes(r.size)}</span>
                <span className="ml-auto text-mute">{fmtDate(r.modifiedAt)}</span>
                <button onClick={() => deleteSnapshot(r.name)} className="text-mute hover:text-danger" title="Delete">✕</button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Rollback backups */}
      {info && info.rollbacks.length > 0 && (
        <div className="mt-3 rounded-2xl border border-line bg-canvas p-4">
          <div className="font-display text-sm font-semibold text-ink">Rollback backups</div>
          <p className="mt-0.5 text-xs text-mute">Snapshots written automatically before each import (under <span className="font-mono">backups/</span>).</p>
          <ul className="mt-2 space-y-1">
            {info.rollbacks.map((r) => (
              <li key={r.name} className="flex items-center gap-2 text-[11px]">
                <a href={`/api/backup/snapshots/${encodeURIComponent(r.name)}`} download
                  className="font-mono text-primary hover:underline">{r.name}</a>
                <span className="text-mute">{fmtBytes(r.size)}</span>
                <span className="ml-auto text-mute">{fmtDate(r.modifiedAt)}</span>
                <button onClick={() => deleteSnapshot(r.name)} className="text-mute hover:text-danger" title="Delete">✕</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-4 text-xs text-mute">
        To move to another machine: export (or copy the newest snapshot), install Virtual Worker
        there, and import. Models are re-downloaded from the hub.
      </p>
    </div>
  );
}

export default BackupCard;
