"""Validated local configuration. Production privileges default to off."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    if os.name == "posix" and __import__("sys").platform == "darwin":
        return Path.home() / "Library/Application Support/Photobook Radar"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share")) / "photobook-radar"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    mode: str = "shadow"
    bind_host: str = "127.0.0.1"
    port: int = 8765
    allow_marketplace_network: bool = False
    allow_real_notifications: bool = False
    allow_github_mutations: bool = False
    notification_enabled: bool = False
    notification_primary: str = "telegram"
    research_provider: str = "none"
    research_recurring_enabled: bool = False
    research_model: str = "gpt-6-sol"
    source_oxfam_photography: bool = False
    source_oxfam_broad: bool = False
    source_charity_shops: bool = False
    source_specialist_shops: bool = False
    source_ebay_broad: bool = False
    source_ebay_private: bool = False
    source_ebay_charity: bool = False
    source_ebay_endgame: bool = False
    source_abebooks: bool = False
    source_wider_web: bool = False
    source_publishers: bool = False
    source_prizes: bool = False
    max_recommended_item_gbp: str = "200.00"
    min_net_profit_gbp: str = "50.00"
    min_discount_pct: int = 40
    collector_min_discount_pct: int = 20
    ebay_daily_limit: int = 5000
    ebay_reserve: int = 25
    ebay_endgame_cap: int = 3600
    tick_seconds: int = 15

    @property
    def database(self) -> Path:
        return self.data_dir / "radar.db"

    @property
    def production(self) -> bool:
        return self.mode == "production"

    def validate(self) -> None:
        from decimal import Decimal
        if self.mode not in {"replay", "shadow", "production"}:
            raise ValueError("app.mode must be replay, shadow or production")
        if self.bind_host != "127.0.0.1":
            raise ValueError("Only loopback binding is supported until private access is configured")
        if not 1024 <= self.port <= 65535:
            raise ValueError("app.port must be between 1024 and 65535")
        if self.allow_github_mutations:
            raise ValueError("GitHub mutations are not implemented and must remain disabled")
        if self.mode != "production" and (self.allow_marketplace_network or self.allow_real_notifications):
            raise ValueError("Outbound scanning and notifications require production cutover")
        if self.notification_enabled and not self.allow_real_notifications:
            raise ValueError("Notification delivery needs production permission")
        if self.notification_primary not in {"telegram", "pushover"}:
            raise ValueError("notifications.primary must be telegram or pushover")
        if self.research_provider not in {"none", "codex_cli", "approved_api"}:
            raise ValueError("Unknown research provider")
        if self.research_recurring_enabled and self.research_provider != "codex_cli":
            raise ValueError("Recurring research requires the local Codex CLI provider")
        if self.research_model != "gpt-6-sol":
            raise ValueError("Research model must be the tested GPT-6 Sol configuration")
        max_buy = Decimal(self.max_recommended_item_gbp) if self.max_recommended_item_gbp != "unlimited" else Decimal("100000")
        minimum_profit = Decimal(self.min_net_profit_gbp)
        if not max_buy.is_finite() or not Decimal("0") < max_buy <= Decimal("100000"):
            raise ValueError("Invalid collector cash limit")
        if (not minimum_profit.is_finite() or not Decimal("0") <= minimum_profit <= Decimal("10000")
                or not 0 <= self.min_discount_pct <= 95 or not 0 <= self.collector_min_discount_pct <= 95):
            raise ValueError("Invalid bargain margin thresholds")
        if self.ebay_reserve < 25 or self.ebay_endgame_cap > 3600:
            raise ValueError("eBay reserve or Endgame cap is outside the supported range")
        if self.ebay_daily_limit < self.ebay_reserve or self.tick_seconds < 5:
            raise ValueError("Invalid quota or scheduler interval")


def load_config(path: Path | None = None) -> Config:
    data_dir = default_data_dir()
    selected = path or Path(os.getenv("PHOTOBOOK_RADAR_CONFIG", data_dir / "config.toml"))
    raw = tomllib.loads(selected.read_text()) if selected.exists() else {}
    app = raw.get("app", {})
    policy = raw.get("policy", {})
    ebay = raw.get("ebay", {})
    research = raw.get("research", {})
    sources = raw.get("sources", {})
    notifications = raw.get("notifications", {})
    scheduler = raw.get("scheduler", {})
    config = Config(
        data_dir=Path(app.get("data_dir", data_dir)).expanduser(),
        mode=str(app.get("mode", "shadow")),
        bind_host=str(app.get("bind_host", "127.0.0.1")),
        port=int(app.get("port", 8765)),
        allow_marketplace_network=bool(app.get("allow_marketplace_network", False)),
        allow_real_notifications=bool(app.get("allow_real_notifications", False)),
        allow_github_mutations=bool(app.get("allow_github_mutations", False)),
        notification_enabled=bool(notifications.get("enabled", False)),
        notification_primary=str(notifications.get("primary", "telegram")),
        research_provider=str(research.get("provider", "none")),
        research_recurring_enabled=bool(research.get("recurring_enabled", False)),
        research_model=str(research.get("model", "gpt-6-sol")),
        source_oxfam_photography=bool(sources.get("oxfam_photography", False)),
        source_oxfam_broad=bool(sources.get("oxfam_broad", False)),
        source_charity_shops=bool(sources.get("charity_shops", False)),
        source_specialist_shops=bool(sources.get("specialist_shops", False)),
        source_ebay_broad=bool(sources.get("ebay_broad", False)),
        source_ebay_private=bool(sources.get("ebay_private", False)),
        source_ebay_charity=bool(sources.get("ebay_charity", False)),
        source_ebay_endgame=bool(sources.get("ebay_endgame", False)),
        source_abebooks=bool(sources.get("abebooks", False)),
        source_wider_web=bool(sources.get("wider_web", False)),
        source_publishers=bool(sources.get("publishers", False)),
        source_prizes=bool(sources.get("prizes", False)),
        max_recommended_item_gbp=str(policy.get("max_recommended_item_gbp", "200.00")),
        min_net_profit_gbp=str(policy.get("min_net_profit_gbp", "50.00")),
        min_discount_pct=int(policy.get("min_discount_pct", 40)),
        collector_min_discount_pct=int(policy.get("collector_min_discount_pct", 20)),
        ebay_daily_limit=int(ebay.get("nominal_daily_browse_limit", 5000)),
        ebay_reserve=int(ebay.get("protected_reserve", 25)),
        ebay_endgame_cap=int(ebay.get("endgame_daily_cap", 3600)),
        tick_seconds=int(scheduler.get("tick_seconds", 15)),
    )
    config.validate()
    return config
