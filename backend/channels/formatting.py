"""Model Markdown → a chat platform's own markup.

Models answer in CommonMark-ish Markdown (``**bold**``, ``# heading``, ``- item``,
``[text](url)``, backticks). Chat apps either ignore it (the asterisks show up
literally) or speak a different dialect. Converters here are pure functions,
best-effort, and never raise: anything they do not recognise passes through as
literal text.
"""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_HR_RE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", re.M)

# Private-use placeholders: cannot collide with model text or HTML escaping.
_PH = "{}"
_PH_RE = re.compile("(\\d+)")


def to_telegram_html(text: str) -> str:
    """Convert Markdown to the HTML subset Telegram's ``parse_mode=HTML`` accepts
    (b, i, s, code, pre, a). Everything else is HTML-escaped, so the result is
    always well-formed: tags are only ever emitted in matched pairs."""
    if not text:
        return ""
    saved: list[str] = []

    def keep(fragment: str) -> str:
        saved.append(fragment)
        return _PH.format(len(saved) - 1)

    # Code first: its contents are literal and must not be formatted.
    out = _FENCE_RE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip(), quote=False)}</pre>"), text)
    out = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), out)
    out = _LINK_RE.sub(lambda m: keep(
        f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1), quote=False)}</a>'), out)

    out = html.escape(out, quote=False)
    out = _HR_RE.sub("", out)
    out = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    out = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", out)     # before italics: "* item" is a bullet
    out = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", out)
    out = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", out)
    out = _ITALIC_UNDER_RE.sub(lambda m: f"<i>{m.group(1)}</i>", out)

    out = _PH_RE.sub(lambda m: saved[int(m.group(1))], out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_WA_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def to_whatsapp(text: str) -> str:
    """Convert Markdown to WhatsApp's own markup (``*bold*``, ``_italic_``, ``~strike~``,
    code fences and ``- `` lists are native).

    Only the unambiguous Markdown is rewritten: ``**x**`` / ``__x__`` → ``*x*``,
    ``~~x~~`` → ``~x~``, ``# heading`` → ``*heading*``, ``[text](url)`` → ``text (url)``.
    A single ``*x*`` or ``_x_`` is left alone: it already means bold / italic on
    WhatsApp, and agents written for WhatsApp use it on purpose."""
    if not text:
        return ""
    saved: list[str] = []

    def keep(fragment: str) -> str:
        saved.append(fragment)
        return _PH.format(len(saved) - 1)

    out = re.sub(r"```.*?```", lambda m: keep(m.group(0)), text, flags=re.S)
    out = _INLINE_CODE_RE.sub(lambda m: keep(m.group(0)), out)
    out = _LINK_RE.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2)
                       else f"{m.group(1)} ({m.group(2)})", out)
    out = _HR_RE.sub("", out)
    out = _HEADING_RE.sub(lambda m: f"*{m.group(1).strip('*_ ')}*", out)
    out = _BOLD_RE.sub(lambda m: f"*{m.group(1) or m.group(2)}*", out)
    out = _WA_STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", out)
    out = _PH_RE.sub(lambda m: saved[int(m.group(1))], out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
