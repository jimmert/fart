"""Settings and paths. Loads $FART_ROOT/.env at runtime; values are never logged.

Secrets (RPC URLs with keys, PumpPortal API key, X bearer token, Telegram bot token, the website's password
hash and cookie secret) live only in `$FART_ROOT/.env` (names in `.env.example`). `Settings.describe()` is the
only thing that may be logged or printed: it carries hosts and configured/not-configured flags, never values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .util import load_json
from .version import REPO_ROOT

PUBLIC_RPC_HTTP = "https://api.mainnet-beta.solana.com"
PUBLIC_RPC_WSS = "wss://api.mainnet-beta.solana.com"
DEFAULT_PUMPPORTAL_WSS = "wss://pumpportal.fun/api/data"

ENV_KEYS = (
    "RPC_HTTP_URL", "RPC_WSS_URL", "RPC_DAILY_BUDGET_CU", "PUMPPORTAL_WSS_URL", "FART_ROOT",
    # milestone 2
    "PUMPPORTAL_API_KEY", "X_BEARER_TOKEN", "X_DAILY_BUDGET_USD",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_NOTIFY_ENABLED",
    # milestone 5: the community channel, off by default (both must be set explicitly to send anywhere else)
    "TELEGRAM_COMMUNITY_CHAT_ID", "TELEGRAM_COMMUNITY_ENABLED", "TELEGRAM_COMMUNITY_PREVIEW",
    # milestone 6: the X mode (off | shadow | live); unset = off without a token, shadow with one
    "X_MODE",
    # milestone 8: the website (fart web): loopback bind, the owner's scrypt password hash, the cookie-signing secret
    "FART_WEB_BIND", "FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET",
    # milestone 9: the public section of the website (1 = the dashboard, observations, reports, method and tools pages
    # and /api/public/* need no login; 0 = everything behind the login as in milestone 8)
    "FART_WEB_PUBLIC",
)

DEFAULT_WEB_BIND = "127.0.0.1:8792"

X_MODES = ("off", "shadow", "live")


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no shell expansion). Missing file -> {}."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def redact_url(url: str | None) -> str:
    """Host only; the path or query of an RPC/PumpPortal URL usually carries the API key."""
    if not url:
        return "<none>"
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname}"
    except ValueError:
        return "<unparseable>"


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    root: Path
    rpc_http_url: str
    rpc_wss_url: str
    rpc_is_public: bool
    rpc_daily_budget_cu: int
    pumpportal_wss_url: str
    pumpportal_api_key: str = ""
    x_bearer_token: str = ""
    x_daily_budget_usd: float = 3.0
    x_mode_requested: str = ""
    telegram_bot_token: str = ""
    telegram_owner_chat_id: str = ""
    telegram_notify_enabled: bool = False
    telegram_community_chat_id: str = ""
    telegram_community_enabled: bool = False
    telegram_community_preview: bool = False
    web_bind: str = DEFAULT_WEB_BIND
    web_password_hash: str = ""
    web_secret: str = ""
    web_public: bool = True
    configs: dict[str, Any] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fart.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "budget.lock"

    # ----- milestone 2 derived settings -----------------------------------------------------------
    @property
    def pumpportal_trades_enabled(self) -> bool:
        """PumpPortal only serves subscribeTokenTrade to funded API keys; without a key we subscribe
        to creates and migrations only and take trades from the RPC."""
        return bool(self.pumpportal_api_key)

    @property
    def pumpportal_connect_url(self) -> str:
        """The URL actually used for the connection. Never log it: the key rides in the query."""
        if not self.pumpportal_api_key:
            return self.pumpportal_wss_url
        sep = "&" if "?" in self.pumpportal_wss_url else "?"
        return f"{self.pumpportal_wss_url}{sep}api-key={self.pumpportal_api_key}"

    @property
    def x_configured(self) -> bool:
        return bool(self.x_bearer_token)

    # ----- milestone 6: the X mode ---------------------------------------------------------------
    @property
    def x_live_allowed(self) -> bool:
        """`live` is a rules change: X data may only reach decisions when config/engine.json carries
        `"x_live": true`, which changes the rules fingerprint (a new segment in the week report). The key
        is absent during the measurement week, so `live` cannot be reached from `.env` alone."""
        return (self.configs.get("engine") or {}).get("x_live") is True

    @property
    def x_mode(self) -> str:
        """The effective X mode.

        off     no bearer token, or X_MODE=off: fart-social idles with a heartbeat (as before milestone 6).
        shadow  a token and X_MODE unset or `shadow`: the whole X watch runs within its budget, but nothing of
                it reaches `decisions`: fast posts land in `shadow_decisions`, confirmation counts in
                `shadow_confirmations`, the engine gets no mega-post feature and neutral X features.
        live    X_MODE=live AND `x_live` true in config/engine.json: the milestone-2 behaviour. Without the
                config key a requested `live` runs as shadow and says so (`x_mode_note`, `fart status`).
        """
        if not self.x_configured:
            return "off"
        req = self.x_mode_requested
        if req == "off":
            return "off"
        if req == "live" and self.x_live_allowed:
            return "live"
        return "shadow"

    @property
    def x_mode_note(self) -> str | None:
        """Why the effective mode differs from X_MODE, for `fart status` and the daemon logs; None when it does not."""
        req = self.x_mode_requested
        if req and req not in X_MODES:
            return f"X_MODE={req!r} is not one of {'/'.join(X_MODES)}: running {self.x_mode}"
        if req == "live" and not self.x_configured:
            return "X_MODE=live without X_BEARER_TOKEN: off"
        if req == "live" and not self.x_live_allowed:
            return "X_MODE=live refused: config/engine.json has no \"x_live\": true (a rules change, after the measurement week); running shadow"
        if req == "shadow" and not self.x_configured:
            return "X_MODE=shadow without X_BEARER_TOKEN: off"
        return None

    @property
    def x_active(self) -> bool:
        return self.x_mode != "off"

    @property
    def x_shadow(self) -> bool:
        return self.x_mode == "shadow"

    @property
    def x_live(self) -> bool:
        return self.x_mode == "live"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_owner_chat_id)

    @property
    def telegram_enabled(self) -> bool:
        return self.telegram_notify_enabled and self.telegram_configured

    # ----- milestone 5: the community channel ----------------------------------------------------
    @property
    def community_configured(self) -> bool:
        """A community chat id exists and differs from the owner's."""
        return bool(self.telegram_community_chat_id) and self.telegram_community_chat_id != self.telegram_owner_chat_id

    @property
    def community_enabled(self) -> bool:
        """Only with TELEGRAM_COMMUNITY_CHAT_ID set AND TELEGRAM_COMMUNITY_ENABLED=1 AND the owner channel
        enabled may anything be sent to another chat than the owner's. Default: off."""
        return self.telegram_enabled and self.telegram_community_enabled and self.community_configured

    @property
    def community_preview(self) -> bool:
        """TELEGRAM_COMMUNITY_PREVIEW=1: the community text goes to the OWNER as an extra message."""
        return self.telegram_enabled and self.telegram_community_preview

    # ----- milestone 8: the website -------------------------------------------------------------
    @property
    def web_configured(self) -> bool:
        """Both `.env` values the website needs exist; `fart web` refuses to start without them (and checks their shape)."""
        return bool(self.web_password_hash) and bool(self.web_secret)

    # ----- milestone 9: the public website -----------------------------------------------------
    @property
    def web_site(self) -> dict[str, Any]:
        """`config/web.json`: the brand (site name, product word, tagline, domain, accent colour) and the two public
        limits. Not a rule file. Missing keys fall back to the defaults below so an edited file never breaks a page."""
        cfg = dict(self.configs.get("web") or {})
        out = dict(WEB_DEFAULTS)
        for k in WEB_DEFAULTS:
            v = cfg.get(k)
            if v is not None and v != "":
                out[k] = v
        return out

    def config(self, name: str) -> Any:
        return self.configs[name]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.out_dir, self.logs_dir, self.raw_dir, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, Any]:
        """Safe to log/print: no secret values, only hosts and configured/not-configured flags."""
        return {
            "root": str(self.root),
            "rpc_http": redact_url(self.rpc_http_url),
            "rpc_wss": redact_url(self.rpc_wss_url),
            "rpc_is_public": self.rpc_is_public,
            "rpc_daily_budget_cu": self.rpc_daily_budget_cu,
            "pumpportal": redact_url(self.pumpportal_wss_url),
            "pumpportal_api_key": "configured" if self.pumpportal_api_key else "not configured",
            "pumpportal_trades": "pumpportal (api key)" if self.pumpportal_trades_enabled else "rpc only (no key)",
            "x_bearer_token": "configured" if self.x_configured else "not configured",
            "x_daily_budget_usd": self.x_daily_budget_usd,
            "x_mode": self.x_mode,
            "x_mode_requested": self.x_mode_requested or "unset",
            "x_mode_note": self.x_mode_note,
            "x_live_allowed": self.x_live_allowed,
            "telegram": "configured" if self.telegram_configured else "not configured",
            "telegram_notify_enabled": self.telegram_notify_enabled,
            "telegram_community": "configured" if self.community_configured else "not configured",
            "telegram_community_enabled": self.community_enabled,
            "telegram_community_preview": self.community_preview,
            "web_bind": self.web_bind,
            "web_password_hash": "configured" if self.web_password_hash else "not configured",
            "web_secret": "configured" if self.web_secret else "not configured",
            "web_public": self.web_public,
        }


