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
