// Team & Access (PRD-07). Off by default: a clearly-warned opt-in that moves the
// trust boundary from "localhost = trusted" to "authenticated user = trusted".
// When on, Owner/Admin get a user table (invite / role / remove).

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";
import { atLeast, useAuth, type Role, type User } from "@/lib/auth";

const ROLES: Role[] = ["owner", "admin", "member", "viewer"];

function EnableTeamCard() {
  const { refresh } = useAuth();
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const enable = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      // The first account becomes the Owner and flips Team mode on (and logs in).
      await apiPost("/api/auth/register", { username: username.trim(), password });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not enable team mode");
      setBusy(false);
    }
  };

  return (
    <div className="rounded-2xl border border-line bg-canvas p-5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="font-display text-sm font-semibold text-ink">Team mode is off</h3>
          <p className="mt-1 text-sm text-body">
            Today this studio trusts anyone who can reach it on this machine — there is no
            login. Turn on Team mode to require accounts and roles for every <code className="rounded bg-soft px-1 font-mono text-[11px]">/api</code> request.
          </p>
        </div>
        <span className="rounded-full bg-soft px-2.5 py-1 text-[11px] font-semibold text-mute">Solo</span>
      </div>

      <div className="mt-3 rounded-xl border border-warn-soft bg-warn-soft px-3 py-2.5 text-xs text-warn">
        <strong>Security note:</strong> enabling Team mode changes the trust boundary from
        “localhost is trusted” to “the signed-in user is trusted.” Anyone who could already
        reach this box will be locked out until you give them an account. You become the Owner.
      </div>

      {!open ? (
        <button onClick={() => setOpen(true)}
          className="mt-4 rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep">
          Enable team mode…
        </button>
      ) : (
        <form onSubmit={enable} className="mt-4 space-y-3 border-t border-line pt-4">
          <p className="text-xs font-semibold text-body">Create the Owner account</p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" autoFocus
              className="flex-1 rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary" />
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password"
              className="flex-1 rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary" />
          </div>
          {error && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 text-xs font-medium text-danger">{error}</p>}
          <div className="flex gap-2">
            <button type="submit" disabled={busy || !username || !password}
              className="rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-primary-deep disabled:opacity-40">
              {busy ? "Enabling…" : "Create owner & enable"}
            </button>
            <button type="button" onClick={() => setOpen(false)}
              className="rounded-xl border border-pressed px-4 py-2 text-sm font-medium text-body hover:bg-soft">Cancel</button>
          </div>
        </form>
      )}
    </div>
  );
}

