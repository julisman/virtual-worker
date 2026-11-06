// Vision fallback (global setting) — lets a text-only agent hand incoming
// images to a vision-capable helper agent. Opt-in; the helper's own model
// (possibly a remote/BYOK provider) is the one egress this feature adds.

import { useCallback, useEffect, useState } from "react";
import { apiJson } from "@/lib/api";

interface VisionHelperConfig {
  enabled: boolean;
  agentId: string;
}

interface AgentRef {
  id: string;
  name: string;
}

function Spinner() {
  return <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

export function VisionHelperCard() {
  const [enabled, setEnabled] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [agents, setAgents] = useState<AgentRef[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [cfg, ag] = await Promise.all([
          apiJson<VisionHelperConfig>("/api/vision-helper"),
          apiJson<{ agents: AgentRef[] }>("/api/agents"),
        ]);
        setEnabled(cfg.enabled);
        setAgentId(cfg.agentId);
        setAgents(ag.agents);
      } catch (e) {
        setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // save(): follows the PUT-merge pattern used by the channel account cards —
  // apply the patch, replace local state from the response, flash "Saved."
  const save = useCallback(async (patch: Partial<VisionHelperConfig>) => {
    setSaving(true);
    setMsg(null);
    try {
      const cfg = await apiJson<VisionHelperConfig>("/api/vision-helper", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      setEnabled(cfg.enabled);
      setAgentId(cfg.agentId);
      setMsg({ kind: "ok", text: "Saved." });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }, []);

  const toggle = useCallback(() => { save({ enabled: !enabled }); }, [enabled, save]);

  return (
    <div className="mt-8">
      <div className="mb-4 flex items-start gap-3">
        <div>
          <h2 className="font-display text-base font-semibold text-ink">Vision fallback</h2>
          <p className="mt-0.5 text-sm text-body">
            Let text-only agents read incoming images by handing each one to a vision-capable helper agent.
          </p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {saving && <span className="flex items-center gap-1.5 text-xs text-mute"><Spinner /> Saving…</span>}
          {!saving && msg?.kind === "ok" && <span className="text-xs text-success">{msg.text}</span>}
          <button type="button" onClick={toggle} disabled={loading || saving}
            className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              enabled ? "border-primary bg-primary text-white" : "border-pressed bg-canvas text-body hover:bg-soft"
            }`}
          >{enabled ? "On" : "Off"}</button>
        </div>
      </div>

      {msg?.kind === "err" && (
        <p className="mb-3 rounded-xl border border-danger-soft bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">{msg.text}</p>
      )}

      <div className="rounded-2xl border border-line bg-canvas p-4">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-body">Vision helper agent</span>
          <select
            className="w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-primary disabled:opacity-50"
            value={agentId}
            disabled={loading || saving}
            onChange={(e) => { const next = e.target.value; setAgentId(next); save({ agentId: next }); }}
          >
            <option value="">— Select an agent —</option>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </label>

        {enabled && (
          <p className="mt-3 text-xs text-mute">
            The helper transcribes images using its own model. If that model is a remote (BYOK) provider, images
            are sent to that provider — this is the one network egress this feature introduces. A local vision
            model keeps everything on-device.
          </p>
        )}
      </div>
    </div>
  );
}

export default VisionHelperCard;
