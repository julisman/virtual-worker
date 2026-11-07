---
name: Social Core
description: Shared guardrails for the social lead-gen skills — the social.py helper (guard, budget, dedupe, pacing, ledgers, kill switch), the brand kit layout, platform notes and the safety runbook. Attach it with any social-* skill.
when_to_use: Whenever a social-* skill runs (they load social.py from here), when setting up a new brand (init, check), or when the user asks how the social lead-gen guardrails, budgets, ramp or kill switch work.
version: 1.0.0
---

# Social Core

The social lead-gen skills find people publicly asking for what a business sells,
reply to them helpfully, answer DMs and comments on the business's own posts, and
route everyone into WhatsApp. They drive a real, logged-in browser as the brand, so
most of this pack is guardrails, on purpose: an agent replying to strangers from a
real business account can get it suppressed, say something wrong in public, or
quietly do nothing useful. The rules are code with exit codes, not prompts.

## The helper: `social.py`

Every social skill uses one script. Make sure it is in the working folder:

1. If `social.py` is not in the working folder, fetch it:
   `load_skill_resource({"slug": "social-core", "path": "scripts/social.py"})`
   and save it with `write_file` as `social.py`.
2. Run commands with `shell("python3 social.py <command> …")`.

| Command | What it answers |
|---|---|
| `guard <brand> <platform>` | **May I act as this account right now?** Exit 0 = yes (prints JSON with `replies_remaining_today`); exit 1 = no, stop that account for this run; exit 2 = bad config. Checks, in order: the kill switch, a cooldown, `enabled`, then the budget. |
| `budget <brand> <platform>` | Public replies still allowed today. `0` until a person sets `ramp_start`. |
| `seen <platform> <post_id> <author>` | Dedupe gate, before every public reply. Exit 1 = skip: this post was answered before, or this person inside the dedupe window, by any brand. |
| `seen-add <platform> <post_id> <author> <brand>` | Record a reply **immediately after it lands**, never batched. |
| `lead-add --json '{…}'` | Append a lead (prospecting's deliverable). Requires platform, brand, author, post_url, text, intent (high or medium). `status` defaults to `collected`; use `research` for a product the brand kit cannot answer yet. |
| `log --json '{…}'` | Audit log of everything that touched a human; requires brand, platform, kind. The budget counts `public_reply`; DM and comment replies are uncapped and use their own kinds. |
| `cooldown <brand> <platform> <hours> "<reason>"` | Park an account the moment the platform pushes back. |
| `pace [--check] <brand> <platform>` | Human rhythm: a fresh random 40 to 120 s gap since the last public action. `--check` prints the seconds still owed; without it the script waits up to 50 s and prints what is still owed, so call it until it owes 0. |
| `wa-link <brand> <platform> [topic]` | The WhatsApp hand-off link plus the readable number. |
| `due [brand]` | Which skills are due now (working hours aware). |
| `status`, `check`, `init --brand <slug>` | State summary, config validation, first-time scaffold. |

### Where things live, and why

- **Control panel** `config.json`, in the Virtual Worker data directory under `social/`
  (not in the working folder). Handles, `enabled`, `daily_cap`, `ramp_start`, the
  WhatsApp number, working hours. **You cannot write it; only a person edits it.** That
  is deliberate: `ramp_start` decides whether an account may speak in public at all,
  and an agent must never clear itself to start talking to strangers. If something there
  is wrong, say so and stop.
- **Kill switch** a `PAUSED` file next to the control panel, or in the working folder.
  `guard` checks it first.
- **Ledgers** `data/leads.jsonl`, `data/seen.jsonl`, `data/actions.jsonl`,
  `data/watch.jsonl` in the working folder, append-only through `social.py`. Never
  rewrite or prune them by hand. If something looks wrong, say so and stop.
- **Brand kits** `brands/<slug>/BRAND.md` (what we sell and the voice),
  `KEYWORDS.md` (queries, negative filters, seeker-vs-seller), `REPLIES.md` (reply
  shapes with examples). Prose, written by the owner. Read the kit before acting for a
  brand; never claim a price, item or policy that is not in it.

## The six rules

1. Run `guard` before touching a browser tab. Exit 1 means stop that account for this run.
2. Run `seen` before every public reply. Exit 1 means skip that lead entirely.
3. **No links in public replies, ever.** The WhatsApp link lives in the bio. Links in DMs are fine.
4. **Never retry through a platform block.** An action block, captcha, login challenge or
   "try again later" means `cooldown … 24 "<what you saw>"` and move on. A tooling
   failure (page did not load, a timeout) is not a platform block and gets no cooldown.
5. **Log every action that touched a human, immediately.** The budget counts the log, so
   an unlogged reply is an over-budget reply.
6. **Never send someone to WhatsApp without the number.** Answer their question, give
   the number in readable digits, ask one qualifying question. In public: no number,
   answer and point at the bio.

## Browser rules

- These skills need the interactive control browser (`browser_open`, `browser_snapshot`,
  `browser_click`, `browser_type`, `browser_screenshot`), logged in to the brand's
  accounts. Attach mode to the owner's own Chrome is ideal; launch mode works once the
  profile is signed in from the Browser card.
- Confirm the logged-in account on screen at the start of every run. One brand must
  never reply as another.
- Step through anything that writes: click, type, **screenshot and read it**, then
  submit. Never batch a write.
- Never click anything that opens a native dialog (delete, block, report, discard).
- If a page will not load or an element will not respond after two or three attempts,
  stop and report. Do not keep hammering it.

## Setup, once per brand

1. `shell("python3 social.py init --brand <slug>")` creates the control panel if missing,
   the ledgers, and a brand kit skeleton.
2. Ask the owner to edit the control panel (handles, `enabled`, WhatsApp number; leave
   `ramp_start` null for a read-only trial) and to write the brand kit.
3. `shell("python3 social.py check")` until it says the config is usable.
4. Suggest schedules (the owner or you with the schedule tools): DM triage every 30
   minutes, prospecting hourly, comments every 3 hours, health daily, inside working
   hours. `due` tells you what is owed at any moment.

References in this pack: `references/platform-notes.md` (per-platform UI recipes
and traps, learned in use), `references/safety.md` (budgets, ramp, pacing, blocks).
