"""WhatsApp transports: ``linked`` (QR-paired linked device via a Node bridge,
no public host) and ``cloud`` (the official Cloud API webhook)."""

from channels.whatsapp import cloud, linked  # noqa: F401  (registers adapters)
