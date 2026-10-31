# Meting: wat we meten en hoe je het dagrapport leest

Dit document beschrijft de definities achter `fart measure` en het dagrapport
`out/recall/JJJJ-MM-DD.md`. Alles hier is bewust streng: een winnaar die we niet kunnen meten
telt als **onbekend**, nooit als nul en nooit als raak.

## Begrippen

**Universum.** Alle graduaties (migraties van de bonding curve naar pump-amm) van een UTC-dag,
zoals wij ze zelf gezien hebben (PumpPortal, RPC-logstroom, RPC-sweep). Het universum is
onafhankelijk van onze eigen beslissingen: ook munten waar we nooit naar gekeken hebben tellen
mee. Zonder volledig universum is recall een leugen; daarom staat de dekking (coverage) bovenaan.

**Dekking (coverage).** Per uur: hoeveel creates PumpPortal zag, hoeveel de RPC zag, de unie,
het aantal graduaties en het aantal graduaties zonder bekende create. Een uur is **gedekt** als
minstens één bron van creates een deel van dat uur actief was (een bron telt pas vanaf haar eerste
start; een periode waarin fart helemaal niet draaide is een `down`-gat voor elke bron). Een uur is
**compleet** als het voorbij is en minstens één bron het hele uur actief was zonder onhersteld gat.
Niet gedekt is nooit compleet. Gaten zijn intervallen waarin een stroom niet verbonden was (opstart,
verbreking, watchdog, budgetplafond, afsluiten, down). `grads_without_create` hoort dicht bij nul te
liggen; anders missen we creates. Uitzondering sinds mijlpaal 4: een graduatie die PumpPortal meldt van
een munt die vóór de start van fart is aangemaakt, wordt bewaard met `coin_unknown=1` en telt hier ook
mee (het universum blijft compleet, de munt wordt nooit gevolgd); het dagrapport noemt dat aantal apart.

**Winnaar (hit_100k).** Piek-marktkapitalisatie ≥ $100.000 binnen 24 uur na het ankermoment
**én** volume ≥ $20.000 in diezelfde 24 uur. Ankermoment = de graduatie (voor het universum) of
de beslissing (voor precisie). Piek komt uit onze eigen trades (curve-wiskunde × SOL/USD) voor
munten die we volgden, anders uit DexScreener-samples die elke 5 minuten genomen worden. Is de
piek onbekend: onbekend. Is de piek bekend en < $100k: geen winnaar (ook als het volume onbekend
is). Is de piek ≥ $100k maar het volume onbekend: onbekend.

**Gebundeld (bundled).** Een munt heet gebundeld als hij binnen 60 s na de create gegradueerd
is, óf als de dev-buy ≥ 50% van de curve was (≈ 42,5 SOL). Zonder dev-buy-informatie en zonder
instant-graduatie is bundeling **onbekend**. De audit van het oude systeem vond dat ongeveer de
helft van alle graduaties gebundeld is; het rapport splitst daarom alles in organisch / gebundeld
/ onbekend. Alleen organische winnaars zeggen iets over "vroeg zien".

**Lead time.** Gebeurtenis minus beslissing, in seconden. Positief = de `observe`-beslissing kwam
vóór de gebeurtenis; negatief = te laat. Twee gebeurtenissen: de eerste trade die wij zagen en de
graduatie. Per munt (beslissing op de munt zelf) en per thema (een `observe_meta`-beslissing op
een term die in de naam/ticker van de winnaar voorkomt).

**Recall.** Aandeel winnaars met een beslissing vóór graduatie. Apart voor organische winnaars.

**Precisie.** Van alle `observe`-beslissingen (munten) van de dag: het aandeel dat winnaar werd,
berekend over de beslissingen met een **bekende** uitkomst. Onbekend en nog-bezig worden apart
geteld en nooit weggemoffeld.

