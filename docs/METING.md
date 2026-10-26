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
liggen; anders missen we creates.

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
