// Channels — connect chat apps (WhatsApp first) so people can message an agent
// where they already chat. One card per connected account: pairing (QR / code),
// status, policies, allow-list, pairing requests, per-chat bindings, test send.
// Reads/writes /api/channels/*. Secrets are write-only (the API returns hasX flags).

import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

// --------------------------------------------------------------------------- //
// Types (mirror routes/channels.py)
// --------------------------------------------------------------------------- //

interface FieldDef { key: string; label: string; placeholder?: string }
interface Kind {
  kind: string; label: string; description: string;
  configFields: FieldDef[]; secretFields: FieldDef[];
  capabilities: Record<string, boolean | string>;
  installed?: boolean; node?: boolean; installHint?: string;
}
interface Status {
  state: string; detail?: string; me?: string | null; name?: string | null; error?: string | null;
  hasQr?: boolean; pairingCode?: string | null; installed?: boolean; node?: boolean; webhookPath?: string;
}
interface Binding { id: string; conversationId: string; agentId: string; title: string | null }
interface PairingRequest { id: string; senderId: string; senderName: string; code: string; createdAt: string }
interface AccountConfig {
  dmPolicy: "open" | "pairing" | "allowlist"; groupPolicy: "open" | "mention" | "allowlist" | "off";
  allowFrom: string[]; debounceMs: number; ackAfterMs: number; runTimeoutS: number; historyTurns: number;
  sendMediaToModel: boolean; [k: string]: unknown;
}
interface Account {
  id: string; kind: string; name: string; enabled: boolean; defaultAgentId: string | null;
  config: AccountConfig; status: Status; secrets: Record<string, boolean>;
  bindings: Binding[]; pairingRequests: PairingRequest[]; createdAt: string;
}
interface AgentLite { id: string; name: string; role?: string }

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";
const btnPrimary = "rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-40";
const btnGhost = "rounded-xl border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-ink disabled:opacity-40";

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