**Paper trade.** Per `observe`-beslissing één uitvoerbare simulatie: 0,5 SOL kopen 20 s na de
beslissing tegen de curveprijs van de eerste trade daarna (fee, impact en slippage uit de
curve-wiskunde), verkopen bij een trailing stop van 30% vanaf de piek of na 24 uur. Alle kosten
staan expliciet. Geen trade na het instapmoment → geen exit → PnL onbekend. Het rapport toont de
set mét en zónder gebundelde munten. Dit is een meetinstrument, geen handelsadvies.

**rules_version / method_version.** `rules_version` is de git-commit van de code die de
beslissing nam; `method_version` de versie van de meetdefinities. Een herberekening schrijft
nieuwe rijen; oude rijen blijven staan.

## Het dagrapport lezen

1. **Kop**: `COMPLEET` betekent 24 complete uren én elke graduatie heeft een 24-uursmeting.
   `NIET GEDEKT` betekent dat geen enkel uur van de dag gedekt was (fart draaide niet): het universum
   is dan onbekend, niet leeg, en elk percentage blijft leeg (`–`). Bij `NIET COMPLEET` lees je eerst
   "Dekking" en "Onbekenden" en wantrouw je de percentages.
2. **Samenvatting**: aantal graduaties, winnaars (organisch/gebundeld/onbekend), hoeveel winnaars
   we vóór graduatie al als observatie hadden, en de precisie van de dag.
3. **Dekking**: per uur creates/graduaties/gaten/compleet. Veel gaten = de cijfers zijn een
   ondergrens voor het universum.
4. **Winnaars**: één regel per winnaar met bundeling, beslissing en lead times. `–` is geen nul:
   er was geen beslissing of geen eerste trade bekend.
5. **Precisie van observaties** en **Paper trades**: hoe goed waren de observaties van de dag,
   met en zonder bundels.
6. **Verrijking en fouten**: hoeveel gemiste creates/graduaties de RPC-verrijking die dag ophaalde
   tegenover de daglimiet (een deel van het CU-plafond; migraties gaan voor), wat er nog in de wachtrij
   staat, en de fouten van de dag (mislukte engine-ticks, mislukte ingest-handlers, geweigerde berichten,
   afgekapte of gepauzeerde trade-abonnementen). Een dag met veel overgeslagen verrijking heeft
   graduaties zonder chain-tijd en pool-adres: ook dat is een vorm van onbekend.
7. **Onbekenden**: alles wat we niet konden meten, met reden, inclusief het aantal niet-gedekte uren.
8. **Versies**: welke code en welke meetdefinities dit rapport maakten.

## Wat dit rapport niet is

* Geen alert en geen koopadvies. Milestone 1 levert niets af, nergens.
* Geen bewijs van voorspelkracht: `observe`-beslissingen van v0 zijn startdrempels. Pas na een
  gemeten week met bevroren regels (één `rules_version`) zegt precisie iets.
* Geen volledige marktdata: DexScreener-samples elke 5 minuten missen pieken tussen samples;
  eigen trades bestaan alleen voor gevolgde munten. Beide beperkingen staan in de `json`-kolom
  van elke `outcomes`-rij (`peak_src`, `samples_n`, `trades_n`, `data_quality`).

## Het weekrapport (mijlpaal 5)

`fart measure week --end JJJJ-MM-DD --days 7` schrijft `out/recall/week-<einddag>.md` en `.json`: een
rollend venster van 7 UTC-dagen dat standaard gisteren eindigt. De timer draait elke dag om 06:30 UTC; de
maandagrun beslaat de hele kalenderweek (maandag tot en met zondag). Het rapport is een optelling van de
dagrapporten die het ter plekke opnieuw bouwt (dus met alle inmiddels gemeten uitkomsten), **per segment**:
één set regels × één meetmethode.

