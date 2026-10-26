# fart

## Voor Jim: wat dit is (milestone 1)

`fart` is een datamachine voor Solana / pump.fun memecoins. Milestone 1 doet drie dingen, en
verder niets:

1. **Compleet en dubbel inlezen** van elke pump.fun-create en -graduatie: PumpPortal (gratis, één
   verbinding) én de Solana-RPC (logstroom op de mint-authority en het migratie-account, plus elke
   5 minuten een goedkope controle van alle handtekeningen op de chain). Wat de ene bron mist, vult
   de andere aan; wat allebei missen, staat als gat in het dekkingsrapport.
2. **Een datamodel waarin terugkijken onmogelijk is**: elke rij heeft chain-tijd én
   ontvangsttijd; elk kenmerk wordt berekend "zoals bekend op moment T" en kan nooit meer
   veranderen door latere data. Beslissingen en uitkomsten zijn alleen-toevoegen en dragen de
   git-commit van de regels (`rules_version`).
3. **Eerlijke meting**: voor elke graduatie (ons hele universum, niet alleen wat wij aanwezen) en
   elke observatie meten we na 1, 6 en 24 uur piek-marktkapitalisatie, volume, winnaar (≥ $100k
   piek én ≥ $20k volume), gebundeld of organisch. Onbekend blijft onbekend. Elke ochtend 06:00
   UTC komt een dagrapport in `out/recall/` (Nederlands) met dekking, winnaars, lead time,
   precisie en paper trades. Lees `docs/METING.md` voor de definities.

Wat het **niet** doet: geen alerts, geen Telegram, geen X, geen handel, geen keys, geen
machine learning, geen webinterface. De "candidate engine v0" schrijft alleen observaties in de
database zodat de meting iets heeft om te meten.

Alles wat extern is en niet gecontroleerd kon worden (byte-layouts van pump.fun-events, exacte
API-velden, kosten per RPC-call, curve-parameters) staat in `docs/UNVERIFIED.md` met de plek waar
je het aanpast. De eerste dag op de VPS is daarvoor (zie `deploy/DEPLOY.md`, "First-day
checklist").

Dagelijks kijken: `python -m fart status` (gezondheid, budgetten, tellers),
`python -m fart coverage --hours 24` (dekking per uur) en `out/recall/<dag>.md`.

## Quick start (developer)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # or: pip install websockets pytest
make check                       # py_compile + pytest + systemd-analyze verify (when available)
python -m fart status            # works on an empty database
python -m fart ingest            # daemon; PumpPortal only without an RPC key
python -m fart engine            # daemon; 15 s loop
python -m fart measure track     # one tracker run
python -m fart measure report --day 2026-10-25
make live                        # opt-in 60 s PumpPortal smoke test (FART_LIVE=1), skips offline
```

Configuration: `config/*.json` (thresholds, cost tables, curve parameters) and
`$FART_ROOT/.env` (names in `.env.example`; keys only on the server, never in the repo).

Layout:

```
fart/          package: config, db, log, util, version, cli
fart/net/      budget gate (rate, daily caps, health, backoff) + urllib JSON client
fart/chain/    base58, Anchor event decoder, tx parsing, bonding curve math, RPC client
fart/ingest/   raw store, normalization, PumpPortal + RPC log clients, sweep, coverage, retention
fart/engine/   meta clustering, as-of features, decisions, RPC enrichment, daemon
fart/measure/  prices, outcomes, paper trades, daily report
config/        pumpfun, budget, ingest, engine, measure, stopwords
deploy/        systemd units, slice, timers, install.sh, backup.sh, DEPLOY.md
docs/          ARCHITECTURE (EN), METING (NL), PUMPFUN-EVENTS, UNVERIFIED, DECISIONS
tests/         pytest suite with synthetic fixtures; opt-in live smoke test
```

See `docs/ARCHITECTURE.md` for the design and `deploy/DEPLOY.md` for the VPS procedure.
