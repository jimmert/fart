"""The website (milestone 8): `fart web`.

A standard-library HTTP server (`http.server.ThreadingHTTPServer`) that listens on the loopback only
(`FART_WEB_BIND`, default 127.0.0.1:8792) behind the VPS's shared Caddy (`deploy/caddy/fart.caddy`), shows the
operational data of fart in Dutch behind a login, and changes nothing: the database is opened read-only
(`mode=ro` plus `PRAGMA query_only`), the reports are read from `out/recall/`, and the only thing the service
writes is its own log (`logs/web.log`). No framework, no external script, font or tracker; one small CSS and one
small JS file of our own, served under `/static/` (the only paths that need no session: they carry no data).

Modules:

* `auth`     the owner account (scrypt hash in `.env`), signed session cookies (HMAC, 12 h, HttpOnly, Secure,
             SameSite=Strict), the CSRF token of the login form, and the login guard (5 failed attempts per 10 minutes
             per IP and per account, then 15 minutes of waiting; counted in memory and replayed from the log at start).
* `data`     the JSON of the API: status and freshness, coverage per hour, decisions with outcome status, the X
             state, the key points of the latest daily report and the go/no-go section of the latest week report.
             Honesty rules of the reports apply: unknown stays unknown, an hour or day NIET GEDEKT stays empty.
* `markdown` the safe Markdown subset that renders `out/recall/*.md` (headings, lists, tables, bold, code; no HTML,
             no links, no external content).
* `pages`    the server-rendered HTML (Dutch, Europe/Amsterdam with UTC in brackets, the banner).
* `server`   the request handler, the security headers, the request-size limit, the log hygiene, the daemon.
"""
