// ChatProposalCard — hydrates a proposal filed mid-conversation (e.g. a
// wiki_link_propose tool call returned a proposalId) and renders the shared
// ProposalCard inline in the chat bubble. Polls while it awaits approval and
// renders nothing if the proposal can't be fetched (deleted / 404).

import { useState } from "react";
import { ProposalCard } from "@/features/wiki/proposal-card";
import { useProposal, type Proposal } from "@/features/wiki/proposals";

export function ChatProposalCard({ proposalId }: { proposalId: string }) {
  const { proposal, error } = useProposal(proposalId);
  const [resolved, setResolved] = useState<Proposal | null>(null);
  const p = resolved ?? proposal;
  if (error || !p) return null;
  return (
    <div className="mt-2">
      <ProposalCard proposal={p} context="chat" onResolved={setResolved} />
    </div>
  );
}
