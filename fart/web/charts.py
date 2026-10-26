"""Inline SVG charts for the website (milestone 9), rendered on the server without any library.

Every chart is a `<figure>` with an inline `<svg>` whose colours come from CSS classes in `static/app.css` (no inline
style, so the strict Content-Security-Policy of the site holds) and a visually hidden text summary for screen readers
and for anyone whose browser does not draw SVG. The honesty rules of the site apply to the drawing too: an hour or a day
NIET GEDEKT is hatched and has no bar height (we were not there to count: a hatched cell is not a zero), a value that
does not exist leaves a gap in a line (never a zero point), and a confidence interval is drawn as a band only once the
week report has one ("te weinig data" draws no band and says so in the summary).

All numbers in the text summaries are Dutch (`fmt_n`: 12.345 and 12,5).
"""
from __future__ import annotations

import html
from typing import Any

from ..notify.format import n as fmt_n

W = 640            # the drawing width of every chart; the SVG scales to its container through the viewBox
PAD_L = 36
PAD_R = 8
PAD_T = 8
PAD_B = 22


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _f(v: float) -> str:
    """A compact coordinate string."""
    return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def figure(svg: str, summary: str, caption: str | None = None, cls: str = "") -> str:
    cap = f'<figcaption class="chart-caption">{esc(caption)}</figcaption>' if caption else ""
    c = f" {esc(cls)}" if cls else ""
    return f'<figure class="chart{c}">{svg}<p class="sr-only">{esc(summary)}</p>{cap}</figure>'


def _svg(height: int, body: str, title: str, desc: str, cls: str = "") -> str:
    c = f' class="chart-svg {esc(cls)}"' if cls else ' class="chart-svg"'
    return (f'<svg{c} viewBox="0 0 {W} {height}" role="img" preserveAspectRatio="none" focusable="false">'
            f"<title>{esc(title)}</title><desc>{esc(desc)}</desc>{HATCH}{body}</svg>")


# the hatch pattern for NIET GEDEKT cells (defined in every SVG so a chart stands alone when copied)
HATCH = ('<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">'
         '<rect width="6" height="6" class="ch-hatch-bg"/><line x1="0" y1="0" x2="0" y2="6" class="ch-hatch-line"/></pattern></defs>')


# ----- coverage strip: 24 hours ------------------------------------------------------------------------------------
def coverage_strip(rows: list[dict[str, Any]]) -> str:
    """One cell per hour: complete (solid), covered but not complete (half tone), NIET GEDEKT (hatched)."""
    n = len(rows)
    height = 48
    if n == 0:
        return figure(_svg(height, f'<text x="{W / 2}" y="{height / 2 + 4}" class="ch-empty" text-anchor="middle">nog geen gegevens</text>',
                           "Dekking", "Er zijn nog geen gegevens over de dekking."), "Dekking: er zijn nog geen gegevens.")
    gap = 2
    cell_w = (W - gap * (n - 1)) / n
    parts = []
    for i, r in enumerate(rows):
        x = i * (cell_w + gap)
        st = r.get("status") or ("complete" if r.get("complete") else ("covered" if r.get("covered") else "not_covered"))
        label = r.get("clock") or r.get("hour", "")[-2:]
        if st == "not_covered":
            parts.append(f'<rect x="{_f(x)}" y="0" width="{_f(cell_w)}" height="28" class="ch-cell ch-nc" fill="url(#hatch)"><title>{esc(label)}: NIET GEDEKT</title></rect>')
        else:
            parts.append(f'<rect x="{_f(x)}" y="0" width="{_f(cell_w)}" height="28" class="ch-cell ch-{esc(st)}"><title>{esc(label)}: {"compleet" if st == "complete" else "gedekt, niet compleet"}</title></rect>')
        if i % 6 == 0 or i == n - 1:
            parts.append(f'<text x="{_f(x)}" y="44" class="ch-tick">{esc(label)}</text>')
    nc = sum(1 for r in rows if (r.get("status") == "not_covered") or (not r.get("covered") and r.get("status") is None))
    comp = sum(1 for r in rows if r.get("status") == "complete" or r.get("complete"))
    summary = (f"Dekking van de laatste {n} uur: {fmt_n(comp)} uur compleet, {fmt_n(n - nc - comp)} uur gedekt maar niet compleet, "
               f"{fmt_n(nc)} uur NIET GEDEKT (gearceerd: geen telling, geen nul).")
    return figure(_svg(height, "".join(parts), "Dekking per uur", summary, "chart-coverage"), summary)


