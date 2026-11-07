"""Import every built-in channel adapter so it registers itself. Imported once
from ``app.py`` (startup) and ``routes/channels.py`` (catalog)."""

from channels import discord  # noqa: F401
from channels import slack  # noqa: F401
from channels import telegram  # noqa: F401
from channels import whatsapp  # noqa: F401
