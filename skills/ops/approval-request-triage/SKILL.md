---
name: Approval Request Triage
description: Triage an internal approval-request link (DMS/ticket/access request) — open it in the logged-in control browser, extract requestor/resource/level/justification, evaluate against the decision policy, and recommend; approve only after explicit user confirmation.
when_to_use: When the user pastes an approval-request URL (access request, DMS ticket, permission grant) and asks whether to approve it, or asks you to review a pending approval link.
version: 1.0.0
---

# Approval Request Triage

Read one approval request, judge it against the decision policy, and hand the user a clear recommendation — you click Approve only when they explicitly say so, and you never click Reject.

## Inputs

- **Request URL** — the approval-request link (required).
- **Context** — anything the user knows about the requestor (optional; e.g. "she's on my team").

## Steps

1. `browser_open` the URL in the control browser (it may already be logged in to the portal). If the control browser is unavailable, stop and tell the user to enable the interactive control browser for this agent — the request page needs their session.
2. If a login wall appears: SSO account-picker clicks are fine (`browser_click` the signed-in account); a real password/2FA prompt means stop and ask the user to finish sign-in in the visible window.
3. `browser_read` (plus `browser_snapshot` if the layout is unclear) and extract:
   - **Requestor** — name and team, if shown
   - **Resource / system** requested
   - **Access level** and **duration** (temporary vs permanent)
   - **Justification** text (present? empty?)
   - **Prior access / request history** — did they hold this exact access before?
4. Load the decision policy: `load_skill_resource({"slug": "approval-request-triage", "path": "references/decision-policy.md"})`. Classify the request per that policy: **approve-recommend** (exact re-grant only) or **flag** (everything else), with reasons.
5. Report the recommendation in the Output format. Do NOT click anything yet.
6. Only if the user explicitly replies to approve: `browser_snapshot` to locate the exact Approve control, `browser_click` it, then `browser_read` the result and confirm the new status. `memory_write` an audit line: `approval-triage | <date> | <requestor> | <resource>/<level> | approved on user confirmation`.
7. If the user declines or wants a reject, tell them to action the reject themselves — you never click Reject.

## Output format

```
Approval triage — <portal/ticket id>
  Requestor:      <name> (<team>)
  Resource:       <resource> — <level>, <duration>
  Justification:  <text or "EMPTY">
  Prior access:   <exact match found | none | unknown>

Recommendation: APPROVE (exact re-grant) | FLAG FOR YOUR REVIEW
Reasons: <one line per policy signal>

I have NOT clicked anything. Reply "approve" to have me click Approve, or handle it yourself at the link.
```

## Rules

- **One automatic-looking action only exists as a recommendation.** No click of Approve without the user's explicit confirmation in this session; no click of Reject ever.
- A wrong approval is a security incident: when data is missing (no history shown, ambiguous level, unreadable page), the recommendation is always FLAG, with "unknown" spelled out.
- Never approve elevated, permanent, or restricted-resource requests even if the user pre-authorized "approve re-grants" — those always come back for confirmation (see the policy file).
- If the page's Approve/Reject controls are ambiguous or unlabeled, do not guess-click; describe what you see and let the user act.
- Audit every actioned approval to memory (step 6). Recommendations that the user rejects need no memory entry.
- Stay on the given URL and its detail pages only; do not wander the portal.
