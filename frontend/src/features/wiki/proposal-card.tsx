// ProposalCard — one shared approval card for all three linked-wiki proposal
// kinds (link a repo, change operations, change trust tier). It carries forward
// the DiscoveryApprovals language (warn-tinted while awaiting, a StatePill-style
// outcome badge once resolved) and streams any queued run inline over the shared
// useJobStream. Rendered both in the Wiki tab (durable) and inline in chat
// (context="chat").

import { useEffect, useMemo, useState } from "react";
import {
  ArgvLine, KindChip, TierBadge, btnCls, inputCls,
} from "./wiki-shared";
import { useJobStream } from "./use-job-stream";
import {
  approveProposal, isPending, rejectProposal, usePendingProposals,
  type ApproveBody, type LinkPayload, type OperationsPayload, type Proposal,
  type TrustTier, type TrustTierPayload,
} from "./proposals";

// Outcome badge for a resolved proposal — StatePill-styled (success/danger).
function StatusBadge({ status }: { status: Proposal["status"] }) {
  const map: Record<Proposal["status"], { cls: string; dot: string; label: string }> = {
    awaiting_approval: { cls: "bg-warn-soft text-warn", dot: "bg-warn animate-pulse", label: "Awaiting approval" },
    approved: { cls: "bg-success-soft text-success", dot: "bg-success", label: "Approved" },
    executed: { cls: "bg-success-soft text-success", dot: "bg-success", label: "Executed" },
    failed: { cls: "bg-danger-soft text-danger", dot: "bg-danger", label: "Failed" },
    rejected: { cls: "bg-danger-soft text-danger", dot: "bg-danger", label: "Rejected" },
  };
  const m = map[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-bold ${m.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />{m.label}
    </span>
  );
}

// A queued run's inline log — mounts once the proposal reports execution.jobId,
// starts the stream, and renders the dark panel used across the wiki UI.
function ProposalRunLog({ wikiId, jobId }: { wikiId: string; jobId: string }) {
  const { status, lines, start } = useJobStream(wikiId);
  useEffect(() => { start(jobId); }, [start, jobId]);
  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-pressed bg-panel">
      <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2 font-mono text-[11px] text-panel-ink/70">
        {status === "running" && <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/25 border-t-info" />}
        {status === "running" ? "running…" : status}
      </div>
      <div className="max-h-56 overflow-y-auto whitespace-pre-wrap px-3.5 py-2.5 font-mono text-[11px] leading-relaxed text-panel-ink">
        {lines.length === 0 ? <span className="text-mute">waiting for output…</span> : lines.map((l, i) => <div key={i}>{l}</div>)}
      </div>
    </div>
  );
}

export function ProposalCard({ proposal, context, onResolved }: {
  proposal: Proposal; context: "wiki" | "chat"; onResolved?: (updated: Proposal) => void;
}) {
  const [resolved, setResolved] = useState<Proposal | null>(null);
  const p = resolved ?? proposal;
  const pending = isPending(p);

  const initialTier = useMemo<TrustTier>(
    () => (p.kind === "link" ? (p.payload as LinkPayload).requestedTrustTier ?? "readonly" : "readonly"),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const initialChecked = useMemo<Set<string>>(() => {
    if (p.kind === "link") {
      const ops = (p.payload as LinkPayload).operations;
      const keep = new Set<string>();
      // Default to the essentials: manifest-declared ops (explicit author intent)
      // plus the primary setup + sync (prefer the canonically-named op). Variants
      // (bootstrap/update), maintenance, and the rest stay listed but unchecked.
      for (const o of ops) if (o.source === "manifest") keep.add(o.name);
      for (const kind of ["setup", "sync"] as const) {
        const primary = ops.find((o) => o.name === kind && o.kind === kind) ?? ops.find((o) => o.kind === kind);
        if (primary) keep.add(primary.name);
      }
      return keep;
    }
    if (p.kind === "operations") {
      return new Set((p.payload as OperationsPayload).changes.filter((c) => c.action !== "remove").map((c) => c.name));
    }
    return new Set();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [checked, setChecked] = useState<Set<string>>(initialChecked);
  const [tier, setTier] = useState<TrustTier>(initialTier);
  const [note, setNote] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [run, setRun] = useState<{ wikiId: string; jobId: string } | null>(null);

  const toggle = (name: string) => setChecked((s) => {
    const next = new Set(s);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  const approve = async () => {
    setBusy(true); setErr(null);
    try {
      const body: ApproveBody = {};
      if (p.kind === "link" || p.kind === "operations") body.checkedOps = [...checked];
      if (p.kind === "link") body.trustTier = tier;
      else if (p.kind === "trust_tier") body.trustTier = (p.payload as TrustTierPayload).toTier;
      if (note.trim()) body.note = note.trim();
      const updated = await approveProposal(p.id, body);
      setResolved(updated);
      onResolved?.(updated);
      if (updated.execution?.jobId && updated.wikiId) setRun({ wikiId: updated.wikiId, jobId: updated.execution.jobId });
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const reject = async () => {
    setBusy(true); setErr(null);
    try {
      const updated = await rejectProposal(p.id, note.trim() ? { note: note.trim() } : {});
      setResolved(updated);
      onResolved?.(updated);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const chat = context === "chat";
  const shell = pending
    ? "rounded-2xl border border-warn bg-warn-soft"
    : "rounded-2xl border border-pressed bg-canvas";
  const pad = chat ? "p-4" : "p-5";

  return (
    <div className={`${shell} ${chat ? "max-w-full" : ""}`}>
      <div className={pad}>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h4 className={`font-display text-sm font-semibold ${pending ? "text-warn" : "text-ink"}`}>{titleFor(p)}</h4>
          {!pending && <StatusBadge status={p.status} />}
        </div>

        {p.kind === "link" && <LinkBody payload={p.payload as LinkPayload} pending={pending} checked={checked} onToggle={toggle} tier={tier} onTier={setTier} />}
        {p.kind === "operations" && <OperationsBody payload={p.payload as OperationsPayload} pending={pending} checked={checked} onToggle={toggle} />}
        {p.kind === "trust_tier" && <TrustTierBody payload={p.payload as TrustTierPayload} />}

        {p.note && !pending && <p className="mt-3 rounded-lg bg-soft px-3 py-2 text-[11px] text-body"><b className="font-semibold">Note:</b> {p.note}</p>}
        {err && <p className="mt-3 text-xs text-danger">{err}</p>}

        {pending && (
          <>
            <div className="mt-3">
              {noteOpen ? (
                <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)}
                  placeholder="Add a note for the assistant (optional)…" className={`${inputCls} text-xs`} />
              ) : (
                <button onClick={() => setNoteOpen(true)} className="text-[11px] text-primary hover:underline">Add a note</button>
              )}
            </div>
            <div className="mt-3 flex gap-2">
              <button onClick={approve} disabled={busy}
                className="rounded-xl bg-primary px-4 py-1.5 text-xs font-semibold text-white hover:bg-primary-deep disabled:opacity-50">
                {busy ? "…" : approveLabel(p)}</button>
              <button onClick={reject} disabled={busy} className={btnCls}>Reject</button>
            </div>
          </>
        )}

        {run && <ProposalRunLog wikiId={run.wikiId} jobId={run.jobId} />}

        {chat && (
          <p className="mt-3 text-[11px] text-mute">View in Wiki tab to manage this repository.</p>
        )}
      </div>
    </div>
  );
}

function titleFor(p: Proposal): string {
  if (p.kind === "link") return "Link repository";
  if (p.kind === "operations") return "Update operations";
  return "Change trust tier";
}
function approveLabel(p: Proposal): string {
  if (p.kind === "link") return "Approve & link";
  if (p.kind === "trust_tier") return "Approve change";
  return "Approve";
}

// Where a proposed operation came from, so the reviewer can weigh how much to
// trust its argv: the repo declared it, Virtual Worker mined it from docs/conventions, or
// the assistant suggested it after reading the repo.
function OpSourceTag({ source }: { source?: string }) {
  const label = source === "manifest" ? "declared"
    : source === "instruction" ? "from docs"
    : source === "convention" ? "detected"
    : source === "suggested" ? "suggested" : null;
  if (!label) return null;
  return <span className="rounded bg-soft px-1.5 py-0.5 text-[10px] font-medium text-mute">{label}</span>;
}

function LinkBody({ payload, pending, checked, onToggle, tier, onTier }: {
  payload: LinkPayload; pending: boolean; checked: Set<string>;
  onToggle: (name: string) => void; tier: TrustTier; onTier: (t: TrustTier) => void;
}) {
  return (
    <div className="mt-2 space-y-3">
      {payload.reason && <p className="text-xs text-body">{payload.reason}</p>}
      <div className="grid gap-x-4 gap-y-1 text-[11px] text-body sm:grid-cols-2">
        <div><span className="text-mute">Repo path</span><div className="truncate font-mono text-ink">{payload.repoPath}</div></div>
        <div><span className="text-mute">Wiki root</span><div className="truncate font-mono text-ink">{payload.wikiRoot || "."}</div></div>
        <div><span className="text-mute">Pages</span> <b className="text-ink">{payload.pageCount.toLocaleString()}</b>{payload.revision ? <span className="text-mute"> · rev <span className="font-mono">{payload.revision.slice(0, 7)}</span></span> : ""}</div>
      </div>

      {payload.operations.length > 0 && (
        <div className="rounded-xl border border-pressed bg-canvas/60 p-3">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-mute">Operations to register — review &amp; accept</p>
          <div className="space-y-2">
            {payload.operations.map((op) => (
              <label key={op.name} className="flex items-start gap-2">
                <input type="checkbox" disabled={!pending} checked={checked.has(op.name)} onChange={() => onToggle(op.name)}
                  className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-ink">{op.name}</span>
                    <KindChip kind={op.kind} />
                    <OpSourceTag source={op.source} />
                    <span className="text-[10px] text-mute">{op.mode === "assistant" ? "assistant may run" : "you run it"}</span>
                  </span>
                  {op.description && <span className="mt-0.5 block text-[11px] text-mute">{op.description}</span>}
                  <span className="mt-1 block"><ArgvLine argv={op.argv} /></span>
                </span>
              </label>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-xl border border-pressed bg-canvas/60 p-3">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-mute">Trust tier</p>
        <label className="flex items-start gap-2">
          <input type="radio" name={`tier-${payload.repoPath}`} disabled={!pending} checked={tier === "readonly"} onChange={() => onTier("readonly")}
            className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-primary" />
          <span><span className="text-xs font-semibold text-ink">Read-only</span><span className="mt-0.5 block text-[11px] text-mute">Read-only files + read-only shell, pinned to this repo.</span></span>
        </label>
        <label className="mt-2 flex items-start gap-2">
          <input type="radio" name={`tier-${payload.repoPath}`} disabled={!pending} checked={tier === "repo-tooling"} onChange={() => onTier("repo-tooling")}
            className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-warn" />
          <span><span className="text-xs font-semibold text-ink">Repo tooling</span><span className="mt-0.5 block text-[11px] text-warn">Also run the repo's own scripts (executables that resolve inside the repo).</span></span>
        </label>
      </div>
    </div>
  );
}

function OperationsBody({ payload, pending, checked, onToggle }: {
  payload: OperationsPayload; pending: boolean; checked: Set<string>; onToggle: (name: string) => void;
}) {
  const badge: Record<"add" | "edit" | "remove", string> = {
    add: "bg-primary-soft text-primary", edit: "bg-warn-soft text-warn", remove: "bg-danger-soft text-danger",
  };
  return (
    <div className="mt-2 space-y-3">
      {payload.trustTier && <div className="flex items-center gap-2 text-[11px] text-body">Trust tier → <TierBadge tier={payload.trustTier} /></div>}
      {payload.changes.length > 0 && (
        <div className="space-y-2">
          {payload.changes.map((c) => {
            const removing = c.action === "remove";
            return (
              <div key={`${c.action}-${c.name}`} className="rounded-xl border border-pressed bg-canvas/60 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  {!removing && (
                    <input type="checkbox" disabled={!pending} checked={checked.has(c.name)} onChange={() => onToggle(c.name)}
                      className="h-3.5 w-3.5 shrink-0 accent-primary" />
                  )}
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${badge[c.action]}`}>{c.action}</span>
                  <span className="text-xs font-semibold text-ink">{c.name}</span>
                  {c.spec?.kind && <KindChip kind={c.spec.kind} />}
                </div>
                {c.spec?.description && <p className="mt-1 text-[11px] text-mute">{c.spec.description}</p>}
                {c.spec?.argv && <div className="mt-1"><ArgvLine argv={c.spec.argv} className={removing ? "line-through opacity-60" : ""} /></div>}
              </div>
            );
          })}
        </div>
      )}
      {payload.runOnce.length > 0 && (
        <div className="space-y-2">
          {payload.runOnce.map((r, i) => (
            <div key={i} className="rounded-xl border border-warn/40 bg-warn-soft/40 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-warn-soft px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-warn">runs once</span>
                <span className="text-[11px] text-warn">{r.reason}</span>
              </div>
              <div className="mt-1"><ArgvLine argv={r.argv} className="border-warn/40" /></div>
              <p className="mt-2 flex items-start gap-1.5 text-[11px] text-warn/80">⚠ <span>Runs a repository command once — review it before approving.</span></p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TrustTierBody({ payload }: { payload: TrustTierPayload }) {
  const escalating = payload.toTier === "repo-tooling";
  return (
    <div className="mt-2 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-body">
        <TierBadge tier={payload.fromTier} /> <span className="text-mute">→</span> <TierBadge tier={payload.toTier} />
      </div>
      {payload.reason && <p className="text-xs text-body">{payload.reason}</p>}
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
    </div>
  );
}

// The pending queue surface — renders nothing when empty, otherwise a stack of
// ProposalCards. A proposal that queued a run is retained locally so its inline
// log survives the next pending-poll (which no longer returns it).
export function PendingProposals({ wikiId, onResolved }: { wikiId?: string; onResolved: (updated: Proposal) => void }) {
  const { proposals, refresh } = usePendingProposals(wikiId);
  const [sticky, setSticky] = useState<Record<string, Proposal>>({});

  const merged = useMemo(() => {
    const map = new Map<string, Proposal>();
    for (const p of proposals) map.set(p.id, p);
    for (const id of Object.keys(sticky)) map.set(id, sticky[id]);
    return [...map.values()];
  }, [proposals, sticky]);

  if (merged.length === 0) return null;

  return (
    <div className="mt-5 space-y-3">
      {merged.map((p) => (
        <ProposalCard key={p.id} proposal={p} context="wiki"
          onResolved={(updated) => {
            if (updated.execution?.jobId) setSticky((s) => ({ ...s, [updated.id]: updated }));
            refresh();
            onResolved(updated);
          }} />
      ))}
    </div>
  );
}
