// Email delivery (SMTP) — one or more outbound email targets. Schedules and the
// `deliver` tool can email a result, but only to addresses (or @domains) on a
// target's allow-list: network egress is opt-in and recipients are never guessed.
// The password is write-only; the API returns hasPassword only.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

interface Target {
  id: string; kind: string; name: string; enabled: boolean;
  config: { host?: string; port?: number; username?: string; from?: string; tls?: string; allowTo?: string[] };
  secrets: { hasPassword?: boolean };
}

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";
const btnGhost = "rounded-xl border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-40";

export function EmailCard() {
  const [targets, setTargets] = useState<Target[]>([]);
  const [editing, setEditing] = useState<Partial<Target> & { password?: string } | null>(null);
  const [testTo, setTestTo] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await apiJson<{ channels: Target[] }>("/api/delivery-targets");
      setTargets(d.channels.filter((c) => c.kind === "email"));
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    if (!editing) return;
    setBusy(true); setMsg(null);
    const cfg = {
      host: editing.config?.host ?? "", port: Number(editing.config?.port ?? 587),
      username: editing.config?.username ?? "", from: editing.config?.from ?? "",
      tls: editing.config?.tls ?? "", allowTo: editing.config?.allowTo ?? [],
    };
    const body = { kind: "email", name: editing.name || cfg.from || "email", enabled: true, config: cfg,
      ...(editing.password ? { secrets: { password: editing.password } } : {}) };
    try {
      if (editing.id) await apiJson(`/api/delivery-targets/${editing.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      else await apiPost("/api/delivery-targets", body);
      setEditing(null); await load();
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const remove = async (t: Target) => {
    if (!window.confirm(`Remove email target "${t.name}"?`)) return;
    await apiJson(`/api/delivery-targets/${t.id}`, { method: "DELETE" }).catch(() => {});
    await load();
  };
  const toggle = async (t: Target) => {
    await apiJson(`/api/delivery-targets/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "email", enabled: !t.enabled }) }).catch(() => {});
    await load();
  };
  const test = async (t: Target) => {
    setBusy(true); setMsg(null);
    try {
      const r = await apiPost<{ status: string; error?: string }>(`/api/delivery-targets/${t.id}/test`, { target: testTo[t.id] || "", text: "Virtual Worker test email — it works." });
      setMsg(r.status === "sent" ? `Sent to ${testTo[t.id]} ✓` : `${r.status}: ${r.error ?? ""}`);
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <section className="mt-8 rounded-2xl border border-line bg-canvas p-5">
      <div className="flex items-center">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Email delivery (SMTP)</h3>
        <button onClick={() => setEditing({ config: { port: 587, allowTo: [] } })} className={`${btnGhost} ml-auto`}>+ Add</button>
      </div>
      <p className="mt-1 text-xs text-mute">Lets schedules and agents email a result. Only addresses or @domains on the allow-list can receive mail.</p>

      {targets.map((t) => (
        <div key={t.id} className="mt-3 rounded-xl border border-line bg-softer p-3 text-xs">
          <div className="flex items-center gap-2">
            <button onClick={() => toggle(t)} className={`h-2.5 w-2.5 rounded-full ${t.enabled ? "bg-success" : "bg-mute"}`} title={t.enabled ? "Disable" : "Enable"} />
            <span className="font-semibold text-ink">{t.name}</span>
            <span className="font-mono text-mute">{t.config.host}:{t.config.port} · from {t.config.from}</span>
            {t.secrets?.hasPassword ? <span className="text-success">· password set</span> : <span className="text-warn">· no password</span>}
            <span className="ml-auto flex gap-1">
              <button onClick={() => setEditing({ ...t })} className={btnGhost}>Edit</button>
              <button onClick={() => remove(t)} className={btnGhost}>Remove</button>
            </span>
          </div>
          <p className="mt-1 text-mute">Allowed: {(t.config.allowTo ?? []).join(", ") || "(nobody — add addresses)"}</p>
          <div className="mt-2 flex gap-2">
            <input className={`${inputCls} h-8 flex-1 py-1 text-xs`} placeholder="send a test to… (must be allow-listed)" value={testTo[t.id] ?? ""} onChange={(e) => setTestTo({ ...testTo, [t.id]: e.target.value })} />
            <button onClick={() => test(t)} disabled={busy || !(testTo[t.id] ?? "").trim()} className={btnGhost}>Test</button>
          </div>
        </div>
      ))}

      {editing && (
        <div className="mt-3 grid gap-2 rounded-xl border border-primary-line bg-softer p-3 md:grid-cols-2">
          <input className={inputCls} placeholder="Name" value={editing.name ?? ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          <input className={inputCls} placeholder="From address" value={editing.config?.from ?? ""} onChange={(e) => setEditing({ ...editing, config: { ...editing.config, from: e.target.value } })} />
          <input className={inputCls} placeholder="SMTP host (e.g. smtp.gmail.com)" value={editing.config?.host ?? ""} onChange={(e) => setEditing({ ...editing, config: { ...editing.config, host: e.target.value } })} />
          <input className={inputCls} type="number" placeholder="Port (587 STARTTLS · 465 SSL)" value={editing.config?.port ?? 587} onChange={(e) => setEditing({ ...editing, config: { ...editing.config, port: Number(e.target.value) } })} />
          <input className={inputCls} placeholder="Username" value={editing.config?.username ?? ""} onChange={(e) => setEditing({ ...editing, config: { ...editing.config, username: e.target.value } })} />
          <input className={inputCls} type="password" autoComplete="off" placeholder={editing.id ? "Password (leave blank to keep)" : "Password / app password"} value={editing.password ?? ""} onChange={(e) => setEditing({ ...editing, password: e.target.value })} />
          <input className={`${inputCls} md:col-span-2`} placeholder="Allowed recipients: a@b.com, @company.com (comma-separated)" value={(editing.config?.allowTo ?? []).join(", ")}
            onChange={(e) => setEditing({ ...editing, config: { ...editing.config, allowTo: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
          <div className="flex gap-2 md:col-span-2">
            <button onClick={save} disabled={busy} className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-40">Save</button>
            <button onClick={() => setEditing(null)} className={btnGhost}>Cancel</button>
          </div>
        </div>
      )}
      {msg && <p className="mt-2 text-xs text-body">{msg}</p>}
    </section>
  );
}