CONFIG_DIR = REPO_ROOT / "config"
CONFIG_FILES = ("pumpfun", "budget", "engine", "measure", "stopwords", "ingest", "x", "x_kols", "notify", "web", "plays")

# milestone 9: the brand of the public website (config/web.json overrides these; see Settings.web_site)
WEB_DEFAULTS: dict[str, Any] = {
    "site_name": "KingDegen",
    "product": "radar",
    "tagline": "Een radar voor nieuwe Solana-memecoins. Observatie, geen advies. Alles wordt achteraf gemeten.",
    "domain": "kingdegen.dev",
    "accent": "#F2B33D",
    "public_observations_limit": 500,
    "refresh_s": 30,
    # milestone 12: the Terminal (config/web.json -> terminals, terminal); the defaults are the documented link patterns; milestone 13:
    # Photon and GMGN were confirmed in a browser on 31 October 2026, Axiom (bot check) and BullX (Telegram login) were not
    "terminals": [
        {"name": "Axiom", "url": "https://axiom.trade/t/{mint}", "verified": False},
        {"name": "Photon", "url": "https://photon-sol.tinyastro.io/en/lp/{mint}", "verified": True},
        {"name": "BullX", "url": "https://neo.bullx.io/terminal?chainId=1399811149&address={mint}", "verified": False},
        {"name": "GMGN", "url": "https://gmgn.ai/sol/token/{mint}", "verified": True},
    ],
    "terminal": {"creates_window_s": 1800, "max_rows": 800, "stream_heartbeat_s": 15, "stream_max_connections": 64, "follow_max": 50, "stream_poll_s": 1.0},
}


