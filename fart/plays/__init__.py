"""The plays layer (milestone 12, B.6): the proven rules of config/plays.json, tested every tick on the live data by its own
daemon (`fart plays`, fart-plays.service) next to the frozen observation engine, never inside it. A play is a paper
position with the same execution model as the research harness; the outcome after costs is recorded in `plays` and shown
on /plays and in the Terminal. The platform never places an order."""