**Segment.** Elke deploy is een nieuwe commit en dus een nieuwe `rules_version`, ook als er geen drempel
veranderde. Daarom krijgt elke `rules_version` een *vingerafdruk* van de regelbestanden (`fart/version.py`:
`config/engine.json`, `config/pumpfun.json`, `config/stopwords.json`, curve-wiskunde, features, clustering,
verdicts). Gelijke vingerafdruk = dezelfde regels; die rules_versions staan samen in één segment en worden
allemaal genoemd. De bevroren regels van de meetweek (`150ca73cb354`, vingerafdruk in `config/measure.json`
→ `community_gate.frozen_rules`) zijn het eerste segment. Een echte regelwijziging opent een nieuw segment.
Een andere `method_version` in `outcomes` geeft een eigen segment met eigen dagrapporten.
Sinds mijlpaal 11 kan een regelbestand veranderen zonder dat de regels veranderen (de basislijn van de thema's wordt één keer
per tick geteld in plaats van per term; een test bewijst byte-gelijke uitkomsten). Zo'n vingerafdruk staat in
`config/measure.json` → `community_gate.frozen_rules_equivalents` met datum, PR-nummer en de naam van de bewijstest; het
weekrapport telt die rules_versions bij het bevroren segment en zegt onder de segmenttitel dat het om een bewezen
gelijkwaardige berekening gaat. Zonder die registratie blijft een andere vingerafdruk een eigen segment.

**Universum per dag.** Creates per bron (PumpPortal, RPC, unie), graduaties, graduaties zonder bekende
create (`coin_unknown`), winnaars (organisch / gebundeld / onbekend), geen winnaar, uitkomst onbekend (en
hoeveel daarvan door te weinig samples), uren NIET GEDEKT, piekuren niet gedekt, budgetpauzes (geblokkeerde
calls door het RPC- of X-plafond, seconden stroomgat door het plafond, pauzes van de trade-stroom door de
uurenvelop) en de dekking van de tracker. Een dag zonder gedekt uur is **NIET GEDEKT**: zijn rij toont
"onbekend", hij telt niet mee in de totalen, is geen blok voor de bootstrap en nooit een "complete dag".

**Trefkans.** Van de `observe`-beslissingen (munten) van de gedekte dagen: raak = winnaar binnen 24 u na de
beslissing. Noemer = de beslissingen waarvan de 24 u verstreken zijn, **inclusief** de beslissingen met een
onbekende uitkomst (een observatie die we niet konden meten is geen aangetoonde treffer). Beslissingen die
nog bezig zijn staan apart en komen in de noemer zodra hun 24 u voorbij zijn. Daarnaast staat de trefkans
over alleen de bekende uitkomsten. Dit is bewust voorzichtig; het is de maat waarop de go/no-go toetst.

**Recall.** Aandeel winnaars met een munt- óf thema-beslissing vóór de gebeurtenis, apart voor "vóór de
eerste trade die wij zagen" en "vóór graduatie"; munt-alleen en thema-alleen staan ernaast. Een winnaar
zonder bekende eerste trade telt niet mee in de noemer van "vóór eerste trade".

**Lead time.** Gebeurtenis minus de vroegste munt- of thema-beslissing van het segment, in seconden;
positief = op tijd. Mediaan, p25, p75 en de bins uit `config/measure.json` → `report.lead_time_bins_s`
(< -60 min, -60 tot -10, -10 tot 0, 0 tot 10, 10 tot 60, ≥ 60 min).

**Betrouwbaarheidsintervallen.** Blokbootstrap met de gedekte dagen als blokken: 9.999 keer worden de dagen
met teruglegging getrokken, per trekking de samengevoegde verhouding, percentielen bij alpha 0,05; vaste seed
20261026 zodat twee runs hetzelfde interval geven. Een trekking zonder noemer is ongedefinieerd (nooit nul).
Met minder dan 3 gedekte dagen of minder dan 20 winnaars staat er "te weinig data" in plaats van een
interval. Dagen als blokken vangen op dat beslissingen binnen een dag niet onafhankelijk zijn (één meta,
één markt); zeven blokken geven brede intervallen, en dat is eerlijk.

