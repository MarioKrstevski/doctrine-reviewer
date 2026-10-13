"""Render a note's fields roughly the way Anki shows them. Pure, no HTTP.

Student snapshots and accepted proposals are untrusted HTML, so every render
goes through an allow-list sanitizer first. Media names become /media/<name>.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import quote

ALLOWED_TAGS = {"b", "i", "u", "em", "strong", "br", "p", "div", "span", "ul", "ol", "li", "img", "sup", "sub",
                "table", "thead", "tbody", "tr", "td", "th", "a", "code", "pre", "hr", "font", "mark", "small",
                "h1", "h2", "h3", "h4", "blockquote"}
VOID = {"br", "img", "hr"}
ALLOWED_ATTRS = {"img": {"src", "alt", "width", "height"}, "a": {"href"}, "td": {"colspan", "rowspan"},
                 "th": {"colspan", "rowspan"}, "font": {"color"}}
CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        keep = []
        for name, value in attrs:
            if tag == "span" and name == "class" and value in ("cloze-hide", "cloze-show"):
                keep.append(f' class="{value}"')
                continue
            if name not in ALLOWED_ATTRS.get(tag, ()):
                continue
            value = value or ""
            if name == "src" and not re.match(r"^(https?:|/|data:image/)", value, re.I):
                value = "/media/" + quote(value, safe="")
            if name in ("href", "src") and re.match(r"^\s*(javascript|vbscript):", value, re.I):
                continue
            keep.append(f' {name}="{html.escape(value, quote=True)}"')
        if tag == "img":
            keep.append(' loading="lazy"')
        self.out.append(f"<{tag}{''.join(keep)}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ALLOWED_TAGS and tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")


def sanitize(markup):
    p = _Clean()
    p.feed(markup or "")
    p.close()
    return "".join(p.out)


def cloze_numbers(text):
    return sorted({int(m.group(1)) for m in CLOZE_RE.finditer(text or "")})


def render_cloze(text, active=1, reveal=False):
    """Hide or reveal cloze number `active`. Other clozes show their answer."""
    def sub(m):
        n, ans, hint = int(m.group(1)), m.group(2), m.group(3)
        if n != active:
            return ans
        if reveal:
            return f'<span class="cloze-show">{ans}</span>'
        return f'<span class="cloze-hide">[{hint or "..."}]</span>'
    return CLOZE_RE.sub(sub, text or "")


def faces(fields, active=1):
    """{'front', 'back', 'extra'} sanitized HTML for one card of the note."""
    fields = fields or {}
    if "Text" in fields:
        nums = cloze_numbers(fields["Text"])
        if nums and active not in nums:
            active = nums[0]
        return {"front": sanitize(render_cloze(fields["Text"], active, False)),
                "back": sanitize(render_cloze(fields["Text"], active, True)),
                "extra": sanitize(fields.get("Extra", ""))}
    if "Front" in fields:
        return {"front": sanitize(fields["Front"]), "back": sanitize(fields.get("Back", "")), "extra": ""}
    values = [v for v in fields.values() if isinstance(v, str) and v.strip()]
    return {"front": sanitize(values[0] if values else ""), "back": sanitize("<br>".join(values[1:])), "extra": ""}
