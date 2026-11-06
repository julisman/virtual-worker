---
name: Channel Broadcast
description: Compose and send a message to one confirmed chat target (WhatsApp, Telegram, …) safely — target resolved from memory or the user, draft shown, explicit confirmation before sending.
when_to_use: When the user asks to send, broadcast, announce, or forward something to a person or group on a connected chat channel.
version: 1.0.0
---

# Channel Broadcast

Send one message to one confirmed chat target through a connected channel, with
the destination resolved first and the draft confirmed before anything leaves the
machine.

## Steps

1. Check the channel tools are available. If `channel_send` is absent, stop and tell
   the user this agent is not allowed to message people on channels (an owner turns
   it on under the agent's Guardrails → "Message people on channels").
2. Resolve the target — never guess an id:
   a. `memory_search` for a saved mapping, e.g. `"whatsapp group <name>"` or
      `"telegram chat <name>"`.
   b. If nothing is saved, ask the user for the exact phone number (with country
      code, digits only) or the group / chat id, and which connected account to use.
3. Draft the message. Keep it short, plain text, no hidden reasoning, no tables.
4. Show the draft and the exact target to the user and ask for a yes.
5. On confirmation, call `channel_send` with the text, the `to` target, and the
   `account` id when more than one account is connected. Report success or the
   error verbatim.
6. If the send succeeded and the target came from the user (not from memory),
   offer to `memory_write` the mapping so next time it resolves automatically.

## Output format

```
Ready to send on <channel> — NOT sent yet
To: <name> (<id>)
---
<draft>
---
Send it? (yes / edit / cancel)
```

## Rules

- **Never guess or fabricate an id.** A wrong number means a message to a stranger.
- One message, one target. For several recipients, confirm each separately.
- Message content goes only to the confirmed target — no other egress.
- If `channel_send` errors (account not connected, number not allowed), report it
  verbatim and stop; do not retry with a different target.
