"""``channel_send`` — let an agent message someone on a connected chat channel.

Opt-in per agent (``guardrails.channelSend``). Inside a channel turn the tool
defaults to replying in the current conversation; a proactive send to another
number/group requires an explicit ``to``. The self-send guard and the delivery
audit live in ``channels.manager.send``.
"""

from __future__ import annotations

from channels.manager import CURRENT_INBOUND, manager
from storage import chat_channel_store


def make_channel_tools(agent_id: str) -> dict[str, dict]:
    async def channel_send(text: str = "", to: str = "", account: str = "") -> str:
        text = (text or "").strip()
        if not text:
            return "channel_send needs 'text'."
        cur = CURRENT_INBOUND.get()
        account_id = (account or "").strip() or (cur.account_id if cur else "")
        target = (to or "").strip() or (cur.conversation_id if cur else "")
        if not account_id:
            accts = [a for a in chat_channel_store.list_accounts() if a["enabled"]]
            if len(accts) == 1:
                account_id = accts[0]["id"]
            else:
                return ("channel_send needs 'account' (an enabled chat account id); none or "
                        "several are configured: " + ", ".join(f"{a['id']} ({a['kind']}: {a['name']})" for a in accts))
        if not target:
            return "channel_send needs 'to' (a phone number or chat id) when not replying in a conversation."
        rec = await manager.send(account_id, target, text, agent_id=agent_id, source="tool")
        if rec["status"] == "sent":
            return f"sent to {target} via {rec['channel']} (delivery id={rec['id']})"
        return f"send {rec['status']}: {rec.get('error')}"

    return {
        "channel_send": {
            "fn": channel_send,
            "schema": {
                "type": "function",
                "function": {
                    "name": "channel_send",
                    "description": (
                        "Send a chat message on a connected channel (e.g. WhatsApp). When you are "
                        "already replying inside a chat, omit 'to' to send an extra message there; "
                        "give an explicit 'to' (phone number or group id) to message someone else. "
                        "Every send is audited."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "the message"},
                            "to": {"type": "string", "description": "phone number or chat id (optional inside a chat)"},
                            "account": {"type": "string", "description": "chat account id (optional when only one is configured)"},
                        },
                        "required": ["text"],
                    },
                },
            },
        },
    }
