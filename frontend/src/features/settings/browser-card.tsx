// Browser (Settings → Browser) — global config for the interactive agent
// control browser. controlMode/focusPolicy are the common knobs; channel, CDP
// URL, profile dir and fetch-headless live under Advanced. An env var pins a
// field (envOverrides) — shown read-only when it does. "Relaunch" tears the
// control-browser context down so launch-time changes apply without a restart.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPut, apiPost } from "@/lib/api";

type ControlMode = "visible" | "headless";
type FocusPolicy = "allow" | "never";
type Channel = "chrome" | "" | "msedge" | null;

interface BrowserSettings {
  controlMode: ControlMode;
  focusPolicy: FocusPolicy;
  channel: Channel;
  cdpUrl: string;
  fetchHeadless: boolean;
  profileDir: string | null;
  envOverrides: Record<string, boolean>;
}

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary disabled:opacity-50";

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

function EnvPill() {
  return <span className="rounded-full bg-soft px-2 py-0.5 font-mono text-[10px] text-mute">set by env</span>;
}

export function BrowserCard() {
  const [cfg, setCfg] = useState<BrowserSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [relaunching, setRelaunching] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = useCallback(async () => {
    try { setCfg(await apiJson<BrowserSettings>("/api/browser/settings")); }
    catch (e) { setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // PUT the whole object (minus the read-only envOverrides) and refresh from the response.
  const save = useCallback(async (patch: Partial<BrowserSettings>) => {
    if (!cfg) return;
    const next = { ...cfg, ...patch };
    setCfg(next);
    setSaving(true); setMsg(null);
    try {
      const { envOverrides: _drop, ...body } = next;
      const res = await apiPut<BrowserSettings>("/api/browser/settings", body);
      setCfg(res);
      setMsg({ kind: "ok", text: "Saved." });
    } catch (e) { setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }); }
    finally { setSaving(false); }
  }, [cfg]);

  const relaunch = useCallback(async () => {
    setRelaunching(true); setMsg(null);
    try {
      const res = await apiPost<{ ok: boolean; message?: string }>("/api/browser/relaunch", {});
      setMsg({ kind: "ok", text: res.message || "Agent browser relaunched." });
    } catch (e) { setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }); }
    finally { setRelaunching(false); }
  }, []);

  if (!cfg) {
    return (
      <div className="mt-8">
        <h2 className="font-display text-base font-semibold text-ink">Browser</h2>
        {msg?.kind === "err"
          ? <p className="mt-2 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{msg.text}</p>
          : <p className="mt-2 text-sm text-mute">Loading…</p>}
      </div>
    );
  }

  const env = cfg.envOverrides || {};

  return (
    <div className="mt-8">
      <div className="mb-4 flex items-start gap-3">
        <div>
          <h2 className="font-display text-base font-semibold text-ink">Browser</h2>
          <p className="mt-0.5 text-sm text-body">
            How the interactive browser the agents drive is launched and whether it may take over your screen.
          </p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {saving && <span className="flex items-center gap-1.5 text-xs text-mute"><Spinner /> Saving…</span>}
          {!saving && msg?.kind === "ok" && <span className="text-xs text-success">{msg.text}</span>}
        </div>
      </div>

      {msg?.kind === "err" && (
        <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{msg.text}</p>
      )}

      <div className="space-y-3 rounded-2xl border border-line bg-canvas p-4">
        {/* Agent browser mode */}
        <div>
          <div className="mb-1 flex items-center gap-2">
            <span className="text-xs font-medium text-body">Agent browser mode</span>
            {env.controlMode && <EnvPill />}
          </div>
          <div className="flex gap-2">
            {(["visible", "headless"] as ControlMode[]).map((m) => (
              <button key={m} type="button" disabled={env.controlMode || saving}
                onClick={() => save({ controlMode: m })}
                className={`rounded-xl border px-3 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  cfg.controlMode === m ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-body hover:bg-soft"
                }`}>{m === "visible" ? "Visible" : "Headless"}</button>
            ))}
          </div>
          <p className="mt-1.5 text-[11px] leading-snug text-mute">
            Visible shows a window you can log into; headless hides it (logins already saved still apply).
          </p>
        </div>

        {/* Focus policy */}
        <div className="border-t border-pressed pt-3">
          <span className="mb-1 block text-xs font-medium text-body">Focus policy</span>
          <div className="flex gap-2">
            {(["allow", "never"] as FocusPolicy[]).map((p) => (
              <button key={p} type="button" disabled={saving}
                onClick={() => save({ focusPolicy: p })}
                className={`rounded-xl border px-3 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  cfg.focusPolicy === p ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-body hover:bg-soft"
                }`}>{p === "allow" ? "Allow focus" : "Never steal focus"}</button>
            ))}
          </div>
          <p className="mt-1.5 text-[11px] leading-snug text-mute">
            Applies when the browser is visible or CDP-attached: "Never steal focus" stops an agent from
            pulling the window in front of what you're doing on each action. Attached to your Chrome, the
            agent's new tabs also open in the background, like a Cmd-click.
          </p>
        </div>

        {/* Advanced */}
        <div className="border-t border-pressed pt-3">
          <button type="button" onClick={() => setAdvanced((s) => !s)}
            className="flex w-full items-center gap-2 text-left">
            <span className="font-display text-sm font-semibold text-ink">Advanced</span>
            <span className="text-mute">{advanced ? "▾" : "▸"}</span>
          </button>
          {advanced && (
            <div className="mt-3 space-y-3">
              <label className="block">
                <span className="mb-1 flex items-center gap-2 text-xs font-medium text-body">
                  Browser channel {env.channel && <EnvPill />}
                </span>
                <select className={inputCls} disabled={env.channel || saving}
                  value={cfg.channel === null ? "auto" : cfg.channel === "" ? "chromium" : cfg.channel}
                  onChange={(e) => {
                    const v = e.target.value;
                    const channel: Channel = v === "auto" ? null : v === "chromium" ? "" : (v as Channel);
                    save({ channel });
                  }}>
                  <option value="auto">Auto (system Chrome if installed)</option>
                  <option value="chrome">System Google Chrome</option>
                  <option value="chromium">Bundled Chromium</option>
                  <option value="msedge">Microsoft Edge</option>
                </select>
              </label>

              <label className="block">
                <span className="mb-1 flex items-center gap-2 text-xs font-medium text-body">
                  CDP URL {env.cdpUrl && <EnvPill />}
                </span>
                <input className={inputCls} disabled={env.cdpUrl || saving} placeholder="http://127.0.0.1:9222"
                  value={cfg.cdpUrl}
                  onChange={(e) => setCfg({ ...cfg, cdpUrl: e.target.value })}
                  onBlur={(e) => save({ cdpUrl: e.target.value.trim() })} />
                <span className="mt-1 block text-[11px] text-mute">
                  Attach to your own running Chrome (started with --remote-debugging-port) instead of launching one.
                </span>
              </label>

              <label className="block">
                <span className="mb-1 flex items-center gap-2 text-xs font-medium text-body">
                  Profile directory {env.profileDir && <EnvPill />}
                </span>
                <input className={inputCls} disabled={env.profileDir || saving} placeholder="default (~/.virtual-worker/browser-profile)"
                  value={cfg.profileDir ?? ""}
                  onChange={(e) => setCfg({ ...cfg, profileDir: e.target.value })}
                  onBlur={(e) => save({ profileDir: e.target.value.trim() || null })} />
              </label>

              <label className="flex items-center gap-2">
                <input type="checkbox" className="size-4 rounded border-pressed accent-primary"
                  disabled={env.fetchHeadless || saving} checked={cfg.fetchHeadless}
                  onChange={(e) => save({ fetchHeadless: e.target.checked })} />
                <span className="text-xs text-body">Run the headless fetch browser hidden</span>
                {env.fetchHeadless && <EnvPill />}
              </label>
            </div>
          )}
        </div>

        <div className="flex justify-end border-t border-pressed pt-3">
          <button type="button" onClick={relaunch} disabled={relaunching}
            className="flex items-center gap-1.5 rounded-xl border border-pressed bg-canvas px-3 py-1.5 text-xs font-medium text-ink transition-colors hover:bg-soft disabled:opacity-40">
            {relaunching ? <><Spinner /> Relaunching…</> : "Relaunch agent browser"}
          </button>
        </div>
        <p className="text-[11px] text-mute">
          Mode, channel, CDP URL and profile take effect the next time the browser launches — use Relaunch to apply them now.
        </p>
      </div>
    </div>
  );
}

export default BrowserCard;
