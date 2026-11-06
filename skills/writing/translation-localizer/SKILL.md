---
name: Translation Localizer
description: Translate between English and Indonesian (or other stated pairs) with real localization — dates, currency, formality (Anda vs kamu), idioms — using a per-user glossary kept in memory.
when_to_use: When the user asks to translate, localize, or adapt text between languages, especially English ↔ Indonesian, and terminology consistency matters.
version: 1.0.0
---

# Translation Localizer

You translate and localize: the output reads as if written natively in the target
language, with terminology consistent across sessions via a glossary in memory.

## Inputs

- **Text** — pasted, or a file path (`read_file`; `document_read` for PDF/Word).
- **Direction** — source → target. Default pair EN ↔ ID; other pairs when stated.
- **Formality (Indonesian target)** — formal `Anda` vs casual `kamu`. Not stated →
  infer from the text type (business/legal → formal; social/chat copy → casual) and
  say which you chose.
- **Audience/region** (optional) — affects examples, currency, and idiom choices.

## Steps

1. Get the source text and confirm the direction if ambiguous.
2. Check the glossary: `memory_search("glossary <term>")` for the document's recurring
   or product-specific terms, plus `memory_search("translation glossary")` for the
   user's general entries. Confirmed pairs override your own word choice.
3. Translate for meaning, not word order. Localize as you go:
   - **Dates**: target convention — ID "17 Agustus 2026"; EN-US "August 17, 2026".
     Never reorder ambiguous numerics (03/04/2026) silently — keep and flag, or ask.
   - **Currency**: convert format, not value — "Rp1.500.000" / "IDR 1,500,000" /
     "$25" stay the same amount; never apply an exchange rate unless asked.
   - **Numbers**: swap decimal/thousand separators to the target convention.
   - **Formality**: apply the chosen register consistently; never mix `Anda`/`kamu`.
   - **Idioms**: translate to the target-language equivalent, not literally; no
     equivalent → plain-language meaning.
   - **Keep untranslated**: product names, brand names, technical terms of art, code,
     UI strings in quotes — unless the glossary maps them.
4. Collect new term decisions you had to make (recurring domain terms, tricky
   renderings). List them under "New terms" with your chosen translation.
5. After the user confirms a term (or corrects one), persist it:
   `memory_write("glossary: <source term> -> <target term> (<language pair>, <context>)")`.
   Corrections to an old entry: `memory_forget` the stale one, then write the new one.
   Do not write unconfirmed guesses to memory.
6. Present the translation. Long documents from files: offer to save with
   `write_file` as `<name>.<target-lang>.md`.

## Output format

```
**Translation (<source> → <target>, <formal|casual>)**
<full translated text>

**Localization notes** (only when non-obvious choices were made)
- <what and why, one line each>

**New terms — confirm to save to your glossary**
- <source> → <target>
```

Short snippets (≤2 sentences): just the translation, notes only if needed.

## Rules

- Meaning is sacred: no additions, omissions, or register-driven softening of
  commitments and numbers. Amounts never change value.
- Glossary entries the user confirmed always win over your preference.
- Untranslatable or ambiguous source (typos, unclear antecedents): ask, or translate
  the most likely reading and flag it — never silently guess on contracts or legal
  text; recommend professional review for legally binding documents.
- Mixed-language sources: translate everything into the single target language unless
  the user wants the mix preserved.
- Memory writes are limited to glossary terms and the user's stated formality
  preference — never store the translated content itself.
