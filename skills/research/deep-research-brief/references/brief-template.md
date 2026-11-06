# Research brief template

Use this structure verbatim; replace `<...>`. Keep all section headings.

```markdown
# Research brief: <short title>
Date: <YYYY-MM-DD> · Question: <the question, one line>

## Bottom line
<2-3 sentences answering the question as directly as the evidence allows,
with the overall confidence in parentheses, e.g. (confidence: Medium).>

## Findings

### <Sub-question 1>
<Finding paragraph(s). Every factual claim ends with a citation like [1] or [1][3].
Disagreements between sources are stated with both sides cited.>
Assessment: <your inference, clearly separated from sourced facts — omit if none.>
Confidence: <High|Medium|Low> — <why: e.g. 3 independent sources agree / single
vendor source / sources conflict>.

### <Sub-question 2>
<...same shape...>

<!-- one block per sub-question -->

## Open questions
- <What remains unknown or contested, and what evidence would settle it.>
- <...>

## Sources
1. <URL> — <one line: what it is and what it supported>
2. <URL> — <...>
```

## Confidence scale

- **High** — 3+ independent, credible sources agree; or one authoritative primary
  source (official filing, spec, the org's own announcement) with nothing opposing.
- **Medium** — 1-2 credible sources, or sources agree but are secondary/derivative.
- **Low** — single weak source, sources conflict, or the claim is partly inference.

## Filling rules

- Number sources in first-use order; one entry per URL, reused across findings.
- "Bottom line" must commit to an answer shape ("yes, but", "likely X", "evidence
  is split") — never "it depends" without saying on what.
- Open questions must be genuine gaps encountered during research, not filler.
- Keep the whole brief plain markdown; no HTML.