**Kosten.** Per dag de geschatte CU (kostentabel; de ijking met het Alchemy-dashboard staat open in
`docs/UNVERIFIED.md`, dus het blijft een schatting), het CU-plafond, de X-uitgaven in dollars en het aantal
DexScreener-verzoeken.

**Dekking van de tracker.** Aandeel gemeten graduaties met in het winnaarsvenster minstens elke 10 minuten
een `mcap_samples`-rij (de gaten naar het begin en einde van het venster tellen mee). Een graduatie die
volgens de opgeslagen uitkomst "geen winnaar" is maar níet zo gedekt werd, telt in dag- en weekrapport als
**uitkomst onbekend** (een piek tussen schaarse samples kan gemist zijn), nooit als "geen winnaar". Een
winnaar blijft een winnaar: schaarse samples kunnen geen piek verzinnen. De `outcomes`-rijen zelf veranderen
niet; `method_version` blijft m1.0. Loopt de tracker achter (meer dan 600 munten per run of DexScreener
weigert), dan telt `measure.track.skipped` dat (`fart status`) en noemt het dagrapport het aantal.

**Go/no-go.** De vaste sectie toetst de vooraf bevroren criteria uit `config/measure.json` → `community_gate`
(zie `docs/DECISIONS.md`, mijlpaal 5): minimaal 5 complete dagen (piekuren 12:00-02:00 UTC gedekt),
minimaal 30 winnaars, ondergrens trefkans-interval ≥ 0,15, ondergrens recall-interval vóór graduatie ≥ 0,25,
mediane lead time vóór graduatie ≥ 10 minuten, gemiddeld hoogstens 12 observaties per dag. Per criterium
"voldaan", "niet voldaan" of "te weinig data", plus één oordeel. Het rapport beslist niets en verstuurt niets;
het wekelijkse verantwoordingsbericht van `fart-notify` zet deze sectie in gewone woorden over.

**Dagrapport, nieuw sinds mijlpaal 5.** "Dekking van de tracker" in de samenvatting, "Piekuren niet gedekt"
onder dekking, de sectie "Budget en kosten", "Tracker: N munten overgeslagen" onder verrijking en fouten, en
"Observaties per rules_version" onder versies. `geen winnaar` en `uitkomst onbekend` volgen de
tracker-dekkingsregel.

## Mijlpaal 6: X-schaduw en "Waarom wel of geen observaties"

**X-schaduw.** Zolang `X_MODE` niet `live` is (en `live` is pas mogelijk met `"x_live": true` in
`config/engine.json`, een regelwijziging met een nieuwe vingerafdruk, dus niet in de meetweek), bereikt niets van
de X-laag de beslissingen: een snelle post die bij een burst van muntnamen past wordt een rij in
`shadow_decisions` (de kolommen van `decisions` plus `would_kind`, de soort die hij gehad zou hebben), een
bevestigingstelling een rij in `shadow_confirmations`, gekoppeld aan de observatie die ze bevestigd zou hebben,
en de engine krijgt geen mega-post-feature en neutrale X-velden. Het dag- en weekrapport tonen daarom een sectie
**X-schaduw**: hoeveel beslissingen X zou hebben toegevoegd, hoeveel bestaande observaties een telling van die
dag bevestigde (telling in het laatste uur, respectievelijk de laatste 24 uur, ≥ `search_when_count_1h`, 10), en
de uitkomst van de schaduwbeslissingen met dezelfde noemerregels als de trefkans: een schaduwbeslissing is
**winnaar** als een van haar gematchte munten binnen 24 uur na de beslissing gradueerde en die graduatie een
winnaar werd (de 24-uursmeting van de graduatie); **geen winnaar** als geen enkele gematchte munt binnen die 24
uur gradueerde (een winnaar is per definitie een graduatie) of elke graduatie met tracker-dekking geen winnaar
was; anders **onbekend**, en onbekend telt mee in de noemer; **nog bezig** zolang de 24 uur (of de 24 uur van zo'n
graduatie) niet om zijn. De echte tabellen `decisions` en `outcomes` worden hierdoor nooit geraakt; de
go/no-go-toets gebruikt de schaduwcijfers niet.

