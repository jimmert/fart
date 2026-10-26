# fart

## Voor Jim: wat milestone 6 toevoegt

Mijlpaal 6 maakt de X-laag veilig in gebruik te nemen zonder de meetweek te raken, laat zien waarom de munt-tak
weinig observaties geeft, en sluit twee operationele gaten. Geen regel verandert (de vingerafdruk blijft
`sha256:e3bf4320c45c`; dezelfde test bewaakt dat). De beslissingen staan in `docs/DECISIONS.md`, de definities in
`docs/METING.md`, de upgrade stap voor stap in `deploy/DEPLOY.md` ("Upgrade to milestone 6"):

1. **X in schaduwstand.** Nieuwe naam in `.env`: `X_MODE` (`off` zonder sleutel, `shadow` zodra je
   `X_BEARER_TOKEN` zet, `live`). In `shadow` draait `fart-social` helemaal (mega-accounts, KOL's, verversing,
   bevestigingstellingen, snelle posts) binnen het dagbudget, maar niets ervan bereikt de beslissingen: snelle
   posts gaan naar `shadow_decisions`, tellingen naar `shadow_confirmations`, en de engine ziet geen X. Een test
   bewijst dat de beslissingen van een schaduwdag byte voor byte gelijk zijn aan die van een dag zonder X. `live`
   kan pas met `"x_live": true` in `config/engine.json` (een regelwijziging, nieuwe vingerafdruk): ná de meetweek.
   Dag- en weekrapport krijgen een sectie "X-schaduw": wat X zou hebben toegevoegd, hoeveel observaties het binnen
   1 u / 24 u bevestigde, en de uitkomst van die schaduwbeslissingen met dezelfde noemerregels als de echte.
2. **Droogloop zonder netwerk:** `fart social dryrun` toont per soort aanroep de verzoeken en dollars per dag,
   het totaal tegenover `X_DAILY_BUDGET_USD`, en de uren waarin de envelop, de reserve of het KOL-plafond
   aanroepen zou weigeren. De prijzen in `config/x.json` controleer je zelf tegen de X-developer-console
   (`docs/UNVERIFIED.md` zegt hoe).
3. **Waarom wel of geen observaties:** `fart engine explain --day 2026-10-27` (en dezelfde tabel in het dagrapport)
   toont per drempel hoeveel kandidaten eraan voldeden, de verdeling per feature, het aandeel probation-munten
   met trades per uur naast de stroompauzes, en hetzelfde voor thema's. Alleen lezen, geen advies over drempels.
4. **Backupcontrole:** `fart backup verify` zet het nieuwste archief terug onder `data/`, controleert het en
   vergelijkt de rijaantallen met de live database; `deploy/backup.sh` doet dat na elke backup en de unit faalt
   als het misgaat. In DEPLOY.md staan de offsite-kopie (`scp`) en een hersteloefening stap voor stap.
5. **Kostenijking:** `fart budget calibrate --day <dag> --dashboard-cu <N>` rekent uit het Alchemy-dashboardtotaal
   de CU per byte van de websocketframes terug en zegt welke `cu_per_byte` daarbij hoort; het schrijft niets.

Na de upgrade: `fart status` toont `schema_version: 6`, de regel `x: ... mode off` (of `shadow` met sleutel), de
schaduwtellers en de laatste backupcontrole; de rooktests zijn `fart social dryrun`, `fart engine explain --day
2026-10-27` en `fart backup verify`.

## Voor Jim: wat milestone 5 toevoegt

Mijlpaal 5 levert de stukken voor de meetweek en bereidt de community-stap voor zonder hem te zetten. Geen
regel, winnaarsdefinitie of meetmethode verandert (de vingerafdruk van de regelbestanden is gelijk aan die van
`150ca73cb354`; een test bewaakt dat). De beslissingen staan in `docs/DECISIONS.md`, de definities in
`docs/METING.md`, de upgrade stap voor stap in `deploy/DEPLOY.md` ("Upgrade to milestone 5"):

