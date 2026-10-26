"""The accent colour of the design system (milestone 9): from `config/web.json` into the stylesheet without inline styles.

`static/app.css` carries the default accent in its `:root` tokens. When `config/web.json` names another `accent`
(`#rrggbb`), the server appends one small `:root { ... }` block to the stylesheet it serves (`accent_override`), with the
accent itself, its RGB triple for the translucent tints, and a darkened twin for text on a light background (so the
light theme keeps its contrast whatever colour Jim picks). A value that is not a colour is ignored: the default stays.
Everything here is pure arithmetic on the standard library.
"""
from __future__ import annotations

import colorsys
import re

HEX = re.compile(r"^#([0-9a-fA-F]{6})$")
DEFAULT_ACCENT = "#F2B33D"
DARK_LIGHTNESS = 0.27     # the lightness of the darkened twin (HLS), enough for 4.5:1 on an off-white background


def parse_hex(value: str | None) -> tuple[int, int, int] | None:
    m = HEX.match((value or "").strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def darken(rgb: tuple[int, int, int], lightness: float = DARK_LIGHTNESS) -> tuple[int, int, int]:
    """The same hue and saturation at a fixed, low lightness (HLS)."""
    h, l, s = colorsys.rgb_to_hls(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    r, g, b = colorsys.hls_to_rgb(h, min(l, lightness), s)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def accent_override(value: str | None) -> str:
    """The CSS block to append to the stylesheet; empty for the default or an invalid value."""
    rgb = parse_hex(value)
    if rgb is None or to_hex(rgb) == DEFAULT_ACCENT:
        return ""
    dark = darken(rgb)
    # text on the accent background: dark text unless the accent itself is dark
    contrast_text = "#1A1200" if relative_luminance(rgb) > 0.35 else "#FFFFFF"
    return (f"\n/* accent from config/web.json */\n:root {{ --accent: {to_hex(rgb)}; --accent-dark: {to_hex(dark)}; "
            f"--accent-rgb: {rgb[0]} {rgb[1]} {rgb[2]}; --accent-contrast: {contrast_text}; }}\n")