def load_configs(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return {name: load_json(config_dir / f"{name}.json") for name in CONFIG_FILES}


def load_settings(root: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Settings:
    """Resolve root -> read $root/.env -> overlay process env (process env wins)."""
    penv = dict(os.environ if env is None else env)
    root_s = root or penv.get("FART_ROOT") or str(REPO_ROOT)
    root_p = Path(root_s).expanduser().resolve()
    fenv = parse_env_file(root_p / ".env")
    merged = {**fenv, **{k: v for k, v in penv.items() if k in ENV_KEYS and v}}
    http = merged.get("RPC_HTTP_URL", "").strip()
    wss = merged.get("RPC_WSS_URL", "").strip()
    is_public = not http
    try:
        cu = int(merged.get("RPC_DAILY_BUDGET_CU", "") or 3_000_000)
    except ValueError:
        cu = 3_000_000
    try:
        x_usd = float(merged.get("X_DAILY_BUDGET_USD", "") or 3.0)
    except ValueError:
        x_usd = 3.0
    return Settings(
        root=root_p,
        rpc_http_url=http or PUBLIC_RPC_HTTP,
        rpc_wss_url=wss or PUBLIC_RPC_WSS,
        rpc_is_public=is_public,
        rpc_daily_budget_cu=cu,
        pumpportal_wss_url=merged.get("PUMPPORTAL_WSS_URL", "").strip() or DEFAULT_PUMPPORTAL_WSS,
        pumpportal_api_key=merged.get("PUMPPORTAL_API_KEY", "").strip(),
        x_bearer_token=merged.get("X_BEARER_TOKEN", "").strip(),
        x_daily_budget_usd=x_usd,
        x_mode_requested=merged.get("X_MODE", "").strip().lower(),
        telegram_bot_token=merged.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_owner_chat_id=merged.get("TELEGRAM_OWNER_CHAT_ID", "").strip(),
        telegram_notify_enabled=_flag(merged.get("TELEGRAM_NOTIFY_ENABLED")),
        telegram_community_chat_id=merged.get("TELEGRAM_COMMUNITY_CHAT_ID", "").strip(),
        telegram_community_enabled=_flag(merged.get("TELEGRAM_COMMUNITY_ENABLED")),
        telegram_community_preview=_flag(merged.get("TELEGRAM_COMMUNITY_PREVIEW")),
        web_bind=merged.get("FART_WEB_BIND", "").strip() or DEFAULT_WEB_BIND,
        web_password_hash=merged.get("FART_WEB_PASSWORD_HASH", "").strip(),
        web_secret=merged.get("FART_WEB_SECRET", "").strip(),
        web_public=_flag(merged.get("FART_WEB_PUBLIC", "1") or "1"),
        configs=load_configs(),
    )
