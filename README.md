# fart

## Voor Jim: wat milestone 10 toevoegt

Mijlpaal 10 maakt het openbare dashboard snel en robuust zonder iets aan het ontwerp of de inhoud te veranderen. Direct na
de uitrol van M9 deed `GET /` op de VPS twee keer een time-out na 60 seconden; koud duurde het thema-onderdeel meer dan
150 seconden en `status`, `live` en `coverage` zo'n 7 seconden. Drie oorzaken, drie veranderingen; geen regel verandert (de
vingerafdruk blijft `sha256:e3bf4320c45c`; dezelfde test bewaakt dat), geen nieuwe dependency, de website blijft
alleen-lezen. De beslissingen staan in `docs/DECISIONS.md`, de uitrol stap voor stap in `deploy/DEPLOY.md` ("Milestone 10:
performance"):

1. **Thema's komen van de engine.** De engine schrijft elke tick de gerangschikte thema's die hij toch al berekent als JSON
   in kv (`engine.metas_latest`); de thema-kaart leest alleen die waarde en rekent niets meer. Is de waarde ouder dan 5
   minuten (engine uit of nog niet herstart), dan zegt de kaart "Thema's worden nu niet berekend" met de leeftijd, en toont
   hij geen cijfer.
2. **Indexen voor tijdvensters.** Schema v8 voegt `trades(received_at)` en `trades(block_time)` toe; elk tijdvenster van de
   openbare onderdelen begint op de geïndexeerde ontvangsttijd en legt de eventtijd daar overheen, zodat geen venster meer
   een tabel scant (een test bewijst het met `EXPLAIN QUERY PLAN`). De dekking van de laatste 24 uur komt uit de
   `coverage`-tabel die de ingest elke minuut bijhoudt.
3. **Een cache die nooit laat wachten.** Eén achtergrondthread in `fart web` berekent de openbare onderdelen op hun eigen
   ritme (bij de start, daarna elke 15 s de live-onderdelen en elke 30 s de rest); een verzoek leest alleen de laatste
   waarde en wacht nergens op. Een onderdeel dat nog nooit berekend is zegt "wordt berekend" (JSON: `{"state": "computing"}`).
   Een berekening langer dan 10 s komt in het logboek; de laatste duur per onderdeel staat op `/eigenaar` en in `fart status`
   (`web views: themes 0.02 s, live 0.4 s, ...`).

Na de uitrol: `curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://kingdegen.dev/` geeft `200` en een tijd onder
een seconde, koud en warm; `fart status` toont `schema_version: 8` en de regel `web views:`. Zegt de thema-kaart de eerste
minuut "nu niet berekend": dat klopt tot de herstarte engine één keer getikt heeft.

## Voor Jim: wat milestone 9 toevoegt

Mijlpaal 9 maakt van de website een openbaar product: `https://kingdegen.dev` is nu het gezicht van de radar naar buiten, met
het live dashboard als eerste pagina en een kleine eigenaarssectie achter de login. Geen regel verandert (de vingerafdruk
blijft `sha256:e3bf4320c45c`; dezelfde test bewaakt dat), geen schema-migratie, geen nieuwe dependency, de daemons blijven
zoals ze zijn en de site blijft alleen-lezen. De beslissingen staan in `docs/DECISIONS.md`, de uitrol stap voor stap in
`deploy/DEPLOY.md` ("Milestone 9: website v2"):

1. **Het dashboard is verwerkte data, live.** `/` laat zien wat de radar nú ziet: waar het geld heen gaat (de gevolgde munten
   met de meeste netto SOL-instroom in de laatste 5 minuten, met kopers, trades, marktkapitalisatie en het aandeel van de
   grootste koper), de nieuwste munten met hun tempo, de thema's die nu opkomen in de muntnamen (dezelfde clustering als de
   engine), de laatste graduaties met de nieuwste prijssample, wat er rondgaat op X zodra die laag draait, de observaties van
   vandaag en één regel meting. De pagina ververst zichzelf elke 15 seconden. Alles blijft observatie: geen koopknop, geen link
   naar een handelsplaats, het mint-adres alleen als tekst.
2. **De meting heeft een eigen pagina.** `/meting` toont de meetweek (dag x van 7, gedekte dagen, winnaars, trefkans en recall
   met het bootstrapinterval zodra dat bestaat, de go/no-go-toets in gewone woorden), de dekking van de laatste 24 uur als
   balk met de NIET GEDEKT-uren gearceerd, de laatste rapporten en "hoe dit werkt". `/observaties` heeft elke observatie met
   uitkomst in gewone woorden, `/rapporten` elk dag- en weekrapport als nette pagina met grafieken, `/methode` de werkwijze, de
   bevroren regels, de begrippenlijst en de volledige disclaimer, `/tools` een lege plek voor later.
