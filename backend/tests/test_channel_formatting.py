"""Markdown → Telegram HTML and WhatsApp markup conversion."""

from channels.formatting import to_telegram_html, to_whatsapp


def test_bold_italic_strike_heading_bullets():
    md = "# Harga\n\n**Scoopy** dan __NMAX__ ready\n* satu\n- dua\nini *miring* dan _juga_ ~~lama~~"
    out = to_telegram_html(md)
    assert "<b>Harga</b>" in out and "<b>Scoopy</b>" in out and "<b>NMAX</b>" in out
    assert "• satu" in out and "• dua" in out
    assert "<i>miring</i>" in out and "<i>juga</i>" in out and "<s>lama</s>" in out
    assert "**" not in out and "#" not in out


def test_escapes_html_and_leaves_code_and_identifiers_alone():
    out = to_telegram_html("a < b & c > d, snake_case_name, 2*3*4\n`**x** <y>`\n```\nif a<b: **c**\n```")
    assert "a &lt; b &amp; c &gt; d" in out
    assert "snake_case_name" in out and "2*3*4" in out
    assert "<code>**x** &lt;y&gt;</code>" in out
    assert "<pre>if a&lt;b: **c**</pre>" in out


def test_links_and_unmatched_markers():
    out = to_telegram_html("see [booking](https://example.com/a?b=1&c=2) and **unclosed")
    assert '<a href="https://example.com/a?b=1&amp;c=2">booking</a>' in out
    assert "**unclosed" in out
    assert to_telegram_html("") == ""


def test_whatsapp_rewrites_markdown_but_keeps_native_markup():
    md = "## Syarat\n**KTP** dan __SIM__, ~~lama~~\n*sudah bold* _miring_\n- satu\n[booking](https://x.co/b)"
    out = to_whatsapp(md)
    assert out.startswith("*Syarat*")
    assert "*KTP* dan *SIM*, ~lama~" in out
    assert "*sudah bold* _miring_" in out          # native WhatsApp markup untouched
    assert "- satu" in out and "booking (https://x.co/b)" in out
    assert "**" not in out and "#" not in out


def test_whatsapp_leaves_code_alone():
    out = to_whatsapp("run `**x**`\n```\n**y**\n```")
    assert "`**x**`" in out and "```\n**y**\n```" in out
    assert to_whatsapp("") == ""
