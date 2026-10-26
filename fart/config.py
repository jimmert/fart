"""Settings and paths. Loads $FART_ROOT/.env at runtime; values are never logged.

Secrets (RPC URLs with keys, PumpPortal API key, X bearer token, Telegram bot token) live only in
`$FART_ROOT/.env` (names in `.env.example`). `Settings.describe()` is the only thing that may be
logged or printed: it carries hosts and configured/not-configured flags, never values.
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
)


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
    telegram_bot_token: str = ""
    telegram_owner_chat_id: str = ""
    telegram_notify_enabled: bool = False
    telegram_community_chat_id: str = ""
    telegram_community_enabled: bool = False
    telegram_community_preview: bool = False
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
            "telegram": "configured" if self.telegram_configured else "not configured",
            "telegram_notify_enabled": self.telegram_notify_enabled,
            "telegram_community": "configured" if self.community_configured else "not configured",
            "telegram_community_enabled": self.community_enabled,
            "telegram_community_preview": self.community_preview,
        }


CONFIG_DIR = REPO_ROOT / "config"
CONFIG_FILES = ("pumpfun", "budget", "engine", "measure", "stopwords", "ingest", "x", "x_kols", "notify")


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
        telegram_bot_token=merged.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_owner_chat_id=merged.get("TELEGRAM_OWNER_CHAT_ID", "").strip(),
        telegram_notify_enabled=_flag(merged.get("TELEGRAM_NOTIFY_ENABLED")),
        telegram_community_chat_id=merged.get("TELEGRAM_COMMUNITY_CHAT_ID", "").strip(),
        telegram_community_enabled=_flag(merged.get("TELEGRAM_COMMUNITY_ENABLED")),
        telegram_community_preview=_flag(merged.get("TELEGRAM_COMMUNITY_PREVIEW")),
        configs=load_configs(),
    )