3. **Eén ontwerp.** Donker thema standaard, licht thema via de systeeminstelling, één accentkleur, kaarten met een kerncijfer,
   één regel uitleg en een statuspil (vers, oud, NIET GEDEKT, onbekend, te weinig data), grafieken als inline SVG zonder
   bibliotheek, bruikbaar op een telefoon. De naam en de accentkleur staan in `config/web.json` en veranderen zonder code.
4. **Eigenaar achter de login.** `/eigenaar` heeft alles wat operationeel is (versheid van daemons en timers, budgetten,
   X-planner, instellingen, logboeksamenvatting) en de ruwe Markdown van elk rapport; de M8-API onder `/api/*` blijft. Login,
   CSRF en de lockout zijn ongewijzigd. Met `FART_WEB_PUBLIC=0` in `.env` staat alles weer achter de login zoals in M8.
5. **Openbaar en toch veilig en snel.** Alleen de openbare velden onder `/api/public/*` (een test bewijst dat geen enkel
   budget, sleutel, pad of adres erin voorkomt), een cache van 15 of 30 seconden per onderdeel zodat bezoekers de database
   niet belasten, `Cache-Control` en `ETag` op elk openbaar antwoord, 120 verzoeken per minuut per adres en daarna 429, en
   dezelfde strikte Content-Security-Policy als in M8.

Na de uitrol: `curl -sI https://kingdegen.dev/` geeft 200 met `cache-control: public, max-age=15`, `/eigenaar` geeft 401, en
`fart status` zegt `public section on`. Zie je op het dashboard "De X-laag staat uit": dat klopt tot de X-sleutel erin gaat.

## Voor Jim: wat milestone 8 toevoegt

Mijlpaal 8 zet de website `https://kingdegen.dev` neer, met het dashboard als eerste pagina. Alles erop is alleen-lezen,
ook voor jou: er is geen knop die iets verandert. Geen regel verandert (de vingerafdruk blijft `sha256:e3bf4320c45c`;
dezelfde test bewaakt dat), geen schema-migratie, geen nieuwe dependency, geen van de bestaande daemons wordt aangeraakt.
De beslissingen staan in `docs/DECISIONS.md`, de uitrol stap voor stap in `deploy/DEPLOY.md` ("Milestone 8: website"):

1. **Waar de site draait.** Op de VPS zelf, als `fart-web.service` (gebruiker `fart`, dezelfde hardening als de andere
   units, luistert alleen op `127.0.0.1:8792`, opent de database uitsluitend alleen-lezen en schrijft niets behalve zijn
   log), achter de gedeelde Caddy die er al staat; die krijgt één sitebestand (`deploy/caddy/fart.caddy`) voor
   `kingdegen.dev`. Er gaat niets naar een andere host en er gaat geen nieuwe poort open.
2. **Login.** Eén eigenaarsaccount: jouw wachtwoord staat alleen als scrypt-hash in `.env` (`FART_WEB_PASSWORD_HASH`,
   gemaakt met `fart web hash-password`, dat het wachtwoord onzichtbaar vraagt en alleen de hash print) naast een
   geheim voor de ondertekende sessiecookie (`FART_WEB_SECRET`, van `fart web new-secret`; cookie HttpOnly, Secure,
   SameSite=Strict, 12 uur geldig). Vijf foute wachtwoorden binnen 10 minuten, per adres én voor het account, betekenen
   15 minuten wachten; de pagina zegt hoeveel minuten. Zonder geldige sessie geeft elke pagina en elk `/api/`-adres de
   loginpagina of een 401; zonder beide `.env`-waarden start de dienst niet (duidelijke melding, geen herstartlus).
