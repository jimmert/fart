"""A safe Markdown subset for the reports under out/recall/ (milestone 8).

Renders exactly what the daily and week reports use: ATX headings (`#` to `######`), paragraphs, bullet lists (`- ` or
`* `), numbered lists (`1. `), pipe tables with a separator row (`|---|---:|`: a trailing colon right-aligns the column),
`**bold**` and `` `code` `` inline. Everything is HTML-escaped first; raw HTML, links, images and any external
reference are shown as text, never as markup, so a coin name that contains `<script>` stays a coin name.
"""
from __future__ import annotations

import html
import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    """Escape, then bold and code. Code spans are cut out first so `**` inside them stays literal."""
    out: list[str] = []
    pos = 0
    for m in _CODE.finditer(text):
        out.append(_bold(html.escape(text[pos:m.start()], quote=True)))
        out.append(f"<code>{html.escape(m.group(1), quote=True)}</code>")
        pos = m.end()
    out.append(_bold(html.escape(text[pos:], quote=True)))
    return "".join(out)


def _bold(escaped: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", escaped)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _aligns(sep: str) -> list[str]:
    out = []
    for c in _cells(sep):
        c = c.strip()
        out.append("num" if c.endswith(":") and not c.startswith(":") else ("center" if c.startswith(":") and c.endswith(":") else ""))
    return out


def render(text: str) -> str:
    """The HTML body (no document skeleton) for a report's Markdown."""
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    i = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(s.strip() for s in para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
            i += 1
            continue
        m = _HEADING.match(stripped)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1].strip()) and lines[i + 1].strip().startswith("|"):
            flush_para()
            close_list()
            aligns = _aligns(lines[i + 1])
            head = _cells(stripped)
            rows_html = ["<table><thead><tr>" + "".join(f"<th{_cls(aligns, j)}>{inline(c)}</th>" for j, c in enumerate(head)) + "</tr></thead><tbody>"]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _cells(lines[i].strip())
                rows_html.append("<tr>" + "".join(f"<td{_cls(aligns, j)}>{inline(c)}</td>" for j, c in enumerate(cells)) + "</tr>")
                i += 1
            rows_html.append("</tbody></table>")
            out.append("".join(rows_html))
            continue
        m = _BULLET.match(line) or _NUMBERED.match(line)
        if m:
            flush_para()
            tag = "ul" if _BULLET.match(line) else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue
        if stripped in ("---", "***", "___"):
            flush_para()
            close_list()
            out.append("<hr>")
            i += 1
            continue
        close_list()
        para.append(line)
        i += 1
    flush_para()
    close_list()
    return "\n".join(out)


def _cls(aligns: list[str], j: int) -> str:
    a = aligns[j] if j < len(aligns) else ""
    return f' class="{a}"' if a else ""
