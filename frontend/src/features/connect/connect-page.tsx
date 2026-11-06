// Connect — point Claude Code / Codex / Gemini clients at the local gateway.
//
// Reads /api/gateway/info for live upstream status + the resolved model, then
// shows copy-paste setup for each client. Monochrome (Uber-inspired) theme.

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

interface GatewayInfo {
  upstreamUrl: string;
  upstreamReachable: boolean;
  upstreamModels: string[];
  resolvedModel: string | null;
  modelMap: Record<string, string>;
  defaultModel: string | null;
  gatewayKeyRequired: boolean;
  endpoints: Record<string, string>;
}

interface Token {
  id: string; name: string; prefix: string;
  createdAt: string; expiresAt: string | null; lastUsedAt: string | null;
  revoked: boolean; expired: boolean;
}
interface NewToken { id: string; name: string; token: string; prefix: string; expiresAt: string | null; }

const TTL_OPTIONS = [
  { id: "0", label: "Never" },
  { id: "7", label: "7 days" },
  { id: "30", label: "30 days" },
  { id: "90", label: "90 days" },
];

// --------------------------------------------------------------------------- //
// Copy button
// --------------------------------------------------------------------------- //

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch { /* clipboard blocked */ }
      }}
      className="rounded-full border border-pressed px-3 py-1 text-xs font-medium text-body hover:bg-soft hover:text-ink"
    >
      {copied ? "Copied ✓" : "Copy"}
    </button>
  );
}

