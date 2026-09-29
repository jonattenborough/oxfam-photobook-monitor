"""Metered eBay transport; every physical request has a durable reservation.

The existing search parameter builder and response parser remain useful. This
subclass replaces their HTTP boundary and run-scoped token cache. A Browse
request is refused until a recent account-specific quota reading exists.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ebay_api import API_SCOPE, MARKETPLACE_DOMAINS, RATE_LIMIT_URL, TOKEN_URL, EbayApiError, EbayBrowseClient

from .config import Config
from .store import now, reserve_request, settle_request, stamp

_THREAD = threading.local()
_TOKEN_LOCK = threading.RLock()
_TOKEN_CACHE: tuple[str, str, str, datetime] | None = None
_QUOTA_LOCK = threading.Lock()
_QUOTA_CACHE: BrowseWindow | None = None


def load_credentials(config: Config) -> tuple[str, str]:
    path = config.data_dir / "secrets.toml"
    if path.stat().st_mode & 0o077:
        raise PermissionError("Private eBay credentials file is not owner-only")
    credentials = tomllib.loads(path.read_text()).get("ebay", {})
    client_id, client_secret = credentials.get("client_id"), credentials.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str) or not client_id or not client_secret:
        raise RuntimeError("Production eBay credentials are missing")
    return client_id, client_secret


@dataclass(frozen=True)
class BrowseWindow:
    start: str
    reset: str
    limit: int
    remaining: int
    measured_at: str

    @classmethod
    def from_analytics(cls, payload: dict[str, Any]) -> "BrowseWindow":
        reset = stamp(payload.get("reset"))
        duration = int(payload.get("time_window") or 0)
        limit = int(payload.get("limit") or 0)
        remaining = int(payload.get("remaining") if payload.get("remaining") is not None else -1)
        if not reset or duration <= 0 or limit <= 0 or remaining < 0 or remaining > limit:
            raise EbayApiError("Browse quota reading has no usable window")
        start = (datetime.fromisoformat(reset.replace("Z", "+00:00")) - timedelta(seconds=duration)).isoformat(timespec="seconds").replace("+00:00", "Z")
        return cls(start, reset, limit, remaining, now())

    def current(self) -> bool:
        return self.reset > now() and datetime.fromisoformat(self.measured_at.replace("Z", "+00:00")) > datetime.now(timezone.utc) - timedelta(minutes=30)


class MeteredEbayBrowseClient(EbayBrowseClient):
    """One physical attempt per method call; caller schedules later retries."""

    def __init__(self, db: sqlite3.Connection, config: Config, *, route_id: str, client_id: str, client_secret: str, marketplace: str = "EBAY_GB", timeout: int = 15) -> None:
        super().__init__(client_id, client_secret, marketplace=marketplace, timeout=timeout)
        self.db = db
        self.config = config
        self.route_id = route_id
        self.browse_window: BrowseWindow | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _day_window() -> tuple[str, str]:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.isoformat(timespec="seconds").replace("+00:00", "Z"), (midnight + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _reservation(self, request: urllib.request.Request, label: str) -> int:
        url = urllib.parse.urlsplit(request.full_url)
        if url.scheme != "https" or url.netloc != "api.ebay.com":
            raise ValueError("Only the production eBay API host is allowed")
        if not (self.config.production and self.config.allow_marketplace_network):
            raise RuntimeError("Live eBay requests are disabled")
        if url.path == urllib.parse.urlsplit(TOKEN_URL).path:
            start, reset = self._day_window()
            return reserve_request(self.db, bucket="ebay_oauth_client_credentials", window_start=start, reset_at=reset, provider_limit=1000, reserve=0, lane_cap=None, route_id="oauth", reason=label)
        if url.path == urllib.parse.urlsplit(RATE_LIMIT_URL).path:
            start, reset = self._day_window()
            return reserve_request(self.db, bucket="ebay_developer_analytics", window_start=start, reset_at=reset, provider_limit=5000, reserve=0, lane_cap=None, route_id="analytics", reason=label)
        if not url.path.startswith("/buy/browse/v1/"):
            raise ValueError("Unsupported eBay API route")
        window = self.browse_window
        if window is None or not window.current():
            raise RuntimeError("Current account-specific Browse quota is required")
        return reserve_request(
            self.db, bucket="ebay_browse", window_start=window.start, reset_at=window.reset,
            provider_limit=min(self.config.ebay_daily_limit, window.limit), reserve=self.config.ebay_reserve,
            lane_cap=self.config.ebay_endgame_cap if self.route_id.startswith(("endgame", "ebay-endgame:")) else None,
            route_id=self.route_id, reason=label, provider_remaining=window.remaining,
            provider_measured_at=window.measured_at,
        )

    def _json_request(self, request: urllib.request.Request, label: str) -> dict[str, Any]:
        request_id = self._reservation(request, label)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            settle_request(self.db, request_id, response_class=f"HTTP {exc.code}")
            if exc.code == 401:
                global _TOKEN_CACHE
                with _TOKEN_LOCK:
                    _TOKEN_CACHE = None
                self._access_token = None
            # Do not include a potentially sensitive URL, credential, or body.
            raise EbayApiError(f"{label} returned HTTP {exc.code}; retry must be scheduled as a new metered attempt") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            settle_request(self.db, request_id, response_class="transport-uncertain", uncertain=True)
            raise EbayApiError(f"{label} transport result unknown; attempt charged conservatively") from None
        settle_request(self.db, request_id, response_class=f"HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise EbayApiError(f"{label} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise EbayApiError(f"{label} returned a non-object JSON response")
        return payload

    def access_token(self) -> str:
        global _TOKEN_CACHE
        if self._access_token and datetime.now(timezone.utc) < self._token_expires_at:
            return self._access_token
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE
            if (cached and cached[0] == self.client_id and cached[1] == self.client_secret
                    and datetime.now(timezone.utc) < cached[3]):
                self._access_token, self._token_expires_at = cached[2], cached[3]
                return self._access_token
            self._access_token = None
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode("ascii")
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": API_SCOPE}).encode("ascii")
            request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
            result = self._json_request(request, "eBay OAuth")
            token = result.get("access_token")
            try:
                lifetime = int(result.get("expires_in"))
            except (ValueError, TypeError):
                lifetime = 0
            if not isinstance(token, str) or not token or lifetime <= 60:
                raise EbayApiError("eBay OAuth returned no usable access token or expiry")
            self._access_token = token
            self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lifetime - 60)
            _TOKEN_CACHE = (self.client_id, self.client_secret, token, self._token_expires_at)
            return token

    def refresh_browse_quota(self) -> BrowseWindow:
        window = BrowseWindow.from_analytics(self.browse_quota())
        self.browse_window = window
        return window


def thread_client(db: sqlite3.Connection, config: Config, route_id: str, *, marketplace: str = "EBAY_GB") -> MeteredEbayBrowseClient:
    """Share OAuth and one recent account quota reading across source threads."""
    global _QUOTA_CACHE
    if marketplace not in MARKETPLACE_DOMAINS:
        raise ValueError("Unsupported eBay marketplace")
    client_id, client_secret = load_credentials(config)
    client = getattr(_THREAD, "client", None)
    if client is None or client.client_id != client_id or client.client_secret != client_secret:
        client = MeteredEbayBrowseClient(db, config, route_id=route_id, client_id=client_id, client_secret=client_secret)
        _THREAD.client = client
    client.db = db
    client.config = config
    client.route_id = route_id
    client.marketplace = marketplace
    with _QUOTA_LOCK:
        window = _QUOTA_CACHE
        if window is None or datetime.fromisoformat(window.measured_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc) - timedelta(minutes=15):
            window = client.refresh_browse_quota()
            _QUOTA_CACHE = window
        client.browse_window = window
    return client
