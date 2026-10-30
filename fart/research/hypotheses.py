"""The pre-registered hypotheses of milestone 12 (B.2), in the order they are tested: H4, H5, H1, H3, H7, H2, H6.

Every hypothesis is a fixed statement, a scope (which coins, which moments), a grid of variants (entry parameters x exit
rules) and one predicate `fires(variant, ctx)` that the backtest harness (fart/research/harness.py) and the plays layer
(fart/plays) share: a promoted rule in config/plays.json names its hypothesis and variant, and the daemon evaluates
exactly this predicate on the live features. The grids are written down here before the first run on real data and are
not tuned to a report: a change is a commit and a new line in docs/DECISIONS.md. Every variant of a grid is counted and
reported, the rejected ones included.

The context a predicate sees (`Ctx`): the replay features of the coin at the tick (the engine's momentum definitions,
`fart/research/features.py::replay_features`), the same features one tick earlier, the bundle flags, the creator's
sells, the creator's history as-of, whether the coin graduated, and for H3 and H6 the theme role and the post-graduation
sample. A predicate never looks beyond `ctx.t`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..measure.paper import ExitRules

# ----- the exit catalogue -------------------------------------------------------------------------------------------------
# Hard stop -35% and the 24 h outer bound on every rule; the names are what the reports and config/plays.json carry.
EXITS: dict[str, ExitRules] = {
    "ladder60_tr30": ExitRules(target_pct=0.60, target_fraction=0.5, trailing_frac=0.30),
    "ladder40_tr30": ExitRules(target_pct=0.40, target_fraction=0.5, trailing_frac=0.30),
    "ladder100_tr30": ExitRules(target_pct=1.00, target_fraction=0.5, trailing_frac=0.30),
    "tr20": ExitRules(trailing_frac=0.20),
    "tr30": ExitRules(trailing_frac=0.30),
    "tr50": ExitRules(trailing_frac=0.50),
    "ts5": ExitRules(trailing_frac=None, time_stop_s=300.0),
    "ts10": ExitRules(trailing_frac=None, time_stop_s=600.0),
    "ts15": ExitRules(trailing_frac=None, time_stop_s=900.0),
    "ts60": ExitRules(trailing_frac=None, time_stop_s=3600.0),
    "grad_tr30": ExitRules(trailing_frac=0.30, sell_at_graduation=True),
    "devsell_tr30": ExitRules(trailing_frac=0.30, sell_at_dev_sell=True),
    "tr25_24h": ExitRules(trailing_frac=0.25, hard_stop_frac=0.35),
}
EXIT_LABELS_NL = {
    "ladder60_tr30": "50% verkopen bij +60%, rest trailing 30%", "ladder40_tr30": "50% verkopen bij +40%, rest trailing 30%",
    "ladder100_tr30": "50% verkopen bij +100%, rest trailing 30%", "tr20": "trailing stop 20%", "tr30": "trailing stop 30%", "tr50": "trailing stop 50%",
    "ts5": "tijdstop 5 min", "ts10": "tijdstop 10 min", "ts15": "tijdstop 15 min", "ts60": "tijdstop 60 min",
    "grad_tr30": "verkopen bij graduatie (trailing 30% tot dan)", "devsell_tr30": "verkopen bij eerste dev-verkoop (trailing 30% tot dan)",
    "tr25_24h": "trailing stop 25% op uurschaal (hoogstens 24 u)",
}


@dataclass
class Ctx:
    """What a predicate may look at, all as-of `t`."""
    mint: str
    created_t: float
    t: float
    f: dict[str, Any]
    f_prev: dict[str, Any] | None
    bundle: dict[str, Any]
    dev: dict[str, Any]
    graduated: bool
    graduation_t: float | None = None
    creator: dict[str, Any] | None = None       # creator_history, lazily computed by the harness when `needs_creator`
    theme: dict[str, Any] | None = None         # H3: {"role": leader_first | leader_strongest | copy, "term", "decision_as_of"}
    sample: dict[str, Any] | None = None        # H6: the post-graduation sample at t: price_sol, volume_h1_usd, liquidity_sol
    grad_price_sol: float | None = None         # H6: the spot at the graduation point
    hour_utc: int = 0

    @property
    def age_s(self) -> float:
        return self.t - self.created_t


@dataclass(frozen=True)
class Variant:
    id: str
    label_nl: str
    entry: dict[str, Any]
    exit: str                      # a key of EXITS
    group: str = ""                # the entry group (for the report's tables)

    @property
    def rules(self) -> ExitRules:
        return EXITS[self.exit]


@dataclass
class Hypothesis:
    id: str
    title_nl: str
    statement_nl: str              # the pre-registered statement, verbatim in every report
    scope_nl: str
    expectation_nl: str            # what the data must show for the hypothesis to hold
    variants: list[Variant]
    fires: Callable[[Variant, Ctx], bool]
    horizon_s: float = 600.0       # how long after the create the rule is evaluated (tick replay)
    first_tick_s: float = 15.0
    needs_creator: bool = False
    needs_theme: bool = False
    post_graduation: bool = False  # H6: the signal lives after the graduation
    notes_nl: list[str] = field(default_factory=list)

    def variant(self, vid: str) -> Variant | None:
        return next((v for v in self.variants if v.id == vid), None)


# ----- helpers ------------------------------------------------------------------------------------------------------------
def _share_ok(f: dict[str, Any], max_share: float) -> bool:
    s = f.get("largest_buyer_share_15m")
    return s is None or float(s) <= max_share


def _dev_ok(bundle: dict[str, Any], max_frac: float) -> bool:
    d = bundle.get("dev_buy_frac_of_curve")
    return d is not None and float(d) <= max_frac


def _not_bundled(bundle: dict[str, Any]) -> bool:
    return bundle.get("bundled_any") is False


def _rising(ctx: Ctx) -> bool:
    return ctx.f_prev is not None and int(ctx.f.get("unique_buyers_5m") or 0) > int(ctx.f_prev.get("unique_buyers_5m") or 0)


def _in_window(ctx: Ctx, lo: float, hi: float) -> bool:
    return lo <= ctx.age_s <= hi


# ----- H1 Organische acceleratie -------------------------------------------------------------------------------------------
H1_N = (15, 25, 40)
H1_M = (3.0, 5.0, 10.0)
H1_EXITS = ("ladder60_tr30", "tr20", "tr30", "tr50", "ts5", "ts15", "ts60", "grad_tr30", "devsell_tr30")


def _h1_fires(v: Variant, ctx: Ctx) -> bool:
    e = v.entry
    if ctx.graduated or not _in_window(ctx, 120.0, 300.0):
        return False
    f = ctx.f
    return (int(f.get("unique_buyers_5m") or 0) >= int(e["min_buyers"]) and float(f.get("net_sol_5m") or 0.0) >= float(e["min_net_sol"])
            and _share_ok(f, 0.15) and _dev_ok(ctx.bundle, 0.02) and _not_bundled(ctx.bundle) and _rising(ctx))


H1 = Hypothesis(
    id="H1", title_nl="Organische acceleratie",
    statement_nl=("Een munt die binnen 2 tot 5 minuten na de create minstens N unieke kopers en minstens M SOL netto instroom in de laatste 5 minuten "
                  "heeft, waarvan de grootste koper hoogstens 15% van de instroom heeft, met een dev-buy van hoogstens 2% van de curve, niet gebundeld, "
                  "en waarvan het aantal unieke kopers tussen de vorige tick en deze tick nog stijgt, geeft na kosten een positieve verwachte waarde "
                  "bij een instap 20 s na het signaal."),
    scope_nl="Elke nieuwe munt met trades (de radar volgt elke munt zijn eerste 5 minuten); het signaal wordt elke 15 s getoetst tussen 2 en 5 minuten na de create; N uit 15/25/40, M uit 3/5/10 SOL.",
    expectation_nl="De ondergrens van het 95%-interval van de verwachte waarde na kosten ligt boven 0 op de toetshelft, met minstens 100 gevallen, minder dan 20% onbekend en een mediane lead time van minstens 2 minuten.",
    variants=[Variant(f"n{n}_m{int(m)}_{ex}", f"≥ {n} kopers, ≥ {int(m)} SOL netto, {EXIT_LABELS_NL[ex]}", {"min_buyers": n, "min_net_sol": m}, ex, group=f"n{n}_m{int(m)}")
              for n in H1_N for m in H1_M for ex in H1_EXITS],
    fires=_h1_fires, horizon_s=300.0, first_tick_s=15.0,
    notes_nl=["Hoofdkandidaat voor een menselijk uitvoerbare play: het signaal valt in de eerste vijf minuten, de instap is één klik naar een terminal."],
)


# ----- H4 Makers met track record ---------------------------------------------------------------------------------------------
H4_GROUPS = (("good", "maker met eerdere graduatie of winnaar, zonder gebundelde of gedumpte munt"),
             ("bad", "maker met eerdere gebundelde of gedumpte munt"),
             ("none", "maker zonder eerdere munt in onze data"),
             ("neutral", "maker met eerdere munten zonder graduatie, winnaar, bundel of dump"))
H4_EXITS = ("ladder60_tr30", "tr30", "ts15")


def _h4_fires(v: Variant, ctx: Ctx) -> bool:
    if ctx.graduated or not _in_window(ctx, 15.0, 300.0) or int(ctx.f.get("trades_total") or 0) < 3:
        return False
    cr = ctx.creator or {}
    return cr.get("creator_track_record") == v.entry["track"]


H4 = Hypothesis(
    id="H4", title_nl="Makers met track record",
    statement_nl=("Munten van een maker-wallet met een eerdere graduatie of winnaar in onze eigen data doen het na kosten beter dan munten van makers "
                  "zonder geschiedenis; munten van makers met eerder gebundelde of binnen 10 minuten gedumpte munten doen het slechter. De instap is de "
                  "eerste tick in de eerste 5 minuten waarop de munt minstens 3 trades heeft."),
    scope_nl="Elke nieuwe munt met minstens 3 trades in zijn eerste 5 minuten, ingedeeld naar de geschiedenis van de maker zoals die op het instapmoment bekend was (eerdere munten, graduaties, winnaars met een vóór het signaal gemeten uitkomst, gebundelde munten, vroege dev-verkopen, tijd sinds de vorige munt).",
    expectation_nl="De groep 'goed' heeft een hogere verwachte waarde dan 'geen geschiedenis', en 'slecht' een lagere; alleen een groep met een bewezen positieve ondergrens kan een play worden.",
    variants=[Variant(f"{g}_{ex}", f"{label}, {EXIT_LABELS_NL[ex]}", {"track": g, "min_trades": 3}, ex, group=g) for g, label in H4_GROUPS for ex in H4_EXITS],
    fires=_h4_fires, horizon_s=300.0, needs_creator=True,
)


# ----- H5 Bundelvermijding --------------------------------------------------------------------------------------------------
H5_FILTERS = (("none", "zonder bundelfilter"), ("excl_dev", "zonder munten met dev-buy > 5% van de curve"), ("excl_slot", "zonder munten met ≥ 3 kopers in de create-slot > 15%"),
              ("excl_top2", "zonder munten waarvan 2 wallets > 40% van de eerste 10 trades kochten"), ("excl_any", "zonder elke vorm van bundel"),
              ("only_bundled", "ALLEEN gebundelde munten (tegenproef)"))
H5_EXITS = ("ladder60_tr30", "tr30", "ts15")
H5_MIN_BUYERS = 10   # the engine's early_buyers promotion threshold (config/engine.json watchlist.early_buyers_threshold)


def _h5_fires(v: Variant, ctx: Ctx) -> bool:
    if ctx.graduated or not _in_window(ctx, 15.0, 300.0) or int(ctx.f.get("unique_buyers_5m") or 0) < int(v.entry["min_buyers"]):
        return False
    b = ctx.bundle
    flt = v.entry["filter"]
    if flt == "none":
        return True
    if flt == "excl_dev":
        return b.get("bundled_dev") is False
    if flt == "excl_slot":
        return b.get("bundled_slot") is False
    if flt == "excl_top2":
        return b.get("bundled_top2") is False
    if flt == "excl_any":
        return b.get("bundled_any") is False
    if flt == "only_bundled":
        return b.get("bundled_any") is True
    return False


H5 = Hypothesis(
    id="H5", title_nl="Bundelvermijding",
    statement_nl=("Elke instapregel verbetert door gebundelde munten uit te sluiten. Gebundeld = dev-buy > 5% van de curve, of minstens 3 kopers in dezelfde "
                  "slot als de create die samen > 15% van de curve kopen, of 2 wallets die > 40% van de aankopen in de eerste 10 trades deden. De basisregel "
                  "is de vroege-kopersregel van de engine: minstens 10 unieke kopers in de eerste 5 minuten. Als tegenproef wordt ook het instappen in "
                  "uitsluitend gebundelde munten gemeten."),
    scope_nl="Elke nieuwe munt die in zijn eerste 5 minuten de vroege-kopersdrempel haalt; per bundeldefinitie het rendement met en zonder uitsluiting.",
    expectation_nl="De varianten met uitsluiting hebben een hogere verwachte waarde dan de basisvariant; de tegenproef (alleen gebundeld) een lagere.",
    variants=[Variant(f"{flt}_{ex}", f"≥ {H5_MIN_BUYERS} kopers, {label}, {EXIT_LABELS_NL[ex]}", {"min_buyers": H5_MIN_BUYERS, "filter": flt}, ex, group=flt)
              for flt, label in H5_FILTERS for ex in H5_EXITS],
    fires=_h5_fires, horizon_s=300.0,
)


# ----- H3 Themaleider -----------------------------------------------------------------------------------------------------
H3_ROLES = (("leader_strongest", "de leider: meeste unieke kopers in zijn eerste 3 minuten"), ("leader_first", "de leider: de eerst gemaakte munt van het thema"),
            ("copy", "een kopie: gemaakt > 10 minuten na de leider (tegenbewijs)"))
H3_EXITS = ("ladder60_tr30", "tr30", "ts15", "ts60")


def _h3_fires(v: Variant, ctx: Ctx) -> bool:
    th = ctx.theme
    if th is None or ctx.graduated:
        return False
    return th.get("role") == v.entry["role"] and int(ctx.f.get("trades_total") or 0) >= 1


H3 = Hypothesis(
    id="H3", title_nl="Themaleider",
    statement_nl=("In een brandend thema (een thema-observatie van de engine: een woord dat opeens in veel nieuwe muntnamen van verschillende makers "
                  "opduikt, met geld erachter) wint de munt die het eerst of het sterkst tractie heeft: de meeste unieke kopers in zijn eerste 3 minuten, "
                  "of de eerst gemaakte. Kopieën die meer dan 10 minuten na de leider gemaakt zijn verliezen. De instap in de leider is op het moment van "
                  "de thema-observatie; de instap in een kopie op haar 2-minuten-tick."),
    scope_nl="Elke thema-observatie (observe_meta-beslissing) in het venster; de munten van de laatste 3 uur met het woord in naam of ticker.",
    expectation_nl="De leider-varianten hebben een positieve bewezen ondergrens; de kopie-variant niet (en liefst een negatieve verwachte waarde).",
    variants=[Variant(f"{role}_{ex}", f"{label}, {EXIT_LABELS_NL[ex]}", {"role": role}, ex, group=role) for role, label in H3_ROLES for ex in H3_EXITS],
    fires=_h3_fires, horizon_s=7200.0, needs_theme=True,
)


# ----- H7 Dev-verkoop als rode vlag ---------------------------------------------------------------------------------------
H7_MODES = (("baseline", "basisregel zonder dev-verkoopfilter"), ("exclude_dev_sold", "zonder munten waarvan de maker al verkocht"),
            ("only_dev_sold", "ALLEEN munten waarvan de maker al verkocht (tegenproef)"), ("exit_on_dev_sell", "basisregel, uitstap bij de eerste dev-verkoop"))
H7_EXITS = ("ladder60_tr30", "tr30")


def _h7_fires(v: Variant, ctx: Ctx) -> bool:
    if ctx.graduated or not _in_window(ctx, 15.0, 300.0) or int(ctx.f.get("unique_buyers_5m") or 0) < int(v.entry["min_buyers"]):
        return False
    sold = ctx.dev.get("dev_first_sell_t") is not None
    mode = v.entry["mode"]
    if mode == "exclude_dev_sold":
        return not sold
    if mode == "only_dev_sold":
        return sold
    return True


def _h7_variants() -> list[Variant]:
    out = []
    for mode, label in H7_MODES:
        for ex in H7_EXITS:
            exit_key = "devsell_tr30" if mode == "exit_on_dev_sell" and ex == "tr30" else ex
            if mode == "exit_on_dev_sell" and ex == "ladder60_tr30":
                continue   # the dev-sell exit is tested on the trailing rule only (one exit variant keeps the grid honest and small)
            out.append(Variant(f"{mode}_{exit_key}", f"≥ {H5_MIN_BUYERS} kopers, {label}, {EXIT_LABELS_NL[exit_key]}", {"min_buyers": H5_MIN_BUYERS, "mode": mode}, exit_key, group=mode))
    return out


H7 = Hypothesis(
    id="H7", title_nl="Dev-verkoop als rode vlag",
    statement_nl=("Een verkoop door de maker-wallet in de eerste 10 minuten voorspelt een slechte uitkomst. Getoetst als uitsluiting (geen instap als de "
                  "maker al verkocht), als tegenproef (alleen instappen als de maker al verkocht) en als uitstapregel (verkopen bij de eerste dev-verkoop) "
                  "op de vroege-kopersregel van de engine."),
    scope_nl="Elke nieuwe munt die in zijn eerste 5 minuten de vroege-kopersdrempel haalt; de dev-verkoop zoals bekend op het instapmoment.",
    expectation_nl="De tegenproef heeft een lagere verwachte waarde dan de basisregel; de uitsluiting en de uitstapregel een hogere.",
    variants=_h7_variants(), fires=_h7_fires, horizon_s=300.0,
)


# ----- H2 Graduatiemoment ----------------------------------------------------------------------------------------------------
H2_M = (5.0, 10.0, 20.0)
H2_EXITS = ("ts5", "ts10", "ts15")
H2_CROSS_SOL = 75.0


def _h2_fires(v: Variant, ctx: Ctx) -> bool:
    if ctx.graduated:
        return False
    cur = ctx.f.get("curve_sol")
    prev = None if ctx.f_prev is None else ctx.f_prev.get("curve_sol")
    if cur is None or prev is None or float(cur) < H2_CROSS_SOL or float(prev) >= H2_CROSS_SOL:
        return False   # fires once, and only when the crossing itself was seen: the previous tick under 75 SOL, this one at or above
    return float(ctx.f.get("net_sol_5m") or 0.0) >= float(v.entry["min_net_sol"])


H2 = Hypothesis(
    id="H2", title_nl="Graduatiemoment",
    statement_nl=("Munten die met sterke instroom (netto minstens M SOL in de laatste 5 minuten) van 75 naar 85 SOL in de curve gaan, laten rond de "
                  "migratie naar PumpSwap een verhandelbare beweging zien met een houdtijd van 5 tot 15 minuten. De instap is de tick waarop de curve "
                  "75 SOL passeert; de uitstap na 5, 10 of 15 minuten, ook als de munt intussen gegradueerd is (minuutcandles of prijssamples)."),
    scope_nl="Elke gevolgde munt die 75 SOL in de curve passeert terwijl de radar haar trades ziet; M uit 5/10/20 SOL.",
    expectation_nl="Een positieve bewezen ondergrens op minstens één houdtijd; zonder minuutcandles (B.5) blijft een deel van de uitkomsten onbekend en zegt het rapport dat.",
    variants=[Variant(f"m{int(m)}_{ex}", f"≥ {int(m)} SOL netto bij 75 SOL, {EXIT_LABELS_NL[ex]}", {"min_net_sol": m}, ex, group=f"m{int(m)}") for m in H2_M for ex in H2_EXITS],
    fires=_h2_fires, horizon_s=86400.0,
    notes_nl=["Een houdtijd over de graduatie heen heeft een prijspad na de migratie nodig: de minuutcandles van B.5, anders de 5-minuten-samples van de tracker (grover)."],
)


# ----- H6 Voortzetting na graduatie ----------------------------------------------------------------------------------------
H6_VOLUME = (10_000.0, 25_000.0, 50_000.0)
H6_EXITS = ("tr25_24h", "ts60")
H6_AFTER_S = 1800.0


def _h6_fires(v: Variant, ctx: Ctx) -> bool:
    s = ctx.sample
    if s is None or ctx.grad_price_sol is None or not s.get("price_sol"):
        return False
    vol = s.get("volume_h1_usd")
    return float(s["price_sol"]) >= float(ctx.grad_price_sol) and vol is not None and float(vol) >= float(v.entry["min_volume_usd"])


H6 = Hypothesis(
    id="H6", title_nl="Voortzetting na graduatie",
    statement_nl=("Gegradueerde munten die 30 minuten na de graduatie boven de graduatieprijs staan met een uurvolume van minstens Y dollar zetten vaker "
                  "door. De instap is op de prijssample van 30 minuten na de graduatie, de uitstap op uurschaal met een trailing stop van 25% (hoogstens 24 u) "
                  "of na 60 minuten."),
    scope_nl="Elke graduatie in het venster met een prijssample rond 30 minuten na de graduatie; Y uit $10.000 / $25.000 / $50.000.",
    expectation_nl="Een positieve bewezen ondergrens; de kosten zijn de PumpSwap-fee van 0,25% per kant en de impact uit de DexScreener-liquiditeit.",
    variants=[Variant(f"v{int(y / 1000)}k_{ex}", f"boven de graduatieprijs, uurvolume ≥ ${int(y):,}".replace(",", ".") + f", {EXIT_LABELS_NL[ex]}", {"min_volume_usd": y}, ex, group=f"v{int(y / 1000)}k")
              for y in H6_VOLUME for ex in H6_EXITS],
    fires=_h6_fires, horizon_s=86400.0, post_graduation=True,
)


HYPOTHESES: dict[str, Hypothesis] = {h.id: h for h in (H4, H5, H1, H3, H7, H2, H6)}
ORDER = ["H4", "H5", "H1", "H3", "H7", "H2", "H6"]


def get(hid: str) -> Hypothesis:
    key = str(hid).upper()
    if key not in HYPOTHESES:
        raise KeyError(f"unknown hypothesis {hid!r}; known: {', '.join(ORDER)}")
    return HYPOTHESES[key]


def catalogue() -> list[dict[str, Any]]:
    """The hypotheses and their grids as plain data (for `fart research list` and the reports)."""
    return [{"id": h.id, "title": h.title_nl, "statement": h.statement_nl, "scope": h.scope_nl, "expectation": h.expectation_nl,
             "variants": len(h.variants), "groups": sorted({v.group for v in h.variants}), "exits": sorted({v.exit for v in h.variants}),
             "notes": h.notes_nl} for h in (HYPOTHESES[k] for k in ORDER)]