3. **Pagina's.** `/` dashboard: versheid van de daemons en timers, de dekking van de laatste 24 uur per uur (een uur
   NIET GEDEKT blijft leeg, nooit nul), beslissingen van vandaag, de X-stand met de planner (actieve sleutels,
   stopredenen, plan tegenover uitgaven), de budgetten (CU en X tegenover het plafond), de kernpunten van het laatste
   dagrapport en de go/no-go-tabel van het laatste weekrapport; `/observaties`: de laatste 200 beslissingen met
   features, rules_version en uitkomst (winnaar / geen winnaar / onbekend / nog bezig); `/rapporten`: elk dag- en
   weekrapport als pagina; `/instellingen`: versies, vingerafdruk, de bevroren go/no-go-criteria en de actieve
   instellingen zonder geheimen. Tijden in Nederlandse tijd met UTC erachter; de banner "Observatie, geen advies. Alles
   wordt achteraf gemeten." staat op elke pagina; de cijfers verversen elke 60 seconden. Dezelfde gegevens als JSON onder
   `/api/status`, `/api/coverage`, `/api/decisions`, `/api/x`, `/api/report/latest` en `/api/week/latest`.
4. **Wat de site níet doet.** Niets schrijven (een schrijfpoging op de databaseverbinding faalt; een test bewijst het),
   niets van buiten laden (geen extern script, lettertype of tracker; een strikte Content-Security-Policy), nooit een
   cookie, wachtwoord of token in het log, geen winst- of rendementscijfer: wat onbekend is blijft onbekend.
5. **Uitrol** (door de deployer): `.env` krijgt de twee nieuwe namen via jouw helper (die kent ze nog niet; DEPLOY.md
   zegt wat erbij moet), `fart-web` start, het Caddy-sitebestand gaat naar `/srv/fart/fart.caddy` en wordt geïmporteerd,
   `kingdegen.dev` gaat uit het oude meme-radar-launch-blok, eerst `caddy validate`, dan herladen; de controle is
   `curl -sI https://kingdegen.dev` (de loginpagina met de beveiligingsheaders). Terugdraaien is de bewaarde kopie van de
   Caddyfile terugzetten.

Na de uitrol: `fart status` toont `web: password hash configured, secret configured, bind 127.0.0.1:8792`; de rooktest
is `curl -sI http://127.0.0.1:8792/login` op de VPS (200 met de headers) en daarna de site zelf in je browser.

## Voor Jim: wat milestone 7 toevoegt

Mijlpaal 7 maakt van de X-bevestigingslaag een kostenbewuste planner: binnen jouw dagplafond van $10 tot $20 haalt hij
de meeste bevestigende informatie uit X en geeft hij niets uit wat geen informatie toevoegt. Geen regel verandert (de
vingerafdruk blijft `sha256:e3bf4320c45c`; dezelfde test bewaakt dat). De beslissingen staan in `docs/DECISIONS.md`, de
definities in `docs/METING.md`, de formule in `docs/UNVERIFIED.md`, de upgrade stap voor stap in `deploy/DEPLOY.md`
("Upgrade to milestone 7"):

1. **Sleutels alleen van observaties, in een harde volgorde.** Per `observe`-beslissing het mint-adres, de `$TICKER`
   (≥ 3 tekens, niet generiek) en de exacte naam van ≥ 2 woorden; van thema's alleen de 5 sterkste. Jongste munt eerst,
   thema's daarna, hoogstens 20 tegelijk. Een sleutel stopt zodra hij beslist is en de nieuwe tabel `x_keys` zegt
   waarom: bevestigd (telling ≥ 10 in het laatste uur, daarna precies één zoekopdracht), weerlegd (drie tellingen zonder
   groei: een stille munt kost 4 tellingen, 45 minuten, $0,02), verlopen (6 u), gegradueerd, dood, verdrongen, uitgeput.
2. **Adaptief schema.** Tellingen om de 15 minuten in de eerste 2 uur, daarna elk uur (hoogstens 12), één zoekopdracht
   van hoogstens 10 posts met een opvraag van hoogstens 3 auteurs; mega-watch 60 s in de piekuren en 120 s daarbuiten,
   KOL-watch 180 s / 300 s. Volle belasting: 60 sleutels × (12 × $0,005 + 10 × $0,005 + 3 × $0,010) = **$8,40 per dag**
   als plafond voor de bevestigingslaag, $9,89 met mega en KOL = 66% van $15 (M6 zou circa $119 per dag kosten).
3. **Planner in plaats van weigeraar.** Het dagplafond, de 30%-reserve voor mega + KOL en het KOL-plafond toetsen het
   plafondbedrag van elke aanroep (het plafond kan nooit overschreden worden); de uurenvelop stelt uit in plaats van te
   weigeren. Zet in `.env` `X_DAILY_BUDGET_USD=15.00` (binnen je $10 tot $20; zonder die regel valt de code terug op
   $3,00, waaronder de volle belasting niet past). `fart social dryrun` en `fart status` tonen per soort het geplande en
   het werkelijke bedrag en het aandeel van het plafond.
