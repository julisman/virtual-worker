# Digest output template

Use this structure verbatim. Replace `<...>` placeholders; keep the headings and
labels exactly as written. One blank line between blocks.

```markdown
# Trending in <Domain> — <YYYY-MM-DD>

<One sentence: the overall picture across today's topics.>

## 1. <Topic title>
<2-3 sentences: what happened, why it matters, one concrete detail.>
Sources: <url> · <url (optional second)>

## 2. <Topic title>
<2-3 sentences.>
Sources: <url>

<!-- ... repeat through topic N ... -->

---
Method: <N> topics from <k> searches (<query 1>; <query 2>; ...), fetched <m> pages.
```

## Filled example (2 topics, for shape only — never reuse its content)

```markdown
# Trending in AI/Tech — 2026-07-23

Model releases and chip supply dominate this cycle.

## 1. ExampleLab ships Falcon-2 open weights
ExampleLab released Falcon-2 under Apache-2.0, claiming parity with closed models
on MMLU at 34B parameters. It matters because it resets the open-weights baseline
for mid-size models. Weights and a 128k-context variant are on the hub.
Sources: https://example.com/falcon-2

## 2. GPU export rules tighten
Regulators expanded export licensing to cover mid-range accelerators, effective
Q4. Cloud providers say inference pricing in affected regions may rise ~10%.
Sources: https://example.com/gpu-rules · https://example.org/cloud-react

---
Method: 2 topics from 3 searches (AI news this week; trending in AI July 2026;
AI announcements), fetched 3 pages.
```

## Rules for filling it

- Topic titles: 3-10 words, headline style, no trailing period.
- Summaries: exactly 2-3 sentences, each with substance — no "this is interesting".
- `Sources:` lists only URLs actually fetched; separate multiples with ` · `.
- Keep the whole digest plain markdown — no HTML, no tables.
