// Login screen — shown ONLY in Team mode when there's no valid session.
// No "sign up": accounts are created by an Owner/Admin in Team settings.

import { useState } from "react";
import { useAuth } from "@/lib/auth";
import appIcon from "@/assets/icon.svg";

export function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
      setBusy(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-base px-4 text-ink">
      <form onSubmit={submit}
        className="w-full max-w-[360px] rounded-2xl border border-line bg-canvas p-7 shadow-[0_24px_60px_-30px_rgba(43,60,130,0.5)]">
        <div className="mb-6 flex items-center gap-3">
          <img src={appIcon} alt="Virtual Worker" className="h-[38px] w-[38px] rounded-[11px]" />
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight text-ink">Virtual Worker</h1>
            <p className="text-xs text-mute">Sign in to your team workspace</p>
          </div>
        </div>

        <label className="mb-1 block text-xs font-semibold text-body">Username</label>
        <input
          value={username} onChange={(e) => setUsername(e.target.value)}
          autoFocus autoComplete="username"
          className="mb-3 w-full rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary"
        />

        <label className="mb-1 block text-xs font-semibold text-body">Password</label>
        <input
          type="password" value={password} onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          className="mb-4 w-full rounded-xl border border-pressed bg-base px-3 py-2 text-sm text-ink outline-none focus:border-primary"
        />

        {error && (
          <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 text-xs font-medium text-danger">{error}</p>
        )}

        <button type="submit" disabled={busy || !username || !password}
          className="w-full rounded-xl bg-primary px-4 py-2.5 text-sm font-bold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] transition-colors hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="mt-4 text-center text-[11px] text-mute">
          Local-first · runs on this box · no account? ask your workspace owner
        </p>
      </form>
    </div>
  );
}

export default LoginScreen;