**Waarom wel of geen observaties.** De engine schrijft sinds mijlpaal 6 per UTC-dag één rij per gevolgde munt
en per gerangschikt thema (`engine_candidates`) met de hoogste waarde van elke drempel-feature die hij in een
tick zag. Het dagrapport (en `fart engine explain --day`) toont per drempel uit `config/engine.json` hoeveel
kandidaten er op hun beste moment aan voldeden, de verdeling (p50, p90, max) per feature over die maxima, het
aandeel probation-munten dat überhaupt een trade in onze data had (per uur, naast de gaten van de trade-stroom
in dat uur en de pauzes van de uurenvelop), en hetzelfde voor thema's (`burst_ratio`, `sol_inflow`,
`unique_buyers`). Het is een verdeling, geen advies: de drempels blijven wat ze zijn.

## Mijlpaal 7: kwaliteit per dollar van de X-bevestiging

**Wat gemeten wordt.** Sinds mijlpaal 7 plant de X-laag haar bevestigingen (`fart/social/planner.py`, tabel
`x_keys`): een sleutel (mint-adres, `$TICKER`, exacte naam van een munt-observatie, of de term van een van de vijf
sterkste thema-observaties) wordt geteld om de 15 minuten in de eerste twee uur na de beslissing en daarna elk uur,
hoogstens twaalf keer, en stopt zodra hij **beslist** is: **bevestigd** (telling in het laatste uur ≥
`search_when_count_1h`, 10; daarna precies één zoekopdracht van hoogstens 10 posts en een opvraag van hoogstens 3
auteurs), **weerlegd** (drie opeenvolgende tellingen zonder groei van de 24-uurstelling), **verlopen** (ouder dan 6
uur), **gegradueerd**, **dood** (van de watchlist), **verdrongen** (door een jongere observatie toen de 20 plekken vol
waren) of **uitgeput** (twaalf tellingen zonder besluit). Elke uitgave staat op de sleutel die haar veroorzaakte
(`x_keys.usd`), en elke sleutel staat bij de observatie die hem opleverde (`decision_id`). Zo is elke dollar
herleidbaar tot een observatie die erdoor bevestigd of weerlegd kon worden.

**Definities in de sectie "X-schaduw" (dag- en weekrapport).**

* **Kosten per bevestigde observatie** = de uitgaven van de UTC-dag aan de bevestigingssoorten (`ca_counts`,
  `ca_search`, `ca_authors` in `x_spend`) gedeeld door het aantal observaties dat die dag een bevestigende telling
  kreeg (`shadow_confirmations.confirmed_1h`, één keer per observatie). Zonder bevestiging die dag: "geen
  bevestiging", nooit nul.
* **Aandeel binnen 1 uur bevestigd** = de observaties van de dag waarvan de eerste bevestigende telling hoogstens 3600
  s na de beslissing viel, gedeeld door de observaties van de dag die minstens één telling kregen. Een observatie zonder
  telling (geen plek in de planning, of een sleutel die niet geldig was) telt niet in de noemer en staat apart.
* **Verspilling** = tellingen ná de eerste bevestigende telling van dezelfde observatie, gewaardeerd tegen de gemiddelde
  tellingsprijs van de dag. De planner van mijlpaal 7 stopt een bevestigde sleutel, dus dit cijfer is nul van
  constructie; het meet de werking van mijlpaal 6 en elke latere terugval.
