# Safety

The accounts are real, they belong to a real business, and they are not replaceable.
Everything here exists to keep them alive.

## The kill switch

A file named `PAUSED` next to the control panel (`<data dir>/social/PAUSED`), or in the
worker's working folder. Its contents are the reason. `guard` checks it before cooldowns,
budgets, anything. Remove the file to resume.

## Budgets

| | Cap |
|---|---|
| Public replies to strangers | `daily_cap` per account, default 20 |
| DMs | Uncapped: answering inbound is not a spam signal |
| Comments on the brand's own posts | Uncapped, same reason |
| Prospecting, insight, watch | Uncapped: read-only |

The ramp, from `ramp_start` in the control panel:

| | Replies per day |
|---|---|
| `ramp_start` null | **0**: the account has never been cleared. This is how a read-only trial stays read-only. |
| Days 1 to 7 | 5 |
| Days 8 to 14 | 10 |
| Day 15 on | `daily_cap` |

Only a person edits `ramp_start`. The worker proposes; it does not clear itself to speak.

## Pacing

`pace` waits a fresh random 40 to 120 seconds between public actions on an account. The
randomness matters more than the average: a metronomic cadence is a stronger bot signal
than acting quickly. Never fire a day's budget in one burst; spread it over sessions.

## When a platform pushes back

Any of: an action block, a rate-limit notice, "try again later", a captcha or login
challenge where there was none, a reply that submits but never appears, a reply box that
will not open. Park the account for at least 24 hours:
`python3 social.py cooldown <brand> <platform> 24 "<exactly what you saw>"`.

**Never retry through a block.** Retrying is how a 24-hour limit becomes a permanent
one. Do not switch to another post, do not test whether it still works, do not reduce
volume and continue. Two blocks on one account within a week: stop for the week and
rethink volume.

A tooling failure is not a block: a page that will not load, a timeout, a denied
permission. None of those get a cooldown. Parking an account for a day over a slow page
is a self-inflicted outage.

## Suppression

Run Social Health daily. A shadowbanned account behaves normally from the inside; the
only reliable test is viewing the account's own replies logged out. If they are not
visible: park 3 to 7 days, then resume at week-one volume with `ramp_start` reset by the
owner, not at the volume it was running before.

## Things the worker must never do

- Post original content of any kind
- Put a link, phone number or WhatsApp number in a **public** reply
- Reply to the same post twice, or the same person twice inside the dedupe window,
  across any brand
- Click delete, block, report, or any control that opens a native dialog
- Auto-reply to a complaint, a refund request, or anything about an existing booking
- Edit `ramp_start`, `enabled` or `daily_cap`
- Rewrite or prune the ledgers
- Follow, like or mass-view profiles as a growth tactic: out of scope, and a far
  stronger spam signal than replying

## Weekly human review, fifteen minutes

1. Read ten random replies from `data/actions.jsonl`. Do they sound like a person?
   Would you be happy to have sent them?
2. Check `reports/escalations.md`: anything sitting unanswered?
3. Run the logged-out visibility check on each active account.
4. Confirm no account is running above its intended cap.

If replies have started sounding samey, rewrite the reply shapes; do not reduce volume.
