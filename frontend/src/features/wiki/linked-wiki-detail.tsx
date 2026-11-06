// Linked-repository detail. A linked wiki has two natures the UI keeps distinct:
// its *content* is read-only (Virtual Worker reads/searches/cites, never edits), and its
// *maintenance/explore commands* are the repo's own code that only run after you
// trust each one. Runs are streamed jobs (SSE), not blocking calls.
// Move-only extraction from wiki-page.tsx; the OperationRow/DiscoveryCard SSE
// plumbing now shares useJobStream.

import { useCallback, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";
import {
  ArgvLine, KindChip, MarkdownPreview, ModalShell, SourceChip, StatePill, TierBadge,
  btnCls, fmtDuration, inputCls, relTime,
  type LinkedOperation, type Page, type PageListItem,
  type PageListPage, type SearchHit, type Wiki,
} from "./wiki-shared";
import { useJobStream } from "./use-job-stream";
import { PendingProposals } from "./proposal-card";
import { createProposal, type OpKind, type ProposedOp, type TrustTier } from "./proposals";

// The read shape from GET /api/wiki/{id} for a linked repository.
interface LinkedWikiRead extends Wiki {
  pages: Page[];
  operations?: LinkedOperation[];
  trustTier?: TrustTier;          // P3; default "readonly" when omitted
  setupPending?: boolean;
  lastSyncAt?: string | null;
  currentRevision?: string | null; // staleness = observedRevision !== currentRevision
}
// A draft in the Add / Edit operation modal (structured, not free-form shell).
interface OperationForm {
  original: string | null;   // the op name being edited, or null when creating
  id: string;
  description: string;
  argvText: string;          // one argv token per line
  cwd: string;
  timeoutSeconds: string;
  mode: "assistant" | "explicit_user";
  kind: OpKind;
  outputLimitBytes: string;
}
interface OperationDraft {
  id: string;
  command: string;
  arguments: string[];
  mode: "assistant" | "explicit_user";
  kind: OpKind;
}

export function LinkRepository({ onLinked, onCancel }: { onLinked: () => void; onCancel: () => void }) {
  const [repoPath, setRepoPath] = useState("");
  const [wikiRoot, setWikiRoot] = useState(".");
  const [name, setName] = useState("");
  const [trustTier, setTrustTier] = useState<TrustTier>("repo-tooling");
  const [actions, setActions] = useState<OperationDraft[]>([]);
  const [probe, setProbe] = useState<{ pageCount: number; revision?: string; instructions: { path: string }[]; operationNames: string[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const probeRepo = async () => { try { setErr(null); setProbe(await apiPost("/api/wiki/link-repository/probe", { repoPath, wikiRoot })); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } };
  const addAction = (id = "", kind: OpKind = "maintenance") => setActions((current) => [...current, { id, command: "", arguments: [""], mode: "explicit_user", kind }]);
  const updateAction = (index: number, change: Partial<OperationDraft>) => setActions((current) => current.map((action, itemIndex) => itemIndex === index ? { ...action, ...change } : action));
  const removeAction = (index: number) => setActions((current) => current.filter((_, itemIndex) => itemIndex !== index));
  const updateArgument = (actionIndex: number, argumentIndex: number, value: string) => {
    const action = actions[actionIndex];
    updateAction(actionIndex, { arguments: action.arguments.map((argument, index) => index === argumentIndex ? value : argument) });
  };
  const buildOperations = () => {
    const operations: Record<string, { argv: string[]; mode: "assistant" | "explicit_user"; kind: OpKind }> = {};
    for (const action of actions) {
      const id = action.id.trim();
      const command = action.command.trim();
      const args = action.arguments.map((argument) => argument.trim()).filter(Boolean);
      if (!id && !command && args.length === 0) continue;
      if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id)) throw new Error("Each action needs a lowercase name, such as bootstrap, sync, or lint.");
      if (!command) throw new Error(`Add the executable for the ${id} action.`);
      if (operations[id]) throw new Error(`The ${id} action was added more than once.`);
      operations[id] = { argv: [command, ...args], mode: action.mode, kind: action.kind };
    }
    return Object.keys(operations).length > 0 ? operations : undefined;
  };
  const link = async () => {
    try {
      setErr(null);
      const operations = buildOperations();
      await apiPost("/api/wiki/link-repository", { repoPath, wikiRoot, name, operations, trustTier }); onLinked();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return <div className="mx-auto max-w-xl p-6">
    <h3 className="font-display text-xl font-semibold text-ink">Link Wiki repository</h3>
    <p className="mt-1 text-sm text-mute">Registers an existing local checkout. Virtual Worker does not clone, index, or run its commands while linking.</p>
    <div className="mt-5 space-y-4">
      <label className="block text-xs font-medium text-body">Local repository path<input className={`${inputCls} mt-1`} value={repoPath} onChange={(e) => setRepoPath(e.target.value)} placeholder="/path/to/wiki" /></label>
      <label className="block text-xs font-medium text-body">Wiki root (relative)<input className={`${inputCls} mt-1`} value={wikiRoot} onChange={(e) => setWikiRoot(e.target.value)} /></label>
      <label className="block text-xs font-medium text-body">Display name <span className="font-normal text-mute">(optional)</span><input className={`${inputCls} mt-1`} value={name} onChange={(e) => setName(e.target.value)} /></label>
      <fieldset className="rounded-xl border border-pressed bg-canvas p-3">
        <legend className="px-1 text-xs font-medium text-body">Trust tier</legend>
        <label className="flex items-start gap-2">
          <input type="radio" name="link-trust-tier" checked={trustTier === "repo-tooling"} onChange={() => setTrustTier("repo-tooling")} className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-warn" />
          <span><span className="text-xs font-semibold text-ink">Repo tooling</span><span className="mt-0.5 block text-[11px] text-warn">Run the repo's own scripts (executables inside the repo) — for repos you maintain or trust.</span></span>
        </label>
        <label className="mt-2 flex items-start gap-2">
          <input type="radio" name="link-trust-tier" checked={trustTier === "readonly"} onChange={() => setTrustTier("readonly")} className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-primary" />
          <span><span className="text-xs font-semibold text-ink">Read-only</span><span className="mt-0.5 block text-[11px] text-mute">Files + read-only shell only — pick this for repos you don't fully trust.</span></span>
        </label>
      </fieldset>
      <div className="flex gap-2"><button onClick={probeRepo} disabled={!repoPath} className={btnCls}>Check repository</button><button onClick={link} disabled={!repoPath} className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Link repository</button><button onClick={onCancel} className={btnCls}>Cancel</button></div>
      {probe && <div className="rounded-xl border border-pressed bg-softer p-3 text-xs text-body"><p>{probe.pageCount} Markdown page(s){probe.revision ? ` · ${probe.revision.slice(0, 12)}` : ""}</p><p className="mt-1">AGENTS: {probe.instructions.map((i) => i.path).join(", ") || "none"}</p><p className="mt-1">Operation candidates (untrusted): {probe.operationNames.join(", ") || "none"}</p></div>}
      <section className="rounded-2xl border border-pressed bg-canvas p-4" aria-labelledby="repository-actions-title">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h4 id="repository-actions-title" className="text-sm font-semibold text-ink">Repository actions <span className="font-normal text-mute">(optional)</span></h4>
            <p className="mt-1 text-xs text-mute">Register only commands that change things (pull, rebuild) or that you want schedulable / one-click. Read-only repo scripts don't need registration — the agent runs them directly under repo tooling.</p></div>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" disabled={actions.some((a) => a.kind === "setup")} onClick={() => addAction("setup", "setup")} className={`${btnCls} disabled:opacity-40`}>+ Setup</button>
            <button type="button" disabled={actions.some((a) => a.kind === "sync")} onClick={() => addAction("sync", "sync")} className={`${btnCls} disabled:opacity-40`}>+ Sync</button>
          </div>
        </div>
        {actions.length === 0 ? <p className="mt-3 text-xs text-mute">No local actions added. You can still link this repository.</p> : <div className="mt-4 space-y-4">
          {actions.map((action, actionIndex) => <div key={actionIndex} className="rounded-xl border border-pressed bg-softer p-3">
            <div className="flex items-start justify-between gap-3"><div className="grid flex-1 gap-3 sm:grid-cols-2">
              <label className="text-xs font-medium text-body">Action
                <select className={`${inputCls} mt-1`} value={action.kind} onChange={(e) => { const kind = e.target.value as OpKind; updateAction(actionIndex, { id: kind, kind }); }}>
                  <option value="setup" disabled={action.kind !== "setup" && actions.some((a) => a.kind === "setup")}>Setup</option>
                  <option value="sync" disabled={action.kind !== "sync" && actions.some((a) => a.kind === "sync")}>Sync</option>
                </select>
              </label>
              <label className="text-xs font-medium text-body">Executable<input className={`${inputCls} mt-1 font-mono`} value={action.command} onChange={(e) => updateAction(actionIndex, { command: e.target.value })} placeholder="e.g. ./scripts/kb" /></label>
            </div><button type="button" onClick={() => removeAction(actionIndex)} className="mt-5 text-xs text-danger hover:underline">Remove</button></div>
            <div className="mt-3"><p className="text-xs font-medium text-body">Arguments</p>
              <div className="mt-1 space-y-2">{action.arguments.map((argument, argumentIndex) => <div key={argumentIndex} className="flex gap-2"><input aria-label={`Argument ${argumentIndex + 1} for ${action.id || "action"}`} className={`${inputCls} font-mono`} value={argument} onChange={(e) => updateArgument(actionIndex, argumentIndex, e.target.value)} placeholder="One argument, e.g. bootstrap" />
                <button type="button" onClick={() => updateAction(actionIndex, { arguments: action.arguments.filter((_, index) => index !== argumentIndex) })} className={btnCls}>Remove</button></div>)}</div>
              <button type="button" onClick={() => updateAction(actionIndex, { arguments: [...action.arguments, ""] })} className={`mt-2 ${btnCls}`}>+ Argument</button>
            </div>
            <label className="mt-3 block text-xs font-medium text-body">When may Virtual Worker run it?
              <select className={`${inputCls} mt-1`} value={action.mode} onChange={(e) => updateAction(actionIndex, { mode: e.target.value as OperationDraft["mode"] })}>
                <option value="explicit_user">Only when I explicitly ask</option>
                <option value="assistant">Assistant may use it while answering</option>
              </select>
            </label>
            <p className="mt-2 truncate font-mono text-[11px] text-mute">Preview: {[action.command, ...action.arguments].filter(Boolean).join(" ") || "Add an executable and arguments"}</p>
          </div>)}</div>}
      </section>
      {err && <p className="text-xs text-danger">{err}</p>}
    </div>
  </div>;
}

export function LinkedWikiDetail({ wiki, onChanged, onDeleted }: { wiki: Wiki; onChanged: () => void; onDeleted: () => void }) {
  const [detail, setDetail] = useState<LinkedWikiRead | null>(null);
  const [operations, setOperations] = useState<LinkedOperation[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [trustTarget, setTrustTarget] = useState<LinkedOperation | null>(null);
  const [editTarget, setEditTarget] = useState<OperationForm | null>(null);
  const [tierModal, setTierModal] = useState(false);
  const [ppNonce, setPpNonce] = useState(0);   // bump → PendingProposals remounts + refetches
  // Header-level run stream for the Sync / Run-setup buttons.
  const opRun = useJobStream(wiki.id);
  // Content-search state, shared with the ContentBrowser pane below.
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchHit[] | null>(null);
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await apiJson<LinkedWikiRead>(`/api/wiki/${wiki.id}`);
      setDetail(r);
      setOperations(r.operations ?? []);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  }, [wiki.id]);
  useEffect(() => { load(); }, [load]);

  const stale = !!(detail?.availabilityError || detail?.lastError);
  const pageCount = detail?.pageCount ?? wiki.pageCount;
  const rev = detail?.observedRevision ?? wiki.observedRevision;
  const lastOp = detail?.lastOperation ?? wiki.lastOperation;
  const trustTier = detail?.trustTier ?? "readonly";
  const setupPending = !!detail?.setupPending;
  const lastSyncAt = detail?.lastSyncAt ?? null;
  const currentRevision = detail?.currentRevision ?? null;
  const outOfSync = !!(currentRevision && rev && currentRevision !== rev);
  const syncOp = operations.find((op) => (op.kind ?? "maintenance") === "sync");
  const setupOp = operations.find((op) => (op.kind ?? "maintenance") === "setup");

  const runHeaderOp = async (name: string) => {
    try {
      const { jobId } = await apiPost<{ jobId: string }>(`/api/wiki/${wiki.id}/operations/${name}`, { inputs: {} });
      opRun.start(jobId);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  const applyTrust = async (name: string, trusted: boolean) => {
    const trustedOperations = operations
      .filter((op) => (op.name === name ? trusted : op.trusted))
      .map((op) => op.name);
    try { await apiPost(`/api/wiki/${wiki.id}/operations/trust`, { trustedOperations }); await load(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  const del = async () => {
    if (!confirm(`Remove the Virtual Worker link to "${wiki.name}"? The repository files are not deleted.`)) return;
    try { await apiJson(`/api/wiki/${wiki.id}`, { method: "DELETE" }); onDeleted(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const reprobe = async () => {
    try { await apiPost(`/api/wiki/${wiki.id}/probe`, {}); await load(); onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  const manual = operations.filter((op) => op.mode === "explicit_user");
  const assistant = operations.filter((op) => op.mode === "assistant");

  const openAdd = () => setEditTarget({
    original: null, id: "", description: "", argvText: "", cwd: ".",
    timeoutSeconds: "30", mode: "assistant", kind: "maintenance", outputLimitBytes: "65536",
  });
  const openEdit = (op: LinkedOperation) => setEditTarget({
    original: op.name, id: op.name, description: op.description, argvText: op.argv.join("\n"),
    cwd: op.cwd ?? ".", timeoutSeconds: String(op.timeoutSeconds ?? 30),
    mode: op.mode, kind: op.kind ?? "maintenance", outputLimitBytes: String(op.outputLimitBytes ?? 65536),
  });

  // Content search — runs the repo's trusted `search` op when configured, else a
  // bounded direct text match. Results show in the ContentBrowser pane below.
  const search = async () => {
    const q = query.trim();
    if (!q) return;
    setSearching(true); setOpenPath(null); setContent("");
    try { setResults((await apiPost<{ results: SearchHit[] }>(`/api/wiki/${wiki.id}/search`, { query: q })).results); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setSearching(false); }
  };
  const openPage = async (path: string) => {
    setOpenPath(path); setResults(null);
    try { setContent((await apiJson<{ content: string }>(`/api/wiki/${wiki.id}/pages/${path}`)).content); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="mx-auto max-w-5xl p-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="font-display text-2xl font-semibold text-ink">{wiki.name}</h3>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-body">
            {stale ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-warn-soft px-2.5 py-1 font-bold text-warn">
                <span className="h-2 w-2 rounded-full bg-warn" />{detail?.availabilityError ? "Sync required" : "Stale"}
              </span>
            ) : outOfSync ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-warn-soft px-2.5 py-1 font-bold text-warn">
                <span className="h-2 w-2 rounded-full bg-warn" />Out of sync
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-success-soft px-2.5 py-1 font-bold text-success">
                <span className="h-2 w-2 rounded-full bg-success" />Up to date
              </span>
            )}
            <TierBadge tier={trustTier} />
            <button onClick={() => setTierModal(true)} className="text-primary hover:underline">Change tier</button>
            <span>Linked repository</span>
            <span><b className="font-bold text-ink">{pageCount.toLocaleString()}</b> page(s)</span>
            {lastSyncAt && <span>last sync {relTime(lastSyncAt)}</span>}
            {rev && <span>rev <b className="font-mono font-bold text-ink">{rev.slice(0, 7)}</b></span>}
            {lastOp && <span>last {lastOp.name} <b className="text-ink">{lastOp.status}</b>{lastOp.finishedAt ? ` · ${relTime(lastOp.finishedAt)}` : ""}</span>}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          {syncOp && (syncOp.trusted ? (
            <button onClick={() => runHeaderOp(syncOp.name)} disabled={opRun.status === "running"}
              className="rounded-xl bg-primary px-3 py-1.5 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-50">Sync now</button>
          ) : (
            <button disabled title="trust the sync operation first"
              className="rounded-xl border border-pressed px-3 py-1.5 text-xs font-medium text-mute opacity-60">Sync now</button>
          ))}
          <button onClick={reprobe} className={btnCls}>Re-probe</button>
          <button onClick={del} className="rounded-xl border border-danger px-3 py-1.5 text-xs font-medium text-danger hover:bg-danger hover:text-white">Remove link</button>
        </div>
      </div>

      {opRun.status !== "idle" && (
        <div className="mt-3 overflow-hidden rounded-xl border border-pressed bg-panel">
          <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2 font-mono text-[11px] text-panel-ink/70">
            {opRun.status === "running" && <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/25 border-t-info" />}
            {opRun.status === "running" ? "running…" : `${opRun.status}${opRun.exitCode !== null ? ` · exit ${opRun.exitCode}` : ""}`}
          </div>
          <div className="max-h-56 overflow-y-auto whitespace-pre-wrap px-3.5 py-2.5 font-mono text-[11px] leading-relaxed text-panel-ink">
            {opRun.lines.length === 0 ? <span className="text-mute">waiting for output…</span> : opRun.lines.map((l, i) => <div key={i}>{l}</div>)}
          </div>
        </div>
      )}

      {(detail?.availabilityError || detail?.lastError) && (
        <p className="mt-3 rounded-xl border border-warn bg-warn-soft px-3 py-2 text-xs text-warn">
          {detail?.availabilityError || detail?.lastError}
        </p>
      )}

      {/* Two-nature strip */}
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <div className="flex items-start gap-3 rounded-2xl border border-pressed bg-canvas p-4">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-success-soft text-base">🔒</span>
          <div>
            <h4 className="font-display text-sm font-semibold text-ink">Content — read-only</h4>
            <p className="mt-1 text-xs text-mute">Virtual Worker reads, searches, and cites pages. It never edits, generates, or re-indexes this repo's content.</p>
          </div>
        </div>
        <div className="flex items-start gap-3 rounded-2xl border border-pressed bg-canvas p-4">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-warn-soft text-base">⚙️</span>
          <div>
            <h4 className="font-display text-sm font-semibold text-ink">Maintenance — the repo's own commands</h4>
            <p className="mt-1 text-xs text-mute">Setup, sync &amp; explore commands belong to the repository. They run only after you approve each one.</p>
          </div>
        </div>
      </div>

      {/* Content search — the headline "Search linked Wiki" feature */}
      <div className="mt-5 flex gap-2">
        <input className={inputCls} value={query} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()} placeholder="Search linked Wiki" />
        <button onClick={search} disabled={searching}
          className="shrink-0 rounded-xl bg-primary px-5 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-50">
          {searching ? "…" : "Search"}</button>
      </div>

      {err && <p className="mt-4 text-xs text-danger">{err}</p>}

      {/* Pending approvals — a human gates each proposed change via the queue */}
      <PendingProposals key={ppNonce} wikiId={wiki.id} onResolved={async () => { await load(); onChanged(); }} />

      {/* Repository operations, grouped */}
      <div className="mt-5 overflow-hidden rounded-2xl border border-pressed bg-canvas">
        <div className="flex items-center justify-between gap-3 border-b border-soft px-5 py-3.5">
          <span className="font-display text-[11px] font-semibold uppercase tracking-wider text-mute">Repository operations</span>
          <span className="text-[11px] text-mute">Defined by the repo or added by you · you control trust</span>
        </div>
        {operations.length === 0 ? (
          <p className="px-5 py-6 text-xs text-mute">No operations yet — add one below to let Virtual Worker run a repository command.</p>
        ) : (
          <>
            {manual.length > 0 && (
              <OperationGroup title="Manual — you run it" hint="side effects · runs only when you trigger it"
                chipClass="bg-warn-soft text-warn" ops={manual} wikiId={wiki.id} showTrust={false}
                onEdit={openEdit} onTrust={setTrustTarget} onUntrust={(op) => applyTrust(op.name, false)} onError={setErr} />
            )}
            {assistant.length > 0 && (
              <OperationGroup title="Assistant may run" hint="the assistant may use these while answering"
                chipClass="bg-primary-soft text-primary" ops={assistant} wikiId={wiki.id} showTrust={true}
                onEdit={openEdit} onTrust={setTrustTarget} onUntrust={(op) => applyTrust(op.name, false)} onError={setErr} />
            )}
          </>
        )}
        <div className="flex items-center justify-between gap-3 border-t border-soft px-5 py-3.5">
          <span className="text-[11px] text-mute">Manual commands run when you click Run. Assistant commands start untrusted — you approve each once.</span>
          <button onClick={openAdd} className={btnCls}>＋ Add operation</button>
        </div>
      </div>

      {setupPending && (
        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-warn bg-warn-soft px-4 py-3 text-xs text-warn">
          <span>Setup has not run yet — pages and tooling may be incomplete.</span>
          {setupOp?.trusted && (
            <button onClick={() => runHeaderOp(setupOp.name)} disabled={opRun.status === "running"}
              className="rounded-xl bg-warn px-3 py-1.5 font-semibold text-white hover:opacity-90 disabled:opacity-50">Run setup</button>
          )}
        </div>
      )}

      {/* Content — search-first + lazy */}
      <ContentBrowser wikiId={wiki.id} pageCount={pageCount} onError={setErr}
        searching={searching} results={results} openPath={openPath} content={content} onOpen={openPage} />

      {trustTarget && (
        <TrustModal op={trustTarget} onCancel={() => setTrustTarget(null)}
          onConfirm={async () => { const t = trustTarget; setTrustTarget(null); await applyTrust(t.name, true); }} />
      )}
      {editTarget && (
        <OperationModal wikiId={wiki.id} form={editTarget} onClose={() => setEditTarget(null)}
          onFiled={() => { setEditTarget(null); setPpNonce((n) => n + 1); }} onError={setErr} />
      )}
      {tierModal && (
        <ChangeTierModal wikiId={wiki.id} current={trustTier} onClose={() => setTierModal(false)}
          onFiled={() => { setTierModal(false); setPpNonce((n) => n + 1); }} onError={setErr} />
      )}
    </div>
  );
}

function ChangeTierModal({ wikiId, current, onClose, onFiled, onError }: {
  wikiId: string; current: TrustTier; onClose: () => void; onFiled: () => void; onError: (m: string) => void;
}) {
  const [toTier, setToTier] = useState<TrustTier>(current === "readonly" ? "repo-tooling" : "readonly");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const escalating = toTier === "repo-tooling";

  const file = async () => {
    setBusy(true);
    try {
      await createProposal({ kind: "trust_tier", wikiId, payload: { fromTier: current, toTier, reason: reason.trim() || undefined } });
      onFiled();
    } catch (e) { onError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };

  return (
    <ModalShell onClose={onClose}>
      <div className="px-5 pt-5">
        <h3 className="font-display text-lg font-semibold text-ink">Change trust tier</h3>
        <p className="mt-1 text-xs text-mute">Files a proposal — approve it in Pending approvals.</p>
      </div>
      <div className="space-y-3 px-5 py-3">
        <label className="flex items-start gap-2">
          <input type="radio" name="change-tier" checked={toTier === "readonly"} onChange={() => setToTier("readonly")} className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-primary" />
          <span><span className="text-xs font-semibold text-ink">Read-only</span><span className="mt-0.5 block text-[11px] text-mute">Read-only files + read-only shell, pinned to this repo.</span></span>
        </label>
        <label className="flex items-start gap-2">
          <input type="radio" name="change-tier" checked={toTier === "repo-tooling"} onChange={() => setToTier("repo-tooling")} className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-warn" />
          <span><span className="text-xs font-semibold text-ink">Repo tooling</span><span className="mt-0.5 block text-[11px] text-warn">Also run the repo's own scripts (executables that resolve inside the repo).</span></span>
        </label>
        {escalating && (
          <div className="rounded-xl border border-danger bg-danger-soft px-3 py-2.5 text-xs text-danger">
            <b className="font-display">⚠ Grants running the repository's own code</b>
            <ul className="mt-1.5 list-disc pl-4">
              <li>May access the network (fetch sources, submodules)</li>
              <li>Runs executables that resolve inside the checkout</li>
              <li>Runs with your Virtual Worker process permissions</li>
            </ul>
          </div>
        )}
        <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (optional)…" className={`${inputCls} text-xs`} />
      </div>
      <div className="flex justify-end gap-2 border-t border-soft px-5 py-3.5">
        <button onClick={onClose} className={btnCls}>Cancel</button>
        <button onClick={file} disabled={busy} className="rounded-xl bg-primary px-4 py-1.5 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-50">{busy ? "Filing…" : "File change"}</button>
      </div>
    </ModalShell>
  );
}

function OperationGroup({ title, hint, chipClass, ops, wikiId, showTrust, onEdit, onTrust, onUntrust, onError }: {
  title: string; hint: string; chipClass: string; ops: LinkedOperation[]; wikiId: string; showTrust: boolean;
  onEdit: (op: LinkedOperation) => void; onTrust: (op: LinkedOperation) => void;
  onUntrust: (op: LinkedOperation) => void; onError: (m: string) => void;
}) {
  return (
    <div className="px-5 pb-1 pt-3">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 pb-1">
        <span className="font-display text-sm font-semibold text-ink">{title}</span>
        <span className="text-[11px] text-mute">{hint}</span>
        <span className={`ml-auto rounded px-2 py-0.5 text-[10px] font-bold ${chipClass}`}>{title.split(" ")[0]}</span>
      </div>
      {ops.map((op) => (
        <OperationRow key={op.name} op={op} wikiId={wikiId} showTrust={showTrust}
          onEdit={() => onEdit(op)} onTrust={() => onTrust(op)} onUntrust={() => onUntrust(op)} onError={onError} />
      ))}
    </div>
  );
}

function OperationRow({ op, wikiId, showTrust, onEdit, onTrust, onUntrust, onError }: {
  op: LinkedOperation; wikiId: string; showTrust: boolean;
  onEdit: () => void; onTrust: () => void; onUntrust: () => void; onError: (m: string) => void;
}) {
  const { status, lines, exitCode, elapsed, start, cancel } = useJobStream(wikiId);
  const [showLog, setShowLog] = useState(true);

  const run = async () => {
    setShowLog(true);
    try {
      const { jobId } = await apiPost<{ jobId: string }>(`/api/wiki/${wikiId}/operations/${op.name}`, { inputs: {} });
      start(jobId);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const needsInputs = Object.keys(op.inputs ?? {}).length > 0;
  const canRun = op.trusted && !needsInputs;

  return (
    <div className="border-t border-soft py-3.5 first:border-t-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-sm font-semibold text-ink">{op.name}</span>
            <SourceChip source={op.source} />
            <KindChip kind={op.kind} />
          </div>
          {op.description && <p className="mt-0.5 text-xs text-mute">{op.description}</p>}
          <div className="mt-2"><ArgvLine argv={op.argv} /></div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button onClick={onEdit} title="Edit operation"
            className="grid h-8 w-8 place-items-center rounded-lg border border-pressed text-mute hover:border-primary hover:text-primary">✎</button>
          {showTrust && (op.trusted ? (
            <button onClick={onUntrust} className={btnCls}>Untrust</button>
          ) : (
            <button onClick={onTrust} className="rounded-xl border border-primary bg-primary-soft px-3 py-1.5 text-xs font-medium text-primary">Trust</button>
          ))}
          {canRun && (
            status === "running"
              ? <button onClick={cancel} className="rounded-xl border border-danger px-3 py-1.5 text-xs font-medium text-danger">Cancel</button>
              : <button onClick={run} className="rounded-xl bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-deep">Run</button>
          )}
        </div>
      </div>

      {showTrust && !op.trusted && (
        <div className="mt-2.5 inline-flex items-center gap-2 rounded-lg bg-warn-soft px-2.5 py-1 text-[11px] text-warn">
          ⚠ Untrusted — assistant can't use until you approve once.
        </div>
      )}

      {status !== "idle" && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-mute tabular-nums">
          <StatePill status={status} />
          {status === "running" && <span>{fmtDuration(elapsed * 1000)}</span>}
          {status === "succeeded" && <><span>exit {exitCode ?? 0}</span><span>ran {fmtDuration(elapsed * 1000)}</span></>}
          {status === "failed" && <span>exit {exitCode ?? "?"}</span>}
          {status === "cancelled" && <span>after {fmtDuration(elapsed * 1000)}</span>}
          {status !== "running" && lines.length > 0 && (
            <button onClick={() => setShowLog((v) => !v)} className="text-primary hover:underline">{showLog ? "hide log" : "show log"}</button>
          )}
        </div>
      )}

      {status !== "idle" && showLog && (
        <div className="mt-2 overflow-hidden rounded-xl border border-pressed bg-panel">
          <div className="flex items-center justify-between border-b border-white/10 px-3 py-2 font-mono text-[11px] text-panel-ink/70">
            <span className="flex items-center gap-2">
              {status === "running" && <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/25 border-t-info" />}
              {op.argv.join(" ")}{status === "running" ? " — running…" : ""}
            </span>
            <span>{fmtDuration(elapsed * 1000)}</span>
          </div>
          <div className="max-h-56 overflow-y-auto whitespace-pre-wrap px-3.5 py-2.5 font-mono text-[11px] leading-relaxed text-panel-ink">
            {lines.length === 0 ? <span className="text-mute">waiting for output…</span> : lines.map((l, i) => <div key={i}>{l}</div>)}
          </div>
        </div>
      )}
    </div>
  );
}

function TrustModal({ op, onCancel, onConfirm }: { op: LinkedOperation; onCancel: () => void; onConfirm: () => void }) {
  const assist = op.mode === "assistant";
  return (
    <ModalShell onClose={onCancel}>
      <div className="px-5 pt-5">
        <h3 className="font-display text-lg font-semibold text-ink">Trust "{op.name}"?</h3>
      </div>
      <div className="px-5 py-3">
        <p className="text-sm text-body">Virtual Worker will be allowed to run this repository command:</p>
        <div className="mt-2"><ArgvLine argv={op.argv} className="block w-full" /></div>
        <div className="mt-3 rounded-xl border border-danger bg-danger-soft px-3 py-2.5 text-xs text-danger">
          <b className="font-display">⚠ Runs the repository's own code</b>
          <ul className="mt-1.5 list-disc pl-4">
            <li>May access the network (fetch sources, submodules)</li>
            <li>Writes files inside the checkout</li>
            <li>Runs with your Virtual Worker process permissions</li>
          </ul>
        </div>
        <p className="mt-2.5 text-xs text-mute">
          {assist
            ? "Assistant mode — once trusted the assistant may run this while answering."
            : "Manual mode — you still run it each time; trust just confirms you approve the command."}
        </p>
      </div>
      <div className="flex justify-end gap-2 border-t border-soft px-5 py-3.5">
        <button onClick={onCancel} className={btnCls}>Cancel</button>
        <button onClick={onConfirm} className="rounded-xl bg-primary px-4 py-1.5 text-xs font-semibold text-white hover:bg-primary-deep">Trust this command</button>
      </div>
    </ModalShell>
  );
}

function OperationModal({ wikiId, form, onClose, onFiled, onError }: {
  wikiId: string; form: OperationForm; onClose: () => void; onFiled: () => void; onError: (m: string) => void;
}) {
  const [f, setF] = useState<OperationForm>(form);
  const [busy, setBusy] = useState(false);
  const set = (change: Partial<OperationForm>) => setF((cur) => ({ ...cur, ...change }));
  const editing = f.original !== null;

  // Saving no longer writes directly — it files an `operations` proposal that a
  // human approves in Pending approvals.
  const save = async () => {
    const id = f.id.trim();
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id)) { onError("Operation id must be lowercase, e.g. recall or sync."); return; }
    const argv = f.argvText.split("\n").map((t) => t.trim()).filter(Boolean);
    if (argv.length === 0) { onError("Add at least one argv token (the executable)."); return; }
    setBusy(true);
    try {
      const spec: ProposedOp = {
        name: id,
        description: f.description.trim(),
        argv,
        cwd: f.cwd.trim() || ".",
        mode: f.mode,
        kind: f.kind,
        timeoutSeconds: Number(f.timeoutSeconds) || 30,
        outputLimitBytes: Number(f.outputLimitBytes) || 65536,
      };
      await createProposal({
        kind: "operations", wikiId,
        payload: { changes: [{ action: editing ? "edit" : "add", name: id, spec }], runOnce: [] },
      });
      onFiled();
    } catch (e) { onError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };

  const del = async () => {
    if (!f.original) return;
    if (!confirm(`Propose removing operation "${f.original}"? Files a proposal to approve; repository files are untouched.`)) return;
    setBusy(true);
    try {
      await createProposal({ kind: "operations", wikiId, payload: { changes: [{ action: "remove", name: f.original }], runOnce: [] } });
      onFiled();
    } catch (e) { onError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };

  const fieldLabel = "mb-1 block text-xs font-semibold text-ink";
  return (
    <ModalShell onClose={onClose}>
      <div className="px-5 pt-5">
        <h3 className="font-display text-lg font-semibold text-ink">{editing ? `Edit "${f.original}"` : "Add operation"}</h3>
        <p className="mt-1 text-xs text-mute">You author a structured command — no free-form shell. Placeholders like <code className="rounded bg-soft px-1 font-mono">{"{query}"}</code> fill one argument.</p>
        <p className="mt-1 text-xs text-mute">Read-only repo scripts don't need an operation — the agent can already run them; register commands with side effects or that you want schedulable.</p>
      </div>
      <div className="space-y-3 px-5 py-3">
        <div>
          <label className={fieldLabel}>Operation id <span className="font-normal text-mute">— lowercase, e.g. recall</span></label>
          <input className={inputCls} value={f.id} onChange={(e) => {
            const id = e.target.value;
            // Auto-match kind to a well-known id, but never fight an explicit choice.
            const kind: Partial<OperationForm> = (f.kind === "maintenance" && (id === "setup" || id === "sync")) ? { kind: id } : {};
            set({ id, ...kind });
          }} placeholder="recall" />
        </div>
        <div>
          <label className={fieldLabel}>Description</label>
          <input className={inputCls} value={f.description} onChange={(e) => set({ description: e.target.value })} placeholder="Search past answered questions" />
        </div>
        <div>
          <label className={fieldLabel}>Command (argv) <span className="font-normal text-mute">— one token per line; {"{name}"} = typed input</span></label>
          <textarea rows={4} className={`${inputCls} font-mono`} value={f.argvText} onChange={(e) => set({ argvText: e.target.value })} placeholder={"./kb\nrecall\nfind\n{query}"} />
        </div>
        <div className="flex gap-3">
          <div className="flex-1"><label className={fieldLabel}>Working dir</label><input className={inputCls} value={f.cwd} onChange={(e) => set({ cwd: e.target.value })} /></div>
          <div className="flex-1"><label className={fieldLabel}>Timeout (s)</label><input className={inputCls} value={f.timeoutSeconds} onChange={(e) => set({ timeoutSeconds: e.target.value })} /></div>
        </div>
        <div className="flex gap-3">
          <div className="flex-1">
            <label className={fieldLabel}>Run mode</label>
            <select className={inputCls} value={f.mode} onChange={(e) => set({ mode: e.target.value as OperationForm["mode"] })}>
              <option value="assistant">Assistant may run it while answering</option>
              <option value="explicit_user">Manual — only when I ask</option>
            </select>
          </div>
          <div className="flex-1">
            <label className={fieldLabel}>Kind</label>
            <select className={inputCls} value={f.kind} onChange={(e) => set({ kind: e.target.value as OpKind })}>
              <option value="setup">Setup</option>
              <option value="sync">Sync</option>
              <option value="maintenance">Maintenance</option>
            </select>
          </div>
        </div>
        <div><label className={fieldLabel}>Output cap (bytes)</label><input className={inputCls} value={f.outputLimitBytes} onChange={(e) => set({ outputLimitBytes: e.target.value })} /></div>
        <div className="rounded-xl border border-warn bg-warn-soft px-3 py-2.5 text-xs text-warn">
          <b className="font-display">Saving files a proposal</b>
          <span className="mt-1 block font-normal">This change isn't applied yet — approve it in Pending approvals. New / edited commands still start untrusted.</span>
        </div>
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-soft px-5 py-3.5">
        {editing && <button onClick={del} disabled={busy} className="mr-auto rounded-xl border border-danger px-3 py-1.5 text-xs font-medium text-danger disabled:opacity-50">Delete</button>}
        <button onClick={onClose} className={btnCls}>Cancel</button>
        <button onClick={save} disabled={busy} className="rounded-xl bg-primary px-4 py-1.5 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-50">{busy ? "Filing…" : "File proposal"}</button>
      </div>
    </ModalShell>
  );
}

function ContentBrowser({ wikiId, pageCount, onError, searching, results, openPath, content, onOpen }: {
  wikiId: string; pageCount: number; onError: (m: string) => void;
  searching: boolean; results: SearchHit[] | null; openPath: string | null;
  content: string; onOpen: (path: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [items, setItems] = useState<PageListItem[]>([]);
  const [total, setTotal] = useState(pageCount);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);

  const fetchPage = useCallback(async (prefix: string, off: number, append: boolean) => {
    try {
      const r = await apiJson<PageListPage>(`/api/wiki/${wikiId}/page-list?prefix=${encodeURIComponent(prefix)}&offset=${off}&limit=50`);
      setItems((prev) => (append ? [...prev, ...r.items] : r.items));
      setTotal(r.total); setHasMore(r.hasMore); setOffset(off + r.items.length);
    } catch (e) { onError(e instanceof Error ? e.message : String(e)); }
  }, [wikiId, onError]);

  // Debounced filter → refetch from the start; empty filter loads the first page.
  useEffect(() => {
    const t = setTimeout(() => { fetchPage(filter.trim(), 0, false); }, 250);
    return () => clearTimeout(t);
  }, [filter, fetchPage]);

  return (
    <div className="mt-5 overflow-hidden rounded-2xl border border-pressed bg-canvas">
      <div className="flex items-center justify-between gap-3 border-b border-soft px-5 py-3.5">
        <span className="font-display text-[11px] font-semibold uppercase tracking-wider text-mute">Content</span>
        <span className="text-[11px] text-mute">Markdown · read-only</span>
      </div>
      <div className="grid gap-4 p-5 md:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
        <div className="min-w-0">
          <input className={`${inputCls} mb-3`} value={filter} onChange={(e) => setFilter(e.target.value)}
            placeholder={`Filter ${total.toLocaleString()} pages by title or path…`} />
          <div className="space-y-0.5">
            {items.length === 0 && <p className="px-2 py-3 text-xs text-mute">No pages match.</p>}
            {items.map((p) => (
              <button key={p.path} onClick={() => onOpen(p.path)} title={p.path}
                className={`block w-full truncate rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${
                  openPath === p.path ? "bg-soft font-semibold text-ink" : "text-body hover:bg-soft hover:text-ink"
                }`}>{p.title || p.path}</button>
            ))}
          </div>
          {hasMore && (
            <button onClick={() => fetchPage(filter.trim(), offset, true)}
              className="mt-2 w-full rounded-lg border border-dashed border-pressed py-2 text-xs font-semibold text-primary hover:bg-soft">
              Load more (showing {items.length.toLocaleString()} of {total.toLocaleString()})
            </button>
          )}
          <p className="mt-3 flex items-start gap-1.5 text-[11px] text-mute">
            💡 <span>Search-first &amp; lazy — Virtual Worker reads bounded page slices per request and never persists a copy.</span>
          </p>
        </div>
        <div className="min-w-0 rounded-xl border border-pressed bg-softer p-4">
          {searching ? (
            <p className="text-xs text-mute">Searching…</p>
          ) : results ? (
            results.length === 0 ? (
              <p className="text-xs text-mute">No matches.</p>
            ) : (
              <div className="space-y-2">
                {results.map((h) => (
                  <button key={h.path} onClick={() => onOpen(h.path)}
                    className="block w-full rounded-xl border border-pressed bg-canvas p-3 text-left hover:border-primary-line">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-semibold text-ink">{h.title}</span>
                      <span className="shrink-0 text-[11px] text-mute">{h.path}</span>
                    </div>
                    {h.snippet && <p className="mt-1 text-xs text-body">{h.snippet}</p>}
                  </button>
                ))}
              </div>
            )
          ) : openPath ? (
            <MarkdownPreview content={content} />
          ) : (
            <p className="text-xs text-mute">Search or select a page to read it.</p>
          )}
        </div>
      </div>
    </div>
  );
}
