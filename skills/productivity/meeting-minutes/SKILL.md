---
name: Meeting Minutes
description: Turn a raw meeting transcript or notes (pasted, or read via read_file/document_read) into clean minutes — attendees, decisions, action items with owners and due dates — and offer to create each action item on the task board.
when_to_use: When the user pastes a transcript or meeting notes, points at a transcript file, or asks for minutes, a recap, decisions, or action items from a meeting.
version: 1.0.0
---

# Meeting Minutes

Convert one meeting's raw record into structured minutes and optionally push its action items to the task board.

## Inputs

- **Source** — pasted text in chat, a path in the working folder (`read_file`), or a document (`document_read` for PDF/Word/Excel). If the user names a file you can't find, `list_dir` / `glob` for it once, then ask.
- **Meeting metadata** — title, date, attendees. Extract from the source; ask only for what's genuinely absent (at minimum confirm the date if not in the source — use the session clock's today as the suggested default).

## Steps

1. **Read the source.** Use `read_file` or `document_read` as appropriate; for pasted text use it directly. If the transcript is very long, process it in order and keep a running list of candidates — do not summarize from memory of a skim.
2. **Extract, with evidence.** Walk the transcript and collect:
   - **Attendees** — named speakers plus people described as present.
   - **Decisions** — statements where the group settled something ("we'll go with X", "approved", "let's not do Y"). A proposal that got no agreement is NOT a decision — put it under Open questions.
   - **Action items** — concrete commitments: what, **owner** (the person who took it, not who suggested it), **due date**. Convert relative dates ("by Friday") to absolute `YYYY-MM-DD` using the meeting date. Missing owner/date → write `unassigned` / `no date` — never invent them. Split "X and Y" commitments into separate items.
   - **Open questions** — anything explicitly deferred or unresolved.
   When two extracted items restate the same commitment, keep the more specific one.
3. **Compose minutes.** Fill the template — `load_skill_resource({"slug": "meeting-minutes", "path": "references/minutes-template.md"})`. Keep the discussion summary to 3-6 bullets; minutes are a record, not a re-narration.
4. **Show the minutes** in chat as one message.
5. **Offer task creation.** If action items exist and this agent has task-board tools, ask: "Create these <n> action items on the task board?" List exactly what each `task_create` would contain (title = action, description = owner + meeting reference, due date). Only after the user confirms, call `task_create` per item and report the created IDs. No task tools → skip the offer, or suggest `memory_write` for follow-ups instead.
6. **Optionally save.** If the user asks, `write_file` the minutes as `minutes-<YYYY-MM-DD>-<slug>.md` in the working folder, or `deliver` them to a channel the user names.

## Output format

Exactly the structure in `references/minutes-template.md`:
- header: title, date, attendees;
- `## Decisions` (numbered, complete statements);
- `## Action items` — table with columns # / Action / Owner / Due;
- `## Discussion summary` (3-6 bullets), `## Open questions`.
Write minutes in the language of the transcript. When offering task creation, show the exact `task_create` payload per row before asking to confirm.

## Rules

- **Ground everything.** Every decision and action item must be traceable to the source text. If the transcript is ambiguous about who owns an action, mark `unassigned` and flag it — do not assign by guess.
- Attribute decisions to the group, not individuals, unless the transcript clearly shows a single decider.
- Do not include verbatim sensitive chatter (salary talk, personal remarks) in minutes; note "off-topic discussion omitted" if a chunk is skipped.
- `task_create` only after explicit confirmation, and only the items shown — never silently add extras.
- Multiple meetings in one source → produce one minutes block per meeting; never merge two meetings into one record.
- Minutes stay local: `deliver` them only to a target the user explicitly names, and never before the user has seen the draft in chat.
- If the source is not actually a meeting record (e.g. an article), say so and ask what the user wants instead of forcing the template.
- If `document_read` fails on a file (corrupt, unsupported), report the exact failure and ask for a paste — do not reconstruct content from the filename.