# ----- bars per hour or per day -------------------------------------------------------------------------------------
def bars(items: list[dict[str, Any]], value_key: str, label_key: str, title: str, unit_word: str, height: int = 160, tick_every: int = 6,
         empty_text: str = "nog geen gegevens") -> str:
    """Vertical bars; an item with `covered` False (or value None) is hatched at full height: NIET GEDEKT, not zero."""
    n = len(items)
    if n == 0:
        return figure(_svg(80, f'<text x="{W / 2}" y="44" class="ch-empty" text-anchor="middle">{esc(empty_text)}</text>', title, empty_text), f"{title}: {empty_text}.")
    values = [it.get(value_key) for it in items]
    known = [float(v) for v, it in zip(values, items) if v is not None and it.get("covered", True)]
    vmax = max(known) if known else 0.0
    top, div = _nice_top(vmax)
    plot_h = height - PAD_T - PAD_B
    gap = 3 if n > 12 else 6
    bar_w = (W - PAD_L - PAD_R - gap * (n - 1)) / n
    parts = []
    # grid: whole-number ticks
    for k in range(0, div + 1):
        y = PAD_T + plot_h - plot_h * k / div
        val = top * k / div
        parts.append(f'<line x1="{PAD_L}" y1="{_f(y)}" x2="{W - PAD_R}" y2="{_f(y)}" class="ch-grid"/>')
        parts.append(f'<text x="{PAD_L - 4}" y="{_f(y + 3)}" class="ch-tick" text-anchor="end">{esc(_short_num(val))}</text>')
    nc = 0
    for i, (it, v) in enumerate(zip(items, values)):
        x = PAD_L + i * (bar_w + gap)
        label = str(it.get(label_key) or "")
        if v is None or not it.get("covered", True):
            nc += 1
            parts.append(f'<rect x="{_f(x)}" y="{PAD_T}" width="{_f(bar_w)}" height="{_f(plot_h)}" class="ch-bar ch-nc" fill="url(#hatch)">'
                         f"<title>{esc(label)}: NIET GEDEKT</title></rect>")
        else:
            h = (float(v) / top * plot_h) if top else 0.0
            h = max(h, 1.5) if float(v) > 0 else 0.0
            parts.append(f'<rect x="{_f(x)}" y="{_f(PAD_T + plot_h - h)}" width="{_f(bar_w)}" height="{_f(h)}" class="ch-bar" rx="1">'
                         f"<title>{esc(label)}: {esc(fmt_n(v))} {esc(unit_word)}</title></rect>")
        if i % tick_every == 0 or i == n - 1:
            parts.append(f'<text x="{_f(x + bar_w / 2)}" y="{height - 6}" class="ch-tick" text-anchor="middle">{esc(label)}</text>')
    total = sum(float(v) for v, it in zip(values, items) if v is not None and it.get("covered", True))
    summary = (f"{title}: {fmt_n(round(total))} {unit_word} in {fmt_n(n - nc)} gedekte {'periode' if n - nc == 1 else 'periodes'}"
               + (f"; {fmt_n(nc)} NIET GEDEKT (gearceerd, geen telling)" if nc else "") + (f"; hoogste waarde {fmt_n(round(vmax))}." if known else "."))
    return figure(_svg(height, "".join(parts), title, summary, "chart-bars"), summary)


NICE = ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 3), (8, 4), (10, 5), (12, 4), (15, 3), (20, 4), (25, 5), (30, 3), (40, 4), (50, 5), (60, 3), (80, 4), (100, 5))


def _nice_top(vmax: float) -> tuple[float, int]:
    """(top of the axis, number of divisions) such that every tick is a whole number."""
    if vmax <= 0:
        return 1.0, 1
    scale = 1
    while vmax > 100 * scale:
        scale *= 10
    for top, div in NICE:
        if vmax <= top * scale:
            return float(top * scale), div
    return float(100 * scale), 5


def _short_num(v: float) -> str:
    if v >= 1000:
        return fmt_n(round(v))
    if v == int(v):
        return str(int(v))
    return fmt_n(v, 1)