function stateBadge(s: Status | undefined) {
  const st = s?.state ?? "off";
  const map: Record<string, { label: string; cls: string }> = {
    open: { label: "Connected", cls: "bg-success-soft text-success" },
    qr: { label: "Needs pairing", cls: "bg-warn-soft text-warn" },
    connecting: { label: "Connecting…", cls: "bg-primary-soft text-primary-deep" },
    reconnecting: { label: "Reconnecting…", cls: "bg-warn-soft text-warn" },
    logged_out: { label: "Logged out", cls: "bg-danger-soft text-danger" },
    error: { label: "Error", cls: "bg-danger-soft text-danger" },
    off: { label: "Off", cls: "bg-soft text-mute" },
  };
  const m = map[st] ?? { label: st, cls: "bg-soft text-mute" };
  return <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${m.cls}`}>{m.label}</span>;
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function ChannelsPage() {
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [agents, setAgents] = useState<AgentLite[]>([]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await apiJson<{ kinds: Kind[]; accounts: Account[] }>("/api/channels");
      setKinds(d.kinds); setAccounts(d.accounts);
      const a = await apiJson<{ agents: AgentLite[] }>("/api/agents");
      setAgents(a.agents);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  // Poll status while anything is connecting / pairing.
  useEffect(() => {
    const busy = accounts.some((a) => ["connecting", "qr", "reconnecting"].includes(a.status?.state));
    if (!busy) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [accounts, load]);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="mb-6 flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="font-display text-[26px] font-semibold text-ink">Channels</h1>
          <p className="mt-1 text-sm text-body">
            Let people message your agents from the chat apps they already use. Channels dial <em>out</em>
            — your machine is never exposed — and every reply is logged in Deliveries.
          </p>
        </div>
        <button onClick={() => setAdding(true)} className={btnPrimary}>+ Connect a channel</button>
      </div>

      {error && <p className="mb-4 rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>}

      {adding && (
        <AddAccount kinds={kinds} agents={agents} onClose={() => setAdding(false)} onCreated={async () => { setAdding(false); await load(); }} />
      )}

      {accounts.length === 0 && !adding && (
        <div className="rounded-2xl border border-dashed border-pressed bg-softer p-8 text-center">
          <p className="text-sm font-semibold text-ink">No channels connected yet</p>
          <p className="mt-1 text-xs text-mute">Connect a WhatsApp number to talk to an agent from your phone.</p>
        </div>
      )}

      <div className="space-y-5">
        {accounts.map((a) => (
          <AccountCard key={a.id} account={a} kind={kinds.find((k) => k.kind === a.kind)} agents={agents} onChanged={load} />
        ))}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Add account
// --------------------------------------------------------------------------- //

function AddAccount({ kinds, agents, onClose, onCreated }: {
  kinds: Kind[]; agents: AgentLite[]; onClose: () => void; onCreated: () => Promise<void>;
}) {
  const [kind, setKind] = useState(kinds[0]?.kind ?? "whatsapp");
  const [name, setName] = useState("");
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const k = kinds.find((x) => x.kind === kind);

  const create = async () => {
    setBusy(true); setErr(null);
    try {
      await apiPost("/api/channels/accounts", {
        kind, name: name || k?.label, enabled: true, defaultAgentId: agentId || null,
        config, secrets,
      });
      await onCreated();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <section className="mb-6 rounded-2xl border border-primary-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      <div className="mb-3 flex items-center">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Connect a channel</h3>
        <button onClick={onClose} className="ml-auto text-mute hover:text-ink">✕</button>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="md:col-span-2 flex flex-wrap gap-2">
          {kinds.map((x) => (
            <button key={x.kind} onClick={() => setKind(x.kind)}
              className={`rounded-xl border px-3 py-2 text-left text-sm transition-colors ${
                kind === x.kind ? "border-primary bg-primary-soft text-primary-deep" : "border-pressed text-body hover:bg-soft"}`}>
              <div className="font-semibold">{x.label}</div>
              <div className="max-w-[320px] text-[11px] text-mute">{x.description}</div>
            </button>
          ))}
        </div>
        {k && k.kind === "whatsapp" && k.installed === false && (
          <p className="md:col-span-2 rounded-xl bg-warn-soft px-3 py-2 text-xs text-warn">
            The WhatsApp bridge isn't installed yet{k.node === false ? " and Node.js was not found" : ""}. Run:
            <code className="ml-1 font-mono">{k.installHint}</code>
          </p>
        )}
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Name</label>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder={k?.label} />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Default agent</label>
          <select className={inputCls} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.role ? ` — ${a.role}` : ""}</option>)}
          </select>
        </div>
        {k?.configFields.map((f) => (
          <div key={f.key}>
            <label className="mb-1 block text-xs font-medium text-body">{f.label}</label>
            <input className={inputCls} value={config[f.key] ?? ""} placeholder={f.placeholder}
              onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
          </div>
        ))}
        {k?.secretFields.map((f) => (
          <div key={f.key}>
            <label className="mb-1 block text-xs font-medium text-body">{f.label} <span className="text-mute">(write-only)</span></label>
            <input className={inputCls} type="password" autoComplete="off" value={secrets[f.key] ?? ""}
              onChange={(e) => setSecrets({ ...secrets, [f.key]: e.target.value })} />
          </div>
        ))}
      </div>
      {err && <p className="mt-3 text-xs text-danger">{err}</p>}
      <div className="mt-4 flex gap-2">
        <button onClick={create} disabled={busy} className={btnPrimary}>{busy ? <Spinner /> : "Connect"}</button>
        <button onClick={onClose} className={btnGhost}>Cancel</button>
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Account card
// --------------------------------------------------------------------------- //

function AccountCard({ account, kind, agents, onChanged }: {
  account: Account; kind?: Kind; agents: AgentLite[]; onChanged: () => Promise<void>;
}) {
  const a = account;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [cfg, setCfg] = useState<AccountConfig>(a.config);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);
  const [qr, setQr] = useState<{ qr: string | null; pairingCode: string | null; state: string } | null>(null);
  const [showQr, setShowQr] = useState(false);
  const [testTo, setTestTo] = useState("");
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const [bindConv, setBindConv] = useState("");
  const [bindAgent, setBindAgent] = useState(agents[0]?.id ?? "");
  const qrTimer = useRef<number | null>(null);

  useEffect(() => { setCfg(a.config); setDirty(false); }, [a.config]);

  const patch = useCallback(async (body: Record<string, unknown>) => {
    setBusy(true); setErr(null);
    try { await apiJson(`/api/channels/accounts/${a.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }, [a.id, onChanged]);

  const save = async () => {
    const sec = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v !== ""));
    await patch({ config: cfg, ...(Object.keys(sec).length ? { secrets: sec } : {}) });
    setSecrets({}); setDirty(false);
  };

  const pollQr = useCallback(async () => {
    try {
      const d = await apiJson<{ qr: string | null; pairingCode: string | null; state: string; status: Status }>(`/api/channels/accounts/${a.id}/qr`);
      setQr({ qr: d.qr, pairingCode: d.pairingCode, state: d.status?.state ?? d.state });
      if (d.status?.state === "open") { setShowQr(false); await onChanged(); }
    } catch { /* keep polling */ }
  }, [a.id, onChanged]);

  const startPair = async () => {
    setShowQr(true); setErr(null);
    try { await apiPost(`/api/channels/accounts/${a.id}/pair`, {}); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    await pollQr();
  };
  useEffect(() => {
    if (!showQr) { if (qrTimer.current) window.clearInterval(qrTimer.current); qrTimer.current = null; return; }
    qrTimer.current = window.setInterval(pollQr, 2500);
    return () => { if (qrTimer.current) window.clearInterval(qrTimer.current); };
  }, [showQr, pollQr]);

  const logout = async () => {
    if (!window.confirm("Log out this number? You'll need to pair again.")) return;
    setBusy(true);
    try { await apiPost(`/api/channels/accounts/${a.id}/logout`, {}); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!window.confirm(`Remove "${a.name}"? Session keys and media on disk will be deleted too.`)) return;
    setBusy(true);
    try { await apiJson(`/api/channels/accounts/${a.id}?purge=true`, { method: "DELETE" }); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const sendTest = async () => {
    setTestMsg(null); setBusy(true);
    try {
      const r = await apiPost<{ status: string; error?: string }>(`/api/channels/accounts/${a.id}/test`, { to: testTo });
      setTestMsg(r.status === "sent" ? "Sent ✓" : `${r.status}: ${r.error ?? ""}`);
    } catch (e) { setTestMsg(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const decide = async (rid: string, decision: "approve" | "reject") => {
    setBusy(true);
    try { await apiPost(`/api/channels/accounts/${a.id}/pairing-requests/${rid}/${decision}`, {}); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const addBinding = async () => {
    if (!bindConv.trim() || !bindAgent) return;
    setBusy(true);
    try { await apiPost(`/api/channels/accounts/${a.id}/bindings`, { conversationId: bindConv.trim(), agentId: bindAgent }); setBindConv(""); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const removeBinding = async (bid: string) => {
    setBusy(true);
    try { await apiJson(`/api/channels/accounts/${a.id}/bindings/${bid}`, { method: "DELETE" }); await onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const st = a.status;
  const canPair = !!kind?.capabilities.pairing;
  const agentName = (id: string | null) => agents.find((x) => x.id === id)?.name ?? "—";

  return (
    <section className="rounded-2xl border border-line bg-canvas p-5 shadow-[0_14px_40px_-28px_rgba(40,51,92,0.4)]">
      {/* header */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-display text-lg font-semibold text-ink">{a.name}</h2>
            {stateBadge(st)}
          </div>
          <p className="mt-0.5 text-xs text-mute">
            {kind?.label ?? a.kind}
            {st?.me ? <> · <span className="font-mono">{st.me}</span>{st.name ? ` (${st.name})` : ""}</> : null}
            {st?.detail && st.state !== "open" ? <> · {st.detail}</> : null}
          </p>
          {st?.error && <p className="mt-1 text-xs text-danger">{st.error}</p>}
          {st?.webhookPath && (
            <p className="mt-1 text-xs text-body">Webhook URL: <code className="font-mono">{(cfg.publicUrl as string || "https://<your-host>") + st.webhookPath}</code></p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {canPair && st?.state !== "open" && <button onClick={startPair} disabled={busy} className={btnPrimary}>Pair</button>}
          {canPair && st?.state === "open" && <button onClick={logout} disabled={busy} className={btnGhost}>Log out</button>}
          <button onClick={() => patch({ enabled: !a.enabled })} disabled={busy}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
              a.enabled ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-mute hover:text-ink"}`}>
            {a.enabled ? "On" : "Off"}
          </button>
          <button onClick={remove} disabled={busy} className={btnGhost} title="Remove">🗑</button>
        </div>
      </div>

      {/* pairing modal-ish */}
      {showQr && (
        <div className="mt-4 rounded-xl border border-primary-line bg-softer p-4">
          <div className="flex items-start gap-4">
            {qr?.qr ? (
              <img src={qr.qr} alt="WhatsApp pairing QR" className="size-56 rounded-lg bg-white p-1" />
            ) : (
              <div className="grid size-56 place-items-center rounded-lg bg-canvas text-mute"><Spinner /></div>
            )}
            <div className="text-sm text-body">
              <p className="font-semibold text-ink">Link this device</p>
              <ol className="mt-1 list-decimal space-y-1 pl-4 text-xs">
                <li>Open WhatsApp on the phone with the number you want to connect.</li>
                <li>Settings → Linked devices → Link a device.</li>
                <li>Scan the QR code. {qr?.pairingCode && <>Or enter code <b className="font-mono">{qr.pairingCode}</b> under “Link with phone number”.</>}</li>
              </ol>
              <p className="mt-2 text-[11px] text-mute">
                Status: {qr?.state ?? "…"}. The QR refreshes automatically. Use a dedicated number — this transport uses an
                unofficial client library and you accept WhatsApp's terms.
              </p>
              <button onClick={() => setShowQr(false)} className={`${btnGhost} mt-3`}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* pairing requests */}
      {a.pairingRequests.length > 0 && (
        <div className="mt-4 rounded-xl border border-warn/40 bg-warn-soft p-3">
          <p className="text-xs font-semibold text-warn">Waiting for approval</p>
          <ul className="mt-2 space-y-1">
            {a.pairingRequests.map((r) => (
              <li key={r.id} className="flex items-center gap-2 text-xs text-body">
                <span className="font-mono">{r.senderId}</span>{r.senderName && <span>({r.senderName})</span>}
                <span className="text-mute">code {r.code}</span>
                <span className="ml-auto flex gap-1">
                  <button onClick={() => decide(r.id, "approve")} className={btnGhost}>Approve</button>
                  <button onClick={() => decide(r.id, "reject")} className={btnGhost}>Reject</button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* settings */}
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Default agent</label>
          <select className={inputCls} value={a.defaultAgentId ?? ""} onChange={(e) => patch({ defaultAgentId: e.target.value })}>
            <option value="">(the default assistant)</option>
            {agents.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Direct messages</label>
          <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
            {(["open", "pairing", "allowlist"] as const).map((m) => (
              <button key={m} onClick={() => { setCfg({ ...cfg, dmPolicy: m }); setDirty(true); }}
                className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium capitalize ${cfg.dmPolicy === m ? "bg-primary text-white" : "text-body hover:text-ink"}`}>{m}</button>
            ))}
          </div>
          <p className="mt-1 text-[11px] text-mute">open: anyone · pairing: unknown numbers get a code you approve · allowlist: listed numbers only</p>
        </div>
        {kind?.capabilities.groups && (
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Groups</label>
            <div className="flex gap-1 rounded-xl border border-pressed bg-softer p-0.5">
              {(["mention", "open", "allowlist", "off"] as const).map((m) => (
                <button key={m} onClick={() => { setCfg({ ...cfg, groupPolicy: m }); setDirty(true); }}
                  className={`flex-1 rounded-lg px-2 py-1 text-xs font-medium capitalize ${cfg.groupPolicy === m ? "bg-primary text-white" : "text-body hover:text-ink"}`}>{m}</button>
              ))}
            </div>
          </div>
        )}
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Allowed senders</label>
          <input className={inputCls} value={cfg.allowFrom.join(", ")} placeholder="6281234567890, 1203630-1600000@g.us"
            onChange={(e) => { setCfg({ ...cfg, allowFrom: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) }); setDirty(true); }} />
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Debounce (ms)</label>
            <input className={inputCls} type="number" min={0} value={cfg.debounceMs} onChange={(e) => { setCfg({ ...cfg, debounceMs: Number(e.target.value) || 0 }); setDirty(true); }} />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Ack after (ms)</label>
            <input className={inputCls} type="number" min={0} value={cfg.ackAfterMs} onChange={(e) => { setCfg({ ...cfg, ackAfterMs: Number(e.target.value) || 0 }); setDirty(true); }} />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-body">Timeout (s)</label>
            <input className={inputCls} type="number" min={0} value={cfg.runTimeoutS} onChange={(e) => { setCfg({ ...cfg, runTimeoutS: Number(e.target.value) || 0 }); setDirty(true); }} />
          </div>
        </div>
        {kind?.configFields.map((f) => (
          <div key={f.key}>
            <label className="mb-1 block text-xs font-medium text-body">{f.label}</label>
            <input className={inputCls} value={(cfg[f.key] as string) ?? ""} placeholder={f.placeholder}
              onChange={(e) => { setCfg({ ...cfg, [f.key]: e.target.value }); setDirty(true); }} />
          </div>
        ))}
        {kind?.secretFields.map((f) => {
          const has = a.secrets[`has${f.key[0].toUpperCase()}${f.key.slice(1)}`];
          return (
            <div key={f.key}>
              <label className="mb-1 block text-xs font-medium text-body">{f.label} {has && <span className="text-success">· set</span>}</label>
              <input className={inputCls} type="password" autoComplete="off" value={secrets[f.key] ?? ""} placeholder={has ? "•••••• (leave blank to keep)" : ""}
                onChange={(e) => { setSecrets({ ...secrets, [f.key]: e.target.value }); setDirty(true); }} />
            </div>
          );
        })}
        {kind?.capabilities.media && (
          <label className="flex items-center gap-2 text-xs text-body">
            <input type="checkbox" checked={cfg.sendMediaToModel} onChange={(e) => { setCfg({ ...cfg, sendMediaToModel: e.target.checked }); setDirty(true); }} />
            Pass received images to the model (needs a vision-capable model)
          </label>
        )}
      </div>
      {dirty && (
        <div className="mt-3 flex items-center gap-2">
          <button onClick={save} disabled={busy} className={btnPrimary}>{busy ? <Spinner /> : "Save"}</button>
          <button onClick={() => { setCfg(a.config); setSecrets({}); setDirty(false); }} className={btnGhost}>Discard</button>
        </div>
      )}

      {/* bindings */}
      <div className="mt-5">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Route specific chats to agents</h3>
        <p className="mt-0.5 text-[11px] text-mute">Unbound chats use the default agent. Use a phone number for a DM or the group id for a group.</p>
        {a.bindings.length > 0 && (
          <ul className="mt-2 divide-y divide-line rounded-xl border border-line">
            {a.bindings.map((b) => (
              <li key={b.id} className="flex items-center gap-2 px-3 py-2 text-xs">
                <span className="font-mono text-ink">{b.conversationId}</span>
                <span className="text-mute">→</span>
                <span className="text-body">{agentName(b.agentId)}</span>
                <button onClick={() => removeBinding(b.id)} className="ml-auto text-mute hover:text-danger" title="Remove">✕</button>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2 flex gap-2">
          <input className={`${inputCls} flex-1`} value={bindConv} onChange={(e) => setBindConv(e.target.value)} placeholder="6281234567890 or 1203630-1600000@g.us" />
          <select className={`${inputCls} w-48`} value={bindAgent} onChange={(e) => setBindAgent(e.target.value)}>
            {agents.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
          <button onClick={addBinding} disabled={busy || !bindConv.trim()} className={btnGhost}>Add</button>
        </div>
      </div>

      {/* test send */}
      {st?.state === "open" && (
        <div className="mt-5">
          <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Send a test message</h3>
          <div className="mt-2 flex gap-2">
            <input className={`${inputCls} flex-1`} value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="6281234567890" />
            <button onClick={sendTest} disabled={busy || !testTo.trim()} className={btnGhost}>Send</button>
          </div>
          {testMsg && <p className="mt-1 text-xs text-body">{testMsg}</p>}
        </div>
      )}

      {err && <p className="mt-3 text-xs text-danger">{err}</p>}
    </section>
  );
}