function CodeCard({ title, desc, code, note, dot, protocol }: {
  title: string; desc: string; code: string; note?: React.ReactNode;
  dot: string; protocol: string;
}) {
  return (
    <div className="rounded-2xl border border-line bg-canvas p-4">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className={`size-2.5 rounded-full ${dot}`} />
        <h3 className="font-display text-sm font-semibold text-ink">{title}</h3>
        <span className="rounded-full bg-primary-soft px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-primary-deep">{protocol}</span>
        <div className="ml-auto"><CopyButton text={code} /></div>
      </div>
      <p className="mb-3 text-xs text-body">{desc}</p>
      <pre className="overflow-x-auto rounded-xl bg-panel p-3 font-mono text-[11px] leading-relaxed text-panel-ink whitespace-pre">{code}</pre>
      {note && <p className="mt-2 text-[11px] leading-relaxed text-mute">{note}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function ConnectPage() {
  const [info, setInfo] = useState<GatewayInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  // tokens
  const [tokens, setTokens] = useState<Token[]>([]);
  const [tokenName, setTokenName] = useState("");
  const [ttl, setTtl] = useState("0");
  // Plaintext of a just-created token — kept in memory only (shown once; the
  // server stores just a hash). Not persisted: gone on reload.
  const [token, setToken] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    localStorage.removeItem("vw:gateway-token"); // tokens are no longer persisted
    apiJson<GatewayInfo>("/api/gateway/info").then(setInfo).catch((e) =>
      setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const loadTokens = useCallback(async () => {
    try {
      const d = await apiJson<{ tokens: Token[] }>("/api/tokens");
      setTokens(d.tokens);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { loadTokens(); }, [loadTokens]);

  const createToken = useCallback(async () => {
    setCreating(true);
    try {
      const rec = await apiPost<NewToken>("/api/tokens", {
        name: tokenName.trim() || "token",
        ttlDays: ttl === "0" ? null : Number(ttl),
      });
      setToken(rec.token);  // shown once, in memory only
      setTokenName("");
      loadTokens();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }, [tokenName, ttl, loadTokens]);

  const revokeToken = useCallback(async (id: string, prefix: string) => {
    if (!confirm("Revoke this token? Clients using it will stop working.")) return;
    await apiJson(`/api/tokens/${id}`, { method: "DELETE" }).catch(() => {});
    if (token && token.startsWith(prefix)) setToken(null);
    loadTokens();
  }, [loadTokens, token]);

  // Clients connect to the gateway. In packaged/stable the origin IS the
  // gateway; in dev the Vite server proxies /v1 + /api to the backend, so the
  // origin works there too — no port to hardcode or keep in sync.
  const base = useMemo(() => window.location.origin, []);

  const activeTokens = tokens.filter((t) => !t.revoked && !t.expired);
  const keyRequired = (info?.gatewayKeyRequired ?? false) || activeTokens.length > 0;
  const model = info?.resolvedModel || "your-local-model";
  // Use the saved token if we have it; else a placeholder when required.
  const key = token || (keyRequired ? "<your-token>" : "local");

  const claudeCode = `export ANTHROPIC_BASE_URL=${base}
export ANTHROPIC_AUTH_TOKEN=${key}
export ANTHROPIC_MODEL=${model}
export ANTHROPIC_SMALL_FAST_MODEL=${model}
# keep the KV cache warm on local models (see note) — ~90% faster
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export CLAUDE_CODE_ENABLE_TELEMETRY=0
claude`;

  const codex = `# ~/.codex/config.toml
model = "${model}"
model_provider = "virtual-worker"

[model_providers.virtual-worker]
name = "Virtual Worker"
base_url = "${base}/v1"
wire_api = "responses"
env_key = "VW_KEY"

# then:  export VW_KEY=${key}  &&  codex`;

  const gemini = `from google import genai

client = genai.Client(
    api_key="${key}",
    http_options={"base_url": "${base}"},
)
resp = client.models.generate_content(
    model="${model}", contents="Hello",
)
print(resp.text)

# or curl:
# curl "${base}/v1beta/models/${model}:generateContent?key=${key}" \\
#   -H 'content-type: application/json' \\
#   -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'`;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex flex-wrap items-start gap-4 border-b border-pressed bg-softer px-6 py-4">
        <div>
          <h1 className="font-display text-2xl font-bold tracking-tight text-ink">Connect</h1>
          <p className="mt-0.5 text-sm text-body">
            Use your local model from Claude Code, Codex, and Gemini clients — fully offline.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <StatusBadge
            ok={!!info?.upstreamReachable}
            label={info?.upstreamReachable ? "Upstream online" : "Upstream offline"}
          />
          <span className="rounded-full border border-line bg-canvas px-3 py-1 text-xs text-body">
            Model: <span className="font-mono text-ink">{model}</span>
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
        {/* Gateway base URL */}
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl bg-panel px-4 py-3 text-panel-ink">
          <span className="text-xs font-semibold uppercase tracking-wide text-hairline">Gateway</span>
          <code className="min-w-0 flex-1 overflow-x-auto font-mono text-sm text-panel-ink">{`${base}/v1`}</code>
          <CopyButton text={`${base}/v1`} />
        </div>

        {error && (
          <div className="mb-4 rounded-xl border border-danger bg-danger-soft p-3 font-mono text-[11px] text-danger">{error}</div>
        )}
        {!info?.upstreamReachable && (
          <div className="mb-4 rounded-xl border border-line bg-soft p-3 text-xs text-mute">
            Upstream model server not detected. Start one (<span className="font-mono">./dev.sh</span>) or
            download &amp; activate a model in Hub before connecting.
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <CodeCard
            title="Claude Code"
            dot="bg-warn"
            protocol="Anthropic API"
            desc="Set the env vars, then run claude. Maps both the main and small/fast model to your local one."
            code={claudeCode}
            note={
              <>
                🕵️ <span className="font-medium text-body">Why disable the attribution header:</span> Claude Code
                prepends a <span className="font-mono">x-anthropic-billing-header</span> line to the system prompt
                whose value changes every request, so the whole prompt prefix misses the KV cache each turn — up
                to ~90% slower on local models. <span className="font-mono">CLAUDE_CODE_ATTRIBUTION_HEADER=0</span>{" "}
                turns it off. Equivalent inline:{" "}
                <span className="font-mono">{'claude --settings \'{"env":{"CLAUDE_CODE_ATTRIBUTION_HEADER":"0"}}\''}</span>.
              </>
            }
          />
          <CodeCard
            title="Codex CLI"
            dot="bg-success"
            protocol="Responses API"
            desc="Codex only speaks the Responses API — wire_api must be 'responses'."
            code={codex}
          />
          <CodeCard
            title="Gemini / Antigravity"
            dot="bg-info"
            protocol="google-genai"
            desc="Any google-genai client with a custom base_url. Works for curl and the SDK."
            code={gemini}
          />
        </div>

        {/* API tokens */}
        <div className="mt-4 rounded-2xl border border-line bg-canvas p-4">
          <h3 className="font-display text-sm font-semibold text-ink">API tokens</h3>
          <p className="mt-0.5 mb-3 text-xs text-body">
            Generate a token for your agents to authenticate. The gateway accepts any key until
            the first token exists; after that, a valid token is required.
          </p>

          {/* Create */}
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={tokenName}
              onChange={(e) => setTokenName(e.target.value)}
              placeholder="Token name (e.g. claude-code)"
              className="min-w-[180px] flex-1 rounded-full border border-pressed bg-soft px-4 py-1.5 text-sm text-ink outline-none placeholder:text-mute focus:border-primary"
            />
            <select
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              className="rounded-full border border-pressed bg-canvas px-3.5 py-1.5 text-sm text-body outline-none hover:bg-soft focus:border-primary"
            >
              {TTL_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
            <button
              onClick={createToken}
              disabled={creating}
              className="rounded-xl bg-primary px-4 py-1.5 text-sm font-semibold text-white shadow-[0_8px_18px_-8px_rgba(43,60,130,0.7)] hover:bg-primary-deep disabled:opacity-40"
            >{creating ? "Creating…" : "Create token"}</button>
          </div>

          {/* Just-created token — shown once (not saved) */}
          {token && (
            <div className="mt-3 rounded-xl border border-primary-line bg-primary-soft p-3">
              <p className="mb-1 text-xs font-semibold text-primary-deep">✓ New token created — copy it now, it won't be shown again.</p>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto rounded-lg bg-panel px-3 py-1.5 font-mono text-xs text-panel-ink">{token}</code>
                <CopyButton text={token} />
              </div>
              <p className="mt-1 text-[11px] text-body">The snippets above use it now. It's not stored — reload clears it (the server keeps only a hash).</p>
            </div>
          )}

          {/* List */}
          <div className="mt-3 space-y-1.5">
            {tokens.length === 0 && <p className="py-2 text-center text-xs text-mute">No tokens yet.</p>}
            {tokens.map((t) => (
              <div key={t.id} className="flex items-center gap-3 rounded-xl border border-line bg-softer p-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="font-medium text-ink">{t.name}</span>
                    <span className="font-mono text-xs text-mute">{t.prefix}…</span>
                    {t.expired && <span className="rounded-full border border-pressed px-2 py-0.5 text-[10px] text-mute">expired</span>}
                  </div>
                  <div className="text-[11px] text-mute">
                    {t.expiresAt ? `expires ${fmtDate(t.expiresAt)}` : "never expires"}
                    {t.lastUsedAt ? ` · last used ${fmtDate(t.lastUsedAt)}` : " · never used"}
                  </div>
                </div>
                <button
                  onClick={() => revokeToken(t.id, t.prefix)}
                  className="rounded-full border border-pressed px-3 py-1 text-xs font-medium text-danger hover:bg-danger-soft"
                >Revoke</button>
              </div>
            ))}
          </div>
        </div>

        {info && Object.keys(info.modelMap).length > 0 && (
          <div className="mt-4 rounded-2xl border border-line bg-canvas p-4">
            <h3 className="mb-2 font-display text-sm font-semibold text-ink">Model map</h3>
            <div className="space-y-1 font-mono text-xs text-body">
              {Object.entries(info.modelMap).map(([k, v]) => (
                <div key={k}><span className="text-ink">{k}</span> → {v}</div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function fmtDate(iso: string): string {
  try { return new Date(iso).toLocaleDateString(); } catch { return iso; }
}

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs ${
        ok ? "border-line bg-success-soft text-ink" : "border-pressed text-mute"
      }`}
    >
      <span className={`size-2 rounded-full ${ok ? "bg-success" : "bg-mute"}`} />
      {label}
    </span>
  );
}

export default ConnectPage;