* **Gestopte sleutels per reden** met wat de weerlegde kostten: uit `x_keys` op de dag van stoppen.
* **Uitkomst naar bevestiging**: de munt-observaties (`observe`) van de dag in drie groepen (bevestigd; geteld maar
  nooit bevestigd; zonder telling) met hun 24-uursuitkomst volgens dezelfde noemerregels als de trefkans (mijlpaal 5):
  raak = winnaar binnen 24 uur, de noemer = observaties waarvan de 24 uur verstreken zijn, onbekend telt mee in de
  noemer, nog bezig staat apart. Thema-observaties hebben geen eigen uitkomst en tellen alleen in de bevestigingscijfers.
  Het weekrapport sommeert de dagen en toont per dag een tabel.

**Wat het rapport niet doet.** Het geeft geen advies over X live. Het verschil in trefkans tussen bevestigde en
niet-bevestigde observaties, de kosten per bevestiging en het aandeel binnen een uur zijn de cijfers waarop Jim na de
meetweek beslist; de go/no-go-toets van de community-alerts gebruikt ze niet. De regels zelf veranderen niet
(`config/x.json` is geen regelbestand; de vingerafdruk blijft `sha256:e3bf4320c45c`).

**Kosten vooraf.** De droogloop (`fart social dryrun`) toont de volle belasting als plafond: 60 sleutels per dag ×
(12 tellingen × $0,005 + 10 posts × $0,005 + 3 auteurs × $0,010) = $8,40 per dag voor de bevestigingslaag, plus de
account-watches ($0,49 mega, $1,00 KOL) = $9,89 = 66% van `X_DAILY_BUDGET_USD` 15,00. Een sleutel kan niet tegelijk al
zijn tellingen en een zoekopdracht hebben, dus de echte dag blijft eronder; de prijzen zijn nog niet geverifieerd
(`docs/UNVERIFIED.md`, `fart social calibrate`).

## Mijlpaal 12: het onderzoeksharnas, bewezen regels en de Plays-laag

**Wat het harnas doet.** `fart research run --hypothesis <H>` speelt elke kandidaatmunt van het venster af in ticks van 15 s
vanaf de create, met per tick alleen wat toen kenbaar was (eventtijd én ontvangsttijd op of vóór de tick), berekent de
kenmerken van de engine opnieuw in het geheugen (unieke kopers, netto SOL, grootste koper, bundel, dev-verkoop, curvestand) en
laat elke **variant** (een instapregel × een uitstapregel) hoogstens één keer per munt **vuren**. Elk signaal wordt
uitgevoerd als een papierpositie van 0,5 SOL (ook 0,1 en 1 SOL als gevoeligheid): de instap vult 20 s na het signaal (ook
5/60/120 s) op de eerste trade daarna tegen de quote van de curve (1% fee en de prijsimpact van die grootte), de uitstap
volgt de regel (doel met halve verkoop, trailing stop onder de piek, tijdstop, harde stop −35%, bij graduatie, bij de eerste
dev-verkoop, uiterlijk na 24 uur) en vult ook 20 s na het moment waarop de regel afging, via `sell_quote` zolang er trades
zijn en na de graduatie op de pool (PumpSwap-fee 0,25% en de impact uit de liquiditeit van DexScreener; zonder liquiditeit
85 SOL aangenomen en zo gemarkeerd), met 0,0005 SOL priority fee per transactie.

**Begrippen in het onderzoeksrapport (`out/research/<dag>-<H>.md`).**

* **Hypothese** en **varianten**: vooraf vastgelegd in `fart/research/hypotheses.py` (H4, H5, H1, H3, H7, H2, H6; samen 145
  varianten). Het rapport herhaalt de hypothese zoals vastgelegd, vóór de cijfers.
* **Keuzehelft** en **toetshelft**: de dagen van het venster in twee helften (de eerste helft, bij een oneven aantal één dag
  meer). Op de keuzehelft wordt de variant gekozen met de hoogste verwachte waarde onder de varianten met minstens 20
  gevallen; alleen die variant wordt op de toetshelft beoordeeld. De andere varianten staan erbij om te zien, niet om uit te
  kiezen: wie op de toetshelft kiest, toetst niets.