4. **Kwaliteit per dollar, meetbaar in schaduw.** Dag- en weekrapport tonen in "X-schaduw" de kosten per bevestigde
   observatie, het aandeel observaties dat binnen 1 uur bevestigd werd, de tellingen ná de eerste bevestiging
   (verspilling), de gestopte sleutels per reden met wat de weerlegde kostten, en de uitkomst van bevestigde tegenover
   niet-bevestigde observaties met dezelfde noemerregels als de trefkans. Geen advies: jij beslist hierop na de meetweek
   of X live gaat.
5. **Echte prijzen:** `fart social calibrate --day <dag> --console-usd <N>` zet het bedrag uit de X-developer-console
   naast ons grootboek per soort en eenheid en zegt welke prijs daarbij hoort en welke `prices`-regel een commit vraagt;
   het schrijft niets. Zodra er echte aanroepen zijn, rekent de droogloop met de waargenomen aantallen ("observed rates").

Na de upgrade: `fart status` toont `schema_version: 7`, `daily budget $15.00`, de regels `x planner:` en `x plan vs
spend today:`; de rooktest vóór de sleutel is `fart social dryrun` (de regel `full load: ... fits`), de ijkstap na de
eerste volle dag met sleutel `fart social calibrate`.

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
python -m fart social dryrun     # the X plan and USD per kind per day, the full-load formula, no HTTP
python -m fart social calibrate --day 2026-10-28 --console-usd 7.10   # our X ledger against the console's amount, writes nothing
python -m fart notify outbox     # last 20 Telegram outbox rows; `notify test` sends one message when enabled
python -m fart events classify   # undecoded event discriminators with best-guess names
python -m fart chain global      # pump.fun Global account vs config (exit 1 on mismatch)
python -m fart measure track     # one tracker run
python -m fart measure report --day 2026-10-25
python -m fart measure week --end 2026-11-02 --days 7   # week report with bootstrap intervals and the frozen go/no-go test
python -m fart web hash-password # the owner's password as an scrypt hash for .env (asked invisibly; prints the hash only)
python -m fart web new-secret    # the cookie-signing secret for .env
python -m fart web               # the website on 127.0.0.1:8792 (read-only; needs both .env values; FART_WEB_PUBLIC=0 puts it behind the login)
make live                        # opt-in 60 s PumpPortal smoke test (FART_LIVE=1), skips offline
```

Configuration: `config/*.json` (thresholds, cost tables, curve parameters) and
`$FART_ROOT/.env` (names in `.env.example`; keys only on the server, never in the repo).

Layout:

```
fart/          package: config, db (schema v7, write_tx), log, util, version (rules_version + rules_fingerprint), cli
fart/net/      budget gate (rate, daily caps, health, backoff) + urllib JSON client
fart/chain/    base58, PDA derivation, Anchor event decoder (pump.fun + pump-amm), tx parsing, curve math, Global account, RPC client
fart/ingest/   raw store, normalization, PumpPortal + RPC log + RPC per-mint trade clients, sweep, coverage, retention
fart/engine/   meta clustering, as-of features (incl. X), decisions, RPC enrichment, daemon
fart/social/   X budget planner (USD), confirmation planner (keys, schedule, stop reasons), X client (mega/KOL/refresh/confirmation/fast posts), dry run, console calibration, daemon
fart/notify/   outbox, Telegram sender, Dutch formatting (owner + community), daemon with the chat guard
fart/measure/  prices, outcomes, paper trades, daily report, week report (bootstrap, go/no-go)
fart/web/      the website (milestones 8 and 9): auth (scrypt, signed cookies, CSRF, login guard), owner data views (data), public views (public), SVG charts, theme, Markdown subset, pages, server with cache/ETag/rate limit, static/
config/        pumpfun, budget, ingest, engine, measure, stopwords, x, x_kols, notify
deploy/        systemd units (incl. fart-web), slice, timers (track 5 min, report 06:00, week 06:30, backup 05:30), install.sh, backup.sh, caddy/fart.caddy, DEPLOY.md
docs/          ARCHITECTURE (EN), METING (NL), PUMPFUN-EVENTS, UNVERIFIED, DECISIONS
tests/         pytest suite with synthetic fixtures; opt-in live smoke test
```

See `docs/ARCHITECTURE.md` for the design and `deploy/DEPLOY.md` for the VPS procedure.
