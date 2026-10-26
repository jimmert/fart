# fart

## Voor Jim: wat milestone 2 toevoegt

1. **Trades per munt via de RPC.** PumpPortal geeft alleen trades aan een betaalde API-sleutel; zonder
   sleutel stuurt fart die abonnementen niet meer en haalt het de trades van elke gevolgde munt uit de
   Alchemy-websocket (één abonnement per munt, op de bonding curve en na graduatie op de pump-amm-pool,
   maximaal 150, boven 80% van het dagelijkse CU-plafond vallen de proefabonnementen als eerste af).
   `PUMPPORTAL_API_KEY` in `.env` schakelt de PumpPortal-trades weer in.
2. **pump-amm-events en de Global-account.** Graduatie-transacties bevatten pump-amm-events
   (`CreatePoolEvent`, `BuyEvent`) die nu gedecodeerd worden; `fart events classify` laat zien welke
   onbekende events overblijven en welk programma ze stuurt. `fart chain global` leest de pump.fun
   Global-account en vergelijkt hem met `config/pumpfun.json` (fee is 0,95%, niet 1%).
3. **X als bevestiging, nooit als trigger.** Een aparte dienst volgt de grote accounts (elke 60 s), de
   KOL's (elke 180 s), meet hoe snel een post likes verzamelt en telt vermeldingen van een munt
   (contractadres, `$TICKER`, exacte naam) voor munten met een observatie. Alles binnen een hard
   dagbudget in dollars (`X_DAILY_BUDGET_USD`, standaard $3). Een "snelle post" waarvan zeldzame woorden
   in drie of meer nieuwe muntnamen opduiken wordt een `fast_post`-beslissing: een observatie, geen tier.
4. **Telegram naar jouw eigen chat.** Observaties gebundeld per 5 minuten (max 6 per uur), snelle posts
   direct, elke ochtend 06:10 UTC het dagrapport, en gezondheidsmeldingen (stroom stil, budget op 80% of
   op het plafond, PumpPortal 403/429). Alleen met `TELEGRAM_NOTIFY_ENABLED=1`; ieder bericht eindigt met
   "Observatie, geen advies. Alles wordt achteraf gemeten." De community komt pas na een gemeten week.

Dagelijks kijken: `sudo -u fart /opt/fart/bin/fart status`, `… coverage --hours 24`, `… notify outbox`,
en `out/recall/<dag>.md`. Wat nog gecontroleerd moet worden staat in `docs/UNVERIFIED.md` en in de
"Upgrade from milestone 1"-sectie van `deploy/DEPLOY.md`.

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
make check                       # py_compile + pytest + systemd-analyze verify + bash -n
python -m fart status            # works on an empty database
python -m fart ingest            # daemon; creates+migrations via PumpPortal and RPC, per-mint trades via a keyed RPC
python -m fart engine            # daemon; 15 s loop
python -m fart social --once     # one X tick (idle without X_BEARER_TOKEN)
python -m fart notify outbox     # last 20 Telegram outbox rows; `notify test` sends one message when enabled
python -m fart events classify   # undecoded event discriminators with best-guess names
python -m fart chain global      # pump.fun Global account vs config (exit 1 on mismatch)
python -m fart measure track     # one tracker run
python -m fart measure report --day 2026-10-25
make live                        # opt-in 60 s PumpPortal smoke test (FART_LIVE=1), skips offline
```

Configuration: `config/*.json` (thresholds, cost tables, curve parameters) and
`$FART_ROOT/.env` (names in `.env.example`; keys only on the server, never in the repo).

Layout:

```
fart/          package: config, db (schema v2), log, util, version, cli
fart/net/      budget gate (rate, daily caps, health, backoff) + urllib JSON client
fart/chain/    base58, PDA derivation, Anchor event decoder (pump.fun + pump-amm), tx parsing, curve math, Global account, RPC client
fart/ingest/   raw store, normalization, PumpPortal + RPC log + RPC per-mint trade clients, sweep, coverage, retention
fart/engine/   meta clustering, as-of features (incl. X), decisions, RPC enrichment, daemon
fart/social/   X budget (USD), X client (mega/KOL/refresh/confirmation/fast posts), daemon
fart/notify/   outbox, Telegram sender, Dutch formatting, daemon
fart/measure/  prices, outcomes, paper trades, daily report
config/        pumpfun, budget, ingest, engine, measure, stopwords, x, x_kols, notify
deploy/        systemd units, slice, timers, install.sh (bin/fart wrapper), backup.sh, DEPLOY.md
docs/          ARCHITECTURE (EN), METING (NL), PUMPFUN-EVENTS, UNVERIFIED, DECISIONS
tests/         pytest suite with synthetic fixtures; opt-in live smoke test
```

See `docs/ARCHITECTURE.md` for the design and `deploy/DEPLOY.md` for the VPS procedure.