* **Verwachte waarde na kosten**: het gemiddelde resultaat per positie in SOL (en als percentage van de inzet) over de
  gesloten gevallen, alle fees en impact meegerekend. Het **95%-interval** komt uit een blokbootstrap met de UTC-dagen als
  blokken (9.999 trekkingen, vaste seed), zodat dagen die bij elkaar horen bij elkaar blijven; dezelfde data geven altijd
  hetzelfde interval.
* **Trefkans**: het aandeel gesloten gevallen met een positief resultaat na kosten. **Mediaan, p10, p90**: de verdeling van het
  resultaat als percentage. **Maximale drawdown**: de grootste val van de som van de resultaten in tijdsvolgorde.
* **Onbekend**: het prijspad eindigde vóór een uitstap (de munt stierf, of de data houden op). **Geen vulling**: geen trade
  binnen 10 minuten na het signaal. Beide tellen apart en nooit als resultaat; het aandeel onbekend is een van de vier
  criteria.
* **Lead time**: de tijd van het signaal tot de hoogste prijs binnen 24 uur; de mediaan moet minstens 2 minuten zijn, anders
  was de beweging al voorbij toen een mens kon handelen.
* **Capaciteit**: hoeveel SOL de curve op het signaalmoment aankon met hoogstens 5% prijsimpact; de mediaan zegt hoeveel een
  regel écht kan dragen.
* **Conditionering**: de gekozen variant gesplitst op tijd van de dag (piekuren 13:00 tot 02:00 UTC), de SOL/USD-trend over
  de vorige 24 uur en de drukte (nieuwe munten in het uur van het signaal, in tercielen). **Gevoeligheid**: dezelfde variant met
  instapvertraging 5/20/60/120 s en grootte 0,1/0,5/1 SOL.
* **Multipliciteit**: het rapport zegt hoeveel van de getoetste varianten bij louter toeval een positieve ondergrens zouden
  tonen; daarom telt alleen de vooraf gekozen variant als toets.

**Bewezen** is een variant pas als op de toetshelft alle vier gelden: ondergrens van het interval van de verwachte waarde na
kosten boven 0, minstens 100 gevallen, aandeel onbekend onder 20%, mediane lead time minstens 2 minuten. Het oordeel staat
boven de tabellen als "Bewezen" of "niet bewezen"; "geen variant gekozen" betekent dat geen variant genoeg gevallen had op de
keuzehelft. Alleen een bewezen, vooraf gekozen variant kan met `fart research promote` een regel in `config/plays.json`
worden; de weigering noemt het criterium dat faalde.

**De Plays-laag en de pagina `/plays`.** Een bewezen regel wordt elke 15 s tegen de live stroom getoetst met dezelfde
kenmerken en hetzelfde rekenmodel als in het onderzoek. Een **play** is een papierpositie: signaal → open (gevuld) → gesloten
met een resultaat na kosten, of onbekend (24 uur zonder uitstap), of geen vulling (10 minuten zonder trade). Op `/plays` staan
per regel de papierposities sinds de promotie naast de cijfers uit het onderzoek (trefkans, verwachte waarde, interval,
gevallen); de trefkans "live" is die van de gesloten plays en kan afwijken van de gemeten: dat verschil is de meting van de
regel in de echte wereld. De Telegram-alert (hoogstens 6 per uur, alleen aan de eigenaar tot de go/no-go) herhaalt de gemeten
cijfers en de uitstapregel in gewone woorden en linkt naar onze Terminal, nooit naar een handelsplatform.

**Wat dit niet is.** Het harnas voorspelt niets en het platform handelt niet; de mens handelt via de knoppen van de Terminal
naar externe terminals, op eigen rekening. Een rapport met "niet bewezen" is een afgerond resultaat: de fabriek werkte, de
edge toonde zich niet in deze data. De regels van de engine zijn onveranderd (vingerafdruk `sha256:d45c41793c64`, de
bewezen gelijkwaardige van de meetweek); het onderzoek leest ze alleen.

