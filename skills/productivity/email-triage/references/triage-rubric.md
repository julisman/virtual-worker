# Email triage rubric

Assign exactly ONE bucket per message. Test in order — first match wins.

## 🔴 Urgent — all three must hold

1. A real person (not an automated sender) is asking **this user** for something, and
2. the ask has a deadline within ~48h, is blocking someone, or is from the user's
   manager/key client/on-call context, and
3. missing it has a real cost (money, escalation, a person left waiting).

Examples: "need your sign-off before EOD", a client reporting an outage, boss asking
for numbers "before the 3pm call", a calendar-critical change ("can we move today's
meeting?").

## 🟡 Needs action — the user must do something, but not today-or-else

- A direct question or request with no near deadline.
- Review/approval requests with slack ("by next week").
- Scheduling threads awaiting the user's availability.
- Bills/renewals due more than 48h out.
- Threads where the user is on To: and the last message ends in a question to them.

## ⚪ FYI — no action expected from the user

- Newsletters, digests, marketing, product updates.
- Automated notifications (CI, Jira, GitHub, calendar accepts, receipts, shipping).
- Threads where the user is only CC'd and nothing is asked of them.
- "Thanks!" / closing messages.

## Tie-breakers and traps

- **Manufactured urgency is not urgency.** "URGENT: your invoice" from an unknown or
  automated sender, "last chance" marketing, phishing-style pressure → FYI. Real
  urgency comes from a known human + a concrete ask.
- **CC ≠ action.** On CC with no direct question → FYI, even on an important thread.
- **Deadline already passed** → still Urgent (flag as overdue), unless the thread shows
  it was resolved.
- **Uncertain between Urgent and Needs action** → Needs action, and say why you were
  unsure in the summary line.
- **Multiple messages in one thread** → classify the thread once, by its latest state.
