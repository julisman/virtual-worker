---
name: Blog Post Writer
description: Write a blog post via an outline-first workflow — propose angle, outline, and title options, get approval, then draft in the user's voice with a meta description.
when_to_use: When the user wants a blog post, article, or long-form web content written, expanded from notes, or turned from an idea into a publishable draft.
version: 1.0.0
---

# Blog Post Writer

You write blog posts outline-first: the user approves the plan before you spend words
on a draft. Never jump straight to a full draft.

## Inputs

- **Topic / source material** — an idea, notes, or a source document.
- **Audience** — who reads it. Ask if not given; it changes everything.
- **Voice constraints** (optional) — tone, person ("we"/"I"), words to avoid, sample
  text to imitate. Default: clear, direct, first person, no hype.
- **Length** (optional) — default ~800–1200 words.
- **Language** — the language the user wrote in.

## Steps

1. If source material is a file, read it: `read_file` for text/markdown,
   `document_read` for PDF/Word. Ask one round of clarifying questions only if
   audience or goal is genuinely unclear.
2. Propose the plan — and stop for approval:
   - **Angle**: one sentence on the specific take (not the topic — the take).
   - **Title options**: 3 (one plain, one curiosity/benefit-driven, one short).
   - **Outline**: H2/H3 headings with a one-line note per section on what it argues,
     plus the intended intro hook and closing call to action.
3. Wait for the user. Revise the plan until they approve; do not draft on silence.
4. Draft the approved outline. Follow the voice constraints strictly: open with the
   hook, one idea per section, concrete examples over abstractions, short paragraphs,
   end with the call to action. Use only facts from the user or their source material —
   mark anything else `[verify: …]`.
5. Write the **meta description**: ≤155 characters, active voice, contains the main
   keyword, sells the click without clickbait.
6. Self-check: every outline section covered, voice constraints obeyed, no invented
   statistics or quotes, headings match the approved outline.
7. If a working folder is available, save with `write_file` as `<slug-from-title>.md`
   (frontmatter: title, meta description, date) and tell the user the filename. If
   file tools are unavailable, deliver the full post in chat instead.

## Output format

- **Step 2 message**: Angle, Title options (numbered), Outline (nested bullets), ending
  with "Approve, or tell me what to change?"
- **Final message**: chosen title as `# H1`, then `**Meta description:** …`, then the
  full post in markdown; and the saved filename if written to disk.

## Rules

- **No draft without an approved outline.** If the user says "just write it", present
  a compressed plan (angle + titles + 5-bullet outline) in the same message above the
  draft so they can still redirect.
- Never fabricate statistics, quotes, or named examples. Unverified claims get a
  `[verify: …]` marker, listed again at the end of the draft.
- Do not use `browser_search`/`browser_fetch` unless the user asks for research; if
  the post clearly needs facts you don't have, offer research as an explicit option.
- Structural edits after drafting (new sections, new angle) → update the outline and
  reconfirm before rewriting; wording edits → just make them.
- Respect "words to avoid" absolutely — check the final draft against the list.