function RoleBadge({ role }: { role: Role }) {
  const tone = role === "owner" ? "bg-primary-soft text-primary-deep"
    : role === "admin" ? "bg-success-soft text-success"
    : role === "viewer" ? "bg-soft text-mute"
    : "bg-warn-soft text-warn";
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${tone}`}>{role}</span>;
}

function UsersTable() {
  const { user: me } = useAuth();
  const canManage = atLeast(me?.role, "admin");
  const isOwner = me?.role === "owner";
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);
  const [nu, setNu] = useState(""); const [np, setNp] = useState(""); const [nr, setNr] = useState<Role>("member");

  const load = useCallback(async () => {
    try { setUsers((await apiJson<{ users: User[] }>("/api/users")).users); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { if (canManage) load(); }, [canManage, load]);

  if (!canManage) {
    return (
      <p className="rounded-2xl border border-line bg-canvas p-4 text-sm text-body">
        You're signed in as <strong>{me?.username}</strong>. Member and viewer accounts can't
        manage users — ask an owner or admin.
      </p>
    );
  }

  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    setInviting(true); setError(null);
    try {
      await apiPost("/api/users", { username: nu.trim(), password: np, role: nr });
      setNu(""); setNp(""); setNr("member");
      await load();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setInviting(false); }
  };

  const changeRole = async (id: string, role: Role) => {
    setError(null);
    try { await apiJson(`/api/users/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) }); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  const remove = async (id: string) => {
    setError(null);
    try { await apiJson(`/api/users/${id}`, { method: "DELETE" }); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return (
    <div className="space-y-3">
      {error && <p className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 text-xs font-medium text-danger">{error}</p>}

      <div className="overflow-hidden rounded-2xl border border-line bg-canvas">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line bg-softer text-left text-[11px] uppercase tracking-wide text-mute">
              <th className="px-4 py-2.5 font-semibold">User</th>
              <th className="px-4 py-2.5 font-semibold">Role</th>
              <th className="px-4 py-2.5 font-semibold">Last login</th>
              <th className="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-b border-line last:border-0">
                <td className="px-4 py-2.5">
                  <span className="font-medium text-ink">{u.username}</span>
                  {u.id === me?.id && <span className="ml-1.5 text-[10px] text-mute">(you)</span>}
                  {u.status !== "active" && <span className="ml-1.5 rounded bg-danger-soft px-1.5 py-0.5 text-[10px] text-danger">disabled</span>}
                </td>
                <td className="px-4 py-2.5">
                  {isOwner && u.id !== me?.id ? (
                    <select value={u.role} onChange={(e) => changeRole(u.id, e.target.value as Role)}
                      className="rounded-lg border border-pressed bg-base px-2 py-1 text-xs text-ink outline-none focus:border-primary">
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  ) : <RoleBadge role={u.role} />}
                </td>
                <td className="px-4 py-2.5 font-mono text-[11px] text-mute">{u.lastLoginAt ? new Date(u.lastLoginAt).toLocaleString() : "—"}</td>
                <td className="px-4 py-2.5 text-right">
                  {isOwner && u.id !== me?.id && (
                    <button onClick={() => remove(u.id)} className="text-xs font-medium text-danger hover:underline">Remove</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <form onSubmit={invite} className="flex flex-col gap-2 rounded-2xl border border-line bg-canvas p-4 sm:flex-row sm:items-center">
        <input value={nu} onChange={(e) => setNu(e.target.value)} placeholder="new username"
          className="flex-1 rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary" />
        <input type="password" value={np} onChange={(e) => setNp(e.target.value)} placeholder="temp password"
          className="flex-1 rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary" />
        <select value={nr} onChange={(e) => setNr(e.target.value as Role)}
          className="rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary">
          {ROLES.filter((r) => r !== "owner" || isOwner).map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button type="submit" disabled={inviting || !nu || !np}
          className="rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-primary-deep disabled:opacity-40">
          {inviting ? "Adding…" : "Add user"}
        </button>
      </form>
    </div>
  );
}

interface AuditEntry { id: string; ts: string; username: string | null; role: string | null; method: string; path: string; status: number | null; client: string | null }

function AuditCard() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setEntries((await apiJson<{ entries: AuditEntry[] }>("/api/team/audit?limit=100")).entries); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="rounded-2xl border border-line bg-canvas p-4">
      <div className="flex items-center">
        <p className="text-sm font-medium text-ink">Audit log</p>
        <span className="ml-2 text-xs text-mute">every change, who made it, and the outcome</span>
        <button onClick={load} className="ml-auto rounded-lg border border-pressed px-2 py-1 text-xs text-body hover:bg-soft">Refresh</button>
      </div>
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
      {entries.length === 0 && !error && <p className="mt-2 text-xs text-mute">No entries yet.</p>}
      {entries.length > 0 && (
        <div className="mt-2 max-h-72 overflow-y-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="text-mute"><tr><th className="py-1 pr-2">When</th><th className="pr-2">Who</th><th className="pr-2">Action</th><th>Status</th></tr></thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className="border-t border-line">
                  <td className="py-1 pr-2 whitespace-nowrap text-mute">{new Date(e.ts).toLocaleString()}</td>
                  <td className="pr-2 text-body">{e.username ?? "—"}{e.role ? <span className="text-mute"> · {e.role}</span> : null}</td>
                  <td className="pr-2 font-mono text-ink">{e.method} {e.path}</td>
                  <td className={e.status && e.status >= 400 ? "text-danger" : "text-success"}>{e.status ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function TeamSettings() {
  const { loading, teamMode, user, refresh } = useAuth();
  const [busy, setBusy] = useState(false);

  const disable = async () => {
    if (!window.confirm("Turn off Team mode? The studio returns to single-user localhost trust (accounts are kept but unused).")) return;
    setBusy(true);
    try { await apiPost("/api/auth/disable-team", {}); await refresh(); }
    finally { setBusy(false); }
  };

  if (loading) return null;

  return (
    <div className="mt-8">
      <div className="mb-4">
        <h2 className="font-display text-base font-semibold text-ink">Team &amp; access</h2>
        <p className="mt-0.5 text-sm text-body">
          Share this box with a small team — optional accounts, login, and roles. Default off:
          solo localhost mode is unchanged.
        </p>
      </div>

      {!teamMode ? (
        <EnableTeamCard />
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-3 rounded-2xl border border-line bg-canvas p-4">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-ink">Team mode is on</p>
              <p className="mt-0.5 text-xs text-mute">Signed in as <strong>{user?.username}</strong> · {user?.role}. Every <code className="font-mono">/api</code> request requires a session.</p>
            </div>
            {user?.role === "owner" && (
              <button onClick={disable} disabled={busy}
                className="rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 text-xs font-semibold text-danger transition-colors hover:opacity-80 disabled:opacity-40">
                {busy ? "Turning off…" : "Disable team mode"}
              </button>
            )}
          </div>
          <UsersTable />
          {(user?.role === "owner" || user?.role === "admin") && <AuditCard />}
        </div>
      )}
    </div>
  );
}

export default TeamSettings;
