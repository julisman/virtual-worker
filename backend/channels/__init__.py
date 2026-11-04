"""Chat channels — the plugin interface that lets people message an agent from
the chat apps they already use (WhatsApp first; Telegram, Discord, Slack later).

A channel adapter turns an external chat transport into one normalized
``InboundMessage`` stream and one ``send()`` call. Everything above the adapter
(policies, dedup, routing to an agent, per-conversation sessions, the run itself,
reply chunking, audit) lives in ``channels.manager`` and is shared by every
transport. See ``channels/base.py`` for the contract and ``docs/PRD.md`` §8.6.
"""
