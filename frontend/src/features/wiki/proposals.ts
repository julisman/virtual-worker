// Proposals — the unified linked-wiki approval queue (linked-wiki-revamp §5, §9).
// One persisted queue backs three consent-requiring proposal kinds (link a repo,
// change its operations, change its trust tier). A human approves each in the UI;
// no tool can approve. This module is the frozen client contract + the fetch/poll
// hooks the ProposalCard is built on. Job logs still stream over the existing
// /api/wiki/{wikiId}/jobs/{jobId}/stream endpoint via useJobStream.

import { useCallback, useEffect, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

export type ProposalKind = "link" | "operations" | "trust_tier";
export type ProposalStatus = "awaiting_approval" | "approved" | "executed" | "failed" | "rejected";
export type TrustTier = "readonly" | "repo-tooling";
export type OpKind = "setup" | "sync" | "maintenance"; // omitted → "maintenance" (the general bucket)

export interface ProposedOp {
  name: string;
  description: string;
  argv: string[];
  cwd?: string;
  kind?: OpKind;
  mode: "assistant" | "explicit_user";
  source?: string;
  // Optional execution bounds carried on an authored/edited op (OperationSpecIn).
  timeoutSeconds?: number;
  outputLimitBytes?: number;
}
export interface LinkPayload {
  repoPath: string;
  wikiRoot: string;
  name: string;
  description?: string;
  requestedTrustTier: TrustTier;
  operations: ProposedOp[];
  pageCount: number;
  revision?: string;
  reason?: string;
}
export interface OperationsPayload {
  changes: { action: "add" | "edit" | "remove"; name: string; spec?: ProposedOp }[];
  runOnce: { argv: string[]; reason: string }[];
  trustTier?: TrustTier;
}
export interface TrustTierPayload {
  fromTier: TrustTier;
  toTier: TrustTier;
  reason?: string;
}

export interface Proposal {
  id: string;
  kind: ProposalKind;
  status: ProposalStatus;
  wikiId: string | null; // null for link until approved/executed
  createdAt: string;
  decidedAt?: string | null;
  executedAt?: string | null;
  note?: string | null; // user note captured at resolve
  payload: LinkPayload | OperationsPayload | TrustTierPayload;
  result?: Record<string, unknown> | null;
  execution?: { jobId: string } | null;
}

export interface ApproveBody { checkedOps?: string[]; trustTier?: TrustTier; note?: string }
export interface RejectBody { note?: string }

// A proposal is still open (approvable) only while awaiting approval.
export function isPending(p: Proposal): boolean {
  return p.status === "awaiting_approval";
}

export async function listProposals(opts: { status?: string; wikiId?: string } = {}): Promise<Proposal[]> {
  const params = new URLSearchParams();
  if (opts.status) params.set("status", opts.status);
  if (opts.wikiId) params.set("wikiId", opts.wikiId);
  const qs = params.toString();
  const r = await apiJson<{ proposals: Proposal[] }>(`/api/wiki/proposals${qs ? `?${qs}` : ""}`);
  return r.proposals;
}

export function getProposal(id: string): Promise<Proposal> {
  return apiJson<Proposal>(`/api/wiki/proposals/${id}`);
}

export function createProposal(body: { kind: ProposalKind; wikiId?: string; payload: Proposal["payload"] }): Promise<Proposal> {
  return apiPost<Proposal>("/api/wiki/proposals", body);
}

// Approve → updated Proposal (execution.jobId set when a run was queued). A 409
// means the proposal was already resolved elsewhere — re-fetch its final state
// instead of surfacing an error, so the card can render the outcome.
export async function approveProposal(id: string, body: ApproveBody = {}): Promise<Proposal> {
  const res = await apiFetch(`/api/wiki/proposals/${id}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 409) return getProposal(id);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data?.detail ?? data?.error ?? `HTTP ${res.status}`);
  return data as Proposal;
}

export function rejectProposal(id: string, body: RejectBody = {}): Promise<Proposal> {
  return apiPost<Proposal>(`/api/wiki/proposals/${id}/reject`, body);
}

// Fetch one proposal and poll every 3 s while it is still awaiting approval.
export function useProposal(id: string): { proposal: Proposal | null; error: string | null; refresh: () => Promise<void> } {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try { setProposal(await getProposal(id)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, [id]);
  useEffect(() => { refresh(); }, [refresh]);
  usePolling(refresh, 3000, proposal?.status === "awaiting_approval");
  return { proposal, error, refresh };
}

// Fetch the pending queue (optionally scoped to one wiki) and poll every 4 s.
export function usePendingProposals(wikiId?: string): { proposals: Proposal[]; refresh: () => Promise<void> } {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const refresh = useCallback(async () => {
    try { setProposals(await listProposals({ status: "pending", wikiId })); }
    catch { /* best effort — the interval retries */ }
  }, [wikiId]);
  useEffect(() => { refresh(); }, [refresh]);
  usePolling(refresh, 4000);
  return { proposals, refresh };
}