1. **Weekrapport** `fart measure week --end <dag> --days 7` (timer dagelijks 06:30 UTC, de maandagrun is de
   hele kalenderweek) naar `out/recall/week-<einddag>.md|json`: per set gelijke regels × meetmethode het
   universum per dag, de trefkans van munt-observaties (onbekend telt mee in de noemer, nog bezig apart),
   recall vóór eerste trade en vóór graduatie (munt óf thema), lead time (mediaan, p25, p75, bins),
   blokbootstrap-intervallen per dag (9.999 trekkingen, vaste seed; onder 3 dagen of 20 winnaars "te weinig
   data"; een NIET GEDEKT-dag is nooit een nul), kosten per dag (CU-schatting, X) en de go/no-go-toets.
2. **Go/no-go-criteria** vooraf bevroren (`config/measure.json` → `community_gate`): 5 complete dagen, 30
   winnaars, trefkans-ondergrens ≥ 0,15, recall-ondergrens ≥ 0,25, mediane lead ≥ 10 min, hoogstens 12
   observaties per dag. Startwaarden; bijstellen kan vóór het einde van de week, nooit erna. Het rapport toont
   alleen voldaan / niet voldaan / te weinig data en beslist of verstuurt niets.
3. **Community-kanaal, standaard uit.** Nieuwe namen in `.env` (`TELEGRAM_COMMUNITY_CHAT_ID` leeg,
   `TELEGRAM_COMMUNITY_ENABLED=0`): zonder beide gaat er nooit iets naar een andere chat dan de jouwe, ook geen
   rij die al in de outbox stond. `TELEGRAM_COMMUNITY_PREVIEW=1` stuurt je de community-tekst als extra
   bericht met de kop "VOORBEELD community-bericht" (binnen je eigen limieten): wat gezien is, waarom het
   opvalt, de bevroren regels, een vaste waarschuwing, het mint-adres als tekst, geen link of koopknop. De
   community-regels (1 per munt en thema per 24 u, 6 per uur, stil 02:00-08:00) en het wekelijkse
   verantwoordingsbericht (maandag 06:45 UTC) bestaan al.
4. **Meetfidelity**: het aandeel graduaties met minstens elke 10 minuten een sample staat in dag- en
   weekrapport; zonder voldoende samples telt een graduatie als "uitkomst onbekend", nooit als "geen
   winnaar"; `measure.track.skipped` in `fart status` telt wat de tracker overslaat. `method_version` blijft m1.0.
5. **M4-restpunt**: de 195 oude `meta_burst`-rijen die probation 5 plekken lieten, worden door de engine elke
   tick en door schemamigratie v5 eenmalig teruggebracht tot hoogstens 40 (oudste eerst, nooit met een trade in
   de laatste 10 minuten, nooit uit de bevroren sets).

Na de upgrade: `fart status` toont `schema_version: 5`, `meta_burst <= 40`, de regel `telegram community:
not configured, disabled` en `tracker: skipped mints total=0`; de rooktest op echte data is
`fart measure week --end 2026-10-27 --days 1` (verstuurt niets).

## Voor Jim: wat milestone 4 repareert

Mijlpaal 4 voegt niets toe; het lost de bevindingen van de M3-uitrol van 26 oktober 2026 op (de
beslissingen in `docs/DECISIONS.md`, de upgrade stap voor stap in `deploy/DEPLOY.md`, "Upgrade to
milestone 4"):

1. **De watchlist en de trade-stroom zijn stabiel.** `watch.add()` verdringt alleen nog strikt lagere
   prioriteit en reset `since` nooit; 100 van de 200 plekken zijn voor probation (de nieuwe munten); de
   engine kiest per burstende meta de 5 jongste munten, hoogstens 40 in totaal, en houdt die keuze vast
   zolang de meta burst. De stroom meldt pas aan na twee syncs die de munt willen en pas af na 60 s
   niet-gewenst. De reproductie van de churn (5.298 abonnementen in 52 minuten) staat in
   `tests/test_m4_watchlist_stream.py` en geeft nu nul af-/aanmeldingen.
2. **De stroom krijgt een dagaandeel**: `daily_cu_share` 0,35 van het dagplafond (1,46% per uur, de
   0,015 van de VPS-override); de rest blijft voor rpc_logs, verrijking en engine.
3. **De kostentabel is geijkt** op de gepubliceerde Alchemy-cijfers en websocket-frames worden per byte
   geboekt (`cu_per_byte` 0,0002; 0,04 als het dashboard dat zegt). `fart status` toont bytes én CU per
   stroom, alleen open abonnementen, en apart wat vandaag sloot en waarom. De ijkstap tegen het dashboard
   staat in `docs/UNVERIFIED.md`.
4. **Het universum blijft compleet**: een PumpPortal-migratie van een munt van vóór de start van fart
   wordt bewaard met `coin_unknown=1` (nooit gevolgd) en telt mee in `grads_without_create`.
5. **Opruimen**: schemamigratie v4 sluit de 35 `graduation`-rijen uit mijlpaal 2; de VPS-overrides
   vervallen door de rsync.

Na de upgrade zet je in `/opt/fart/.env` `RPC_DAILY_BUDGET_CU` terug op `3000000` en vergelijk je de
volgende ochtend `rpc CU today by method` met het Alchemy-dashboard.

## Voor Jim: wat milestone 3 repareert

Mijlpaal 3 voegt niets toe; het repareert wat de VPS op 26 oktober 2026 liet zien (details en de
beslissingen in `docs/DECISIONS.md`, de upgrade stap voor stap in `deploy/DEPLOY.md`, "Upgrade to
milestone 3"):

1. **De engine schrijft weer.** Elke schrijftransactie begint nu met `BEGIN IMMEDIATE`, blijft kort en
   wordt bij een lock tot drie keer herhaald; de busy-timeout is 15 s. Wat tóch mislukt staat in
   `fart status` onder `failures:` en in het dagrapport; `fart ingest replay --source rpc_logs --since <tijd>`
   verwerkt de ruwe regels van zo'n periode opnieuw.
2. **De trade-stroom kost niet meer de hele dag CU.** USDC en PUMP (en USDT, WSOL) kunnen nooit meer een
   munt, migratie of watchlist-item worden; geweigerde PumpPortal-berichten staan in `rejected_messages`;
   de valse `trades`-rijen worden bij de upgrade opgeruimd; `rpc_trades` volgt munten alleen nog vóór
   graduatie, met maximaal 2.000 notificaties per abonnement per uur en (sinds mijlpaal 4) een dagaandeel
   van 35% van het dagplafond voor de hele stroom (`rpc CU today by method` toont de stroom apart als
   `ws_trade_*`). Gegradueerde
   munten meet de tracker via DexScreener.
3. **De backup werkt weer** (`gzip -n -c` in plaats van `gzip -f`, plus `gzip -t`): controleer met
   `systemctl show -p Result fart-backup.service`.
4. **Het dagrapport is eerlijk over uren zonder fart**: `NIET GEDEKT` met lege percentages, een kolom
   `gedekt` per uur, en een sectie "Verrijking en fouten" met de verrijkingslimiet (nu een deel van het
   CU-plafond, migraties eerst) en de fouten van de dag.
5. **De fees kloppen**: `fart chain global` is tegen de officiële IDL gecontroleerd; 0,95% + 0,05% =
   1,00% (`config/pumpfun.json`), en `fart events classify` noemt de resterende events bij hun naam
   (`InitBoostEvent` van pump-amm was de grootste onbekende).

Na de upgrade zet je in `/opt/fart/.env` `RPC_DAILY_BUDGET_CU` terug op `3000000`.

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
python -m fart ingest            # daemon; creates+migrations via PumpPortal and RPC, per-mint trades (pre-graduation) via a keyed RPC
python -m fart ingest replay --source rpc_logs --since 2026-10-26T08:00Z   # re-feed raw hour files after handler failures
python -m fart engine            # daemon; 15 s loop
python -m fart social --once     # one X tick (idle without X_BEARER_TOKEN)
python -m fart notify outbox     # last 20 Telegram outbox rows; `notify test` sends one message when enabled
python -m fart events classify   # undecoded event discriminators with best-guess names
python -m fart chain global      # pump.fun Global account vs config (exit 1 on mismatch)
python -m fart measure track     # one tracker run
python -m fart measure report --day 2026-10-25
python -m fart measure week --end 2026-11-02 --days 7   # week report with bootstrap intervals and the frozen go/no-go test
make live                        # opt-in 60 s PumpPortal smoke test (FART_LIVE=1), skips offline
```

Configuration: `config/*.json` (thresholds, cost tables, curve parameters) and
`$FART_ROOT/.env` (names in `.env.example`; keys only on the server, never in the repo).

Layout:

```
fart/          package: config, db (schema v5, write_tx), log, util, version (rules_version + rules_fingerprint), cli
fart/net/      budget gate (rate, daily caps, health, backoff) + urllib JSON client
fart/chain/    base58, PDA derivation, Anchor event decoder (pump.fun + pump-amm), tx parsing, curve math, Global account, RPC client
fart/ingest/   raw store, normalization, PumpPortal + RPC log + RPC per-mint trade clients, sweep, coverage, retention
fart/engine/   meta clustering, as-of features (incl. X), decisions, RPC enrichment, daemon
fart/social/   X budget (USD), X client (mega/KOL/refresh/confirmation/fast posts), daemon
fart/notify/   outbox, Telegram sender, Dutch formatting (owner + community), daemon with the chat guard
fart/measure/  prices, outcomes, paper trades, daily report, week report (bootstrap, go/no-go)
config/        pumpfun, budget, ingest, engine, measure, stopwords, x, x_kols, notify
deploy/        systemd units, slice, timers (track 5 min, report 06:00, week 06:30, backup 05:30), install.sh, backup.sh, DEPLOY.md
docs/          ARCHITECTURE (EN), METING (NL), PUMPFUN-EVENTS, UNVERIFIED, DECISIONS
tests/         pytest suite with synthetic fixtures; opt-in live smoke test
```

See `docs/ARCHITECTURE.md` for the design and `deploy/DEPLOY.md` for the VPS procedure.
