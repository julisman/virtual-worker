---
name: Web Form Filler
description: Fill a web form in the interactive control browser — map the fields from a snapshot, type the user's values, then show everything filled and get explicit confirmation before the final submit click.
when_to_use: When the user asks to fill in, complete, or submit a web form (registration, request form, survey, internal tool form) and provides or can provide the values to enter.
version: 1.0.0
---

# Web Form Filler

Fill a web form with the user's values in the visible control browser — and never submit without their explicit go-ahead.

## Inputs

- **Form URL** — the page with the form (required).
- **Values** — the user's field values. Ask for any that are missing; never invent them.
- **Submit intent** — assume the user wants review-then-submit unless they say "fill only, don't submit".

## Steps

1. `browser_open` the form URL. If the control browser is unavailable, stop and tell the user to enable the interactive control browser for this agent (the visible Chrome window — `browser_open`/`browser_type`/`browser_click`); the read-only `browser_fetch` cannot fill forms.
2. `browser_snapshot` the page. Map every form field: label, type (text, dropdown, checkbox, radio, date, file), whether required, and current value.
3. Match the user's values to the fields. List any required field you have no value for and ask before proceeding — do not guess.
4. Fill the form: `browser_click` a field to focus it, `browser_type` the value; use `browser_click` for dropdown options, checkboxes, and radios; `browser_press` (e.g. Tab, Enter) only when a widget needs it.
5. `browser_snapshot` again and read back the actual state of every field.
6. **Confirmation gate — mandatory.** Show the user the complete filled form (Output format below) and ask: "Submit this?" Do NOT click submit, save, send, or confirm until the user explicitly says yes in this conversation.
7. On explicit confirmation, `browser_click` the submit button. Then `browser_snapshot`/`browser_read` the result page and report success, the confirmation number if shown, or any validation errors.
8. If validation errors appear, fix only the flagged fields (repeat steps 4–6, including a fresh confirmation) — never loop-submit.

## Output format

Before submitting:

```
Form ready for review — <URL>
  <Field label>: <value entered>
  <Field label>: <value entered>
  ...
Unfilled/optional: <list or "none">
Reply "submit" to send, or tell me what to change. I have NOT submitted.
```

After submitting: one line with the outcome and any confirmation reference.

## Rules

- **Never submit without explicit user confirmation in this session.** A prior "fill the form" instruction is not submit consent.
- Credentials: type a password/OTP only if the user supplies it in this session, and never store it — no `memory_write` of passwords, tokens, card numbers, or OTPs, and never repeat a password back in the review (show `••••` instead).
- Never invent values for required fields; ask.
- CAPTCHA, 2FA, or "verify it's you" walls: stop and ask the user to complete that step in the visible browser window, then continue.
- Destructive-looking buttons ("Delete", "Pay", "Transfer") get the same explicit-confirmation gate, restated with the amount/consequence.
- If the page navigates away mid-fill, `browser_snapshot`, report what happened, and re-confirm before any further action.