## Mijlpaal 13: elk signaal meetbaar, en de tweede ronde

**Waarom.** De eerste echte onderzoeksrun (31 oktober 2026) liet 54 tot 97% van de uitkomsten onbekend, omdat het prijspad van
een munt ophield zodra de trade-stream haar losliet. Onbekend blijft onbekend (nooit interpoleren), dus moest het pad langer
worden, niet de definitie soepeler.

**Gevolgde munt.** Een munt die op het ruimste niveau een instapvoorwaarde van een hypothese haalt (minstens 10 unieke kopers in 5
minuten voor H1/H4/H5/H7, een thema-munt met trades voor H3, een curve van 60 SOL voor H2), een munt waarop een play-regel
vuurt of een munt met een observe-beslissing wordt 24 uur gevolgd. Haar prijspad komt uit de goedkoopste toereikende bron:
de trade-stream zolang die het abonnement heeft; daarbuiten en vóór graduatie **polls** van het curve-account (elke 60 s in de
eerste 2 uur, daarna elke 300 s): de curve heeft altijd een prijs, ook zonder trade; na graduatie de samples van de tracker en
de minuutcandles. In het rapport staat per variant op welke bron de instappen en uitstappen vulden (trade / poll / sample /
candle), en de gevoeligheidstabel heeft een rij "alleen trade-punten (zonder polls)": het verschil is wat de polls veranderen.
Een dev-verkoop is alleen op een trade-punt zichtbaar; een uitstap "bij de eerste dev-verkoop" vuurt dus nooit op een poll.
Het rapport splitst de signalen in vóór en ná de start van het volgen: oudere signalen krijgen geen pad met terugwerkende
kracht, en alleen de "ná"-helft zegt iets over het doel (onder 20% onbekend).

**Tracker.** De tracker samplet elk onderwerp van zijn venster elke 300 s (cap 2.000, nieuwste eerst, binnen een tijdsbudget);
`fart status` toont de duur per stap en het aandeel graduaties van de laatste 24 uur met een sample minstens elke 10 minuten
(de dekkingsregel van het dag- en weekrapport; doel ≥ 90%). Meer samples onder dezelfde regels is dezelfde methode (m1.0).

**Tweede ronde (na graduatie, vooraf vastgelegd op 31 oktober 2026).** **H8** Dev houdt vast na graduatie: instap N minuten na
graduatie (2/5/10) als de maker op de curve niet verkocht heeft, de prijs boven de graduatieprijs staat en het volume sinds de
graduatie minstens $10.000 (of $25.000) is; tegenproef: de maker verkocht wél. **H9** Terugval na graduatie: binnen 30 minuten
minstens 20/30/40% onder de piek sinds graduatie, met nog $2.500 volume in de laatste 5 minuten. **H10** Themaleider na
graduatie: de eerst gegradueerde munt van een brandend thema tegenover de kopieën, instap 2 minuten na graduatie. Samen 28
varianten (173 in totaal); elke variant telt in de multipliciteitsnoot. De prijs op een tick is het laatste bekende punt op of
vóór die tick; het volume sinds graduatie komt uit de candles (zonder candles uit de laatste sample; zonder beide geen
signaal); het 5-minutenvolume alleen uit candles. "De maker heeft niet verkocht" betekent: niet op de curve; de radar ziet geen
pool-trades, en de autopsie noemt een dev-verkoop na graduatie dan ook "niet gemeten".

**Wekelijks.** Elke maandag 07:00 UTC draait het harnas alle hypotheses over 30 dagen (of zoveel dagen als de database heeft; het
rapport zegt dat) en de autopsie. `/plays` toont per hypothese de gekozen variant, het oordeel, het onbekend-aandeel en de datum;
`/eigenaar` de volledige lijst. Niets hiervan verstuurt of promoveert iets: promotie blijft `fart research promote`, en die
weigert alles wat niet bewezen is.