# ----- rates per day with the bootstrap band -----------------------------------------------------------------------
def rate_lines(days: list[dict[str, Any]], hit_ci: dict[str, Any] | None, recall_ci: dict[str, Any] | None, height: int = 200) -> str:
    """Hit rate and recall per day as two lines with points; a day without a value leaves a gap; the window's bootstrap
    interval of each rate is a horizontal band once it exists."""
    n = len(days)
    title = "Trefkans en recall per dag"
    if n == 0:
        return figure(_svg(80, f'<text x="{W / 2}" y="44" class="ch-empty" text-anchor="middle">nog geen weekrapport</text>', title, "nog geen weekrapport"),
                      f"{title}: nog geen weekrapport.")
    plot_h = height - PAD_T - PAD_B
    inner_w = W - PAD_L - PAD_R
    step = inner_w / max(1, n)
    parts = []
    for k in range(0, 5):
        y = PAD_T + plot_h - plot_h * k / 4
        parts.append(f'<line x1="{PAD_L}" y1="{_f(y)}" x2="{W - PAD_R}" y2="{_f(y)}" class="ch-grid"/>')
        parts.append(f'<text x="{PAD_L - 4}" y="{_f(y + 3)}" class="ch-tick" text-anchor="end">{k * 25}%</text>')

    def y_of(rate: float) -> float:
        return PAD_T + plot_h - plot_h * max(0.0, min(1.0, rate))

    bands = []
    for ci, cls, name in ((hit_ci, "ch-band-hit", "trefkans"), (recall_ci, "ch-band-recall", "recall")):
        if ci and ci.get("status") == "ok" and ci.get("low") is not None and ci.get("high") is not None:
            y1, y2 = y_of(float(ci["high"])), y_of(float(ci["low"]))
            bands.append(f'<rect x="{PAD_L}" y="{_f(y1)}" width="{_f(inner_w)}" height="{_f(max(1.0, y2 - y1))}" class="ch-band {cls}">'
                         f"<title>bootstrapinterval {esc(name)}: {esc(ci.get('low_label'))} tot {esc(ci.get('high_label'))}</title></rect>")
    parts += bands
    series_summary = []
    for key, cls, name in (("hit_rate", "ch-line-hit", "trefkans"), ("recall", "ch-line-recall", "recall")):
        pts = []
        segs: list[list[tuple[float, float]]] = [[]]
        for i, d in enumerate(days):
            v = d.get(key)
            x = PAD_L + step * (i + 0.5)
            if v is None or not d.get("covered", True):
                if segs[-1]:
                    segs.append([])
                continue
            segs[-1].append((x, y_of(float(v))))
            pts.append(f'<circle cx="{_f(x)}" cy="{_f(y_of(float(v)))}" r="4" class="ch-point {cls}"><title>{esc(d.get("day_label"))}: {esc(name)} {esc(fmt_n(round(100 * float(v))))}%</title></circle>')
        for seg in segs:
            if len(seg) >= 2:
                path = "M" + " L".join(f"{_f(x)} {_f(y)}" for x, y in seg)
                parts.append(f'<path d="{path}" class="ch-line {cls}"/>')
        parts += pts
        vals = [d.get(key) for d in days if d.get(key) is not None and d.get("covered", True)]
        series_summary.append(f"{name} op {fmt_n(len(vals))} van {fmt_n(n)} dagen bekend" + (f" (laagste {fmt_n(round(100 * min(vals)))}%, hoogste {fmt_n(round(100 * max(vals)))}%)" if vals else ""))
    for i, d in enumerate(days):
        x = PAD_L + step * (i + 0.5)
        if not d.get("covered", True):
            parts.append(f'<rect x="{_f(x - step / 2 + 1)}" y="{PAD_T}" width="{_f(step - 2)}" height="{_f(plot_h)}" class="ch-nc" fill="url(#hatch)"><title>{esc(d.get("day_label"))}: NIET GEDEKT</title></rect>')
        parts.append(f'<text x="{_f(x)}" y="{height - 6}" class="ch-tick" text-anchor="middle">{esc(_day_tick(d))}</text>')
    nc = sum(1 for d in days if not d.get("covered", True))
    band_text = []
    for ci, name in ((hit_ci, "trefkans"), (recall_ci, "recall")):
        if ci and ci.get("status") == "ok":
            band_text.append(f"band {name} {ci.get('low_label')} tot {ci.get('high_label')}")
        else:
            band_text.append(f"geen band voor {name}: te weinig data")
    summary = f"{title}: " + "; ".join(series_summary) + (f"; {fmt_n(nc)} dagen NIET GEDEKT (gearceerd)" if nc else "") + "; " + "; ".join(band_text) + "."
    legend = ('<p class="chart-legend"><span class="legend-item"><span class="swatch swatch-hit"></span>trefkans</span>'
              '<span class="legend-item"><span class="swatch swatch-recall"></span>recall vóór graduatie</span>'
              '<span class="legend-item"><span class="swatch swatch-band"></span>bootstrapinterval (venster)</span>'
              '<span class="legend-item"><span class="swatch swatch-nc"></span>NIET GEDEKT</span></p>')
    return figure(_svg(height, "".join(parts), title, summary, "chart-rates") + legend, summary)


def _day_tick(d: dict[str, Any]) -> str:
    lab = str(d.get("day_label") or d.get("day") or "")
    parts = lab.split(" ")
    return " ".join(parts[:2]) if len(parts) >= 2 else lab
