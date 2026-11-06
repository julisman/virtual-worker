# Decision policy — approval request triage

Classify every request into exactly one bucket. When any signal is missing or
unclear, the bucket is **FLAG** — "unknown" is never grounds to approve.

## APPROVE-RECOMMEND — exact re-grant only

Recommend approval only when **all** of these hold:

- [ ] The requestor **previously held this exact access** — same resource AND
      same access level — and that history is visible on the request page
      (a history section, "previous grant", or an expired identical grant).
- [ ] The access is **time-bounded or standard-duration** (not "permanent"
      unless the prior grant was also permanent for the same resource).
- [ ] The **justification is non-empty** and consistent with the resource
      (e.g. "rejoining project X, need my old access back").
- [ ] The resource is **not restricted/sensitive** (see list below).

Even a clean re-grant is only a *recommendation* — the user still confirms
before any click.

## FLAG — everything else

Flag, with the matching reason(s) quoted in the report:

| Signal | Reason to give |
|---|---|
| No prior-access history shown | "new access — no re-grant history" |
| Higher level than before (read → write, user → admin) | "elevated vs prior grant" |
| Permanent where prior was temporary | "duration escalation" |
| Empty or boilerplate justification | "no meaningful justification" |
| Resource on the restricted list | "restricted resource" |
| Requestor team unknown / cross-team | "requestor not identifiable as same-team" |
| Page unreadable / fields missing | "could not verify — incomplete data" |
| Duplicate of a recently approved request | "possible duplicate" |

## Restricted / sensitive resources — never approve-recommend

Treat as restricted (ask the user to extend this list for their org):

- Production admin or root/superuser access of any kind
- Finance, payroll, or payment systems
- PII / customer-data stores, data-export tools
- Security tooling (IAM consoles, audit logs, secrets managers)
- Anything the user has previously told you is restricted (check `memory_search "restricted resource"`)

## Wording for the recommendation line

- Approve-recommend: `APPROVE (exact re-grant): same resource + level previously held, justification present, not restricted.`
- Flag: `FLAG FOR YOUR REVIEW: <comma-separated reasons from the table>.`
