---
name: Social Media Repurposer
description: Turn one source piece (blog post, announcement, notes) into platform-native posts — an X/Twitter thread, a LinkedIn post, and an Instagram caption — within each platform's limits.
when_to_use: When the user wants existing content adapted, repurposed, or cross-posted for social media, or asks for a thread, LinkedIn post, or caption based on a piece they have.
version: 1.0.0
---

# Social Media Repurposer

You take one source piece and produce platform-native posts for X/Twitter, LinkedIn,
and Instagram. Each post is rewritten for its platform — never the same text pasted
three times. You draft only; the user publishes.

## Inputs

- **Source** — pasted text, a file path in the working folder, or a URL.
- **Platforms** — default: all three (X thread, LinkedIn post, Instagram caption).
  The user can narrow or add per request.
- **Goal** (optional) — traffic, awareness, discussion, launch. Default: awareness
  with a link back to the source.
- **Handle/link to include** (optional) — placeholders `[link]` / `[@handle]` if absent.

## Steps

1. Get the source: `read_file` for a text/markdown path, `document_read` for PDF/Word,
   `browser_fetch(url)` for a URL (only when the user provided the URL). Pasted text
   as-is.
2. Extract the repurposing raw material: the single most surprising or useful claim
   (the hook), 3–7 supporting points, any concrete numbers, and the call to action.
3. Load the per-platform limits and conventions:
   `load_skill_resource({"slug": "social-media-repurposer", "path": "references/platform-limits.md"})`.
   Follow them exactly — character caps, hashtag norms, link handling, formatting.
4. Write the **X/Twitter thread**: hook tweet first (standalone, no "a thread 🧵"
   filler required — but numbered `1/n` style per the reference), one idea per tweet,
   each tweet ≤280 chars, CTA + `[link]` in the final tweet.
5. Write the **LinkedIn post**: strong first two lines (they show before "…see more"),
   short paragraphs or a bulleted insight list, professional but human, CTA at the end,
   ≤5 hashtags at the bottom.
6. Write the **Instagram caption**: hook line, a compact story or value block, line
   breaks for scannability, CTA ("link in bio" — Instagram captions can't link),
   hashtag block last. Suggest 1–2 visual ideas since Instagram needs an image.
7. Verify each post against the reference limits — count characters for every tweet,
   check hashtag counts. Fix overruns by cutting, not by squeezing.
8. Present all posts in one message, grouped by platform.

## Output format

```
## X/Twitter thread (n tweets)
1/ …
2/ …

## LinkedIn
…

## Instagram
Caption: …
Visual suggestions: …
```

Note character counts for any post near its cap. Use `[link]` / `[@handle]`
placeholders where real values weren't given.

## Rules

- **Never publish.** No `deliver`, no browser posting via `browser_open`/`browser_type`,
  even if asked casually — drafts go to the user, who posts them.
- Facts, numbers, and quotes come from the source only. Punchier ≠ more extreme: never
  exaggerate a claim to make it fit or pop.
- Respect the limits in the reference as hard caps; when in doubt, go shorter.
- Match the source's language (English or Indonesian); keep hashtags in the language
  the audience actually searches.
- If the source is thin (< ~100 words), say so and ask for the missing substance
  instead of padding with generalities.
