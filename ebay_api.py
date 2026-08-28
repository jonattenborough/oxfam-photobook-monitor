#!/usr/bin/env python3
"""Small authenticated client for eBay's production Browse API."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
API_SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_MARKETPLACE = "EBAY_GB"
LEGACY_ITEM_ID = re.compile(r"^v1\|([^|]+)\|")


class EbayApiError(RuntimeError):
    """Raised for eBay authentication, transport, or response errors."""


def configured() -> bool:
    """Return true only when both required GitHub Actions secrets are present."""
    return bool(os.getenv("EBAY_CLIENT_ID", "").strip() and os.getenv("EBAY_CLIENT_SECRET", "").strip())


def _error_detail(raw: bytes) -> str:
    """Extract a short safe API error without echoing credentials or tokens."""
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("error_description", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:240]
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        value = errors[0].get("message") or errors[0].get("longMessage")
        if isinstance(value, str):
            return value.strip()[:240]
    return ""


class EbayBrowseClient:
    """Production Browse API client with one cached application token per run."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        marketplace: str = DEFAULT_MARKETPLACE,
        timeout: int = 30,
    ) -> None:
        self.client_id = (client_id if client_id is not None else os.getenv("EBAY_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret if client_secret is not None else os.getenv("EBAY_CLIENT_SECRET", "")).strip()
        if not self.client_id or not self.client_secret:
            raise EbayApiError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are both required")
        self.marketplace = marketplace
        self.timeout = timeout
        self._access_token: str | None = None

    def _json_request(self, request: urllib.request.Request, label: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise EbayApiError(f"{label} returned a non-object JSON response")
                return payload
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc.read())
                message = f"{label} failed with HTTP {exc.code}"
                if detail:
                    message += f": {detail}"
                last_error = EbayApiError(message)
                if exc.code != 429 and exc.code < 500:
                    raise last_error from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = EbayApiError(f"{label} failed: {exc}")
            if attempt < 2:
                time.sleep(2**attempt)
        raise last_error or EbayApiError(f"{label} failed")

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": API_SCOPE}).encode("ascii")
        request = urllib.request.Request(
            TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        payload = self._json_request(request, "eBay OAuth")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise EbayApiError("eBay OAuth response did not contain an access token")
        self._access_token = token
        return token

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        category_ids: str | None = None,
        fixed_price_only: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "q": query,
            "limit": str(max(1, min(int(limit), 200))),
            "sort": "newlyListed",
            "fieldgroups": "EXTENDED",
        }
        if category_ids:
            params["category_ids"] = category_ids
        if fixed_price_only:
            params["filter"] = "buyingOptions:{FIXED_PRICE}"
        request = urllib.request.Request(
            SEARCH_URL + "?" + urllib.parse.urlencode(params),
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
                "Accept-Language": "en-GB",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                "User-Agent": "photobook-listing-monitor/1.0",
            },
        )
        payload = self._json_request(request, "eBay Browse search")
        rows = payload.get("itemSummaries", [])
        if not isinstance(rows, list):
            raise EbayApiError("eBay Browse search returned an invalid itemSummaries value")
        return [row for row in rows if isinstance(row, dict)]


def _legacy_id(item_id: str) -> str:
    match = LEGACY_ITEM_ID.match(item_id)
    return match.group(1) if match else item_id


def listing_from_summary(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    item_id = str(item.get("itemId") or "").strip()
    title = str(item.get("title") or "").strip()
    if not item_id or not title:
        return None
    legacy_id = _legacy_id(item_id)
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    try:
        price_gbp = round(float(price.get("value")), 2) if price.get("currency") == "GBP" else None
    except (TypeError, ValueError):
        price_gbp = None
    buying_options = item.get("buyingOptions") if isinstance(item.get("buyingOptions"), list) else []
    context_parts = [
        str(item.get("shortDescription") or ""),
        str(item.get("condition") or ""),
        str(item.get("itemCreationDate") or ""),
        " ".join(str(value) for value in buying_options),
    ]
    url = str(item.get("itemWebUrl") or "").strip()
    if not url and legacy_id.isdigit():
        url = f"https://www.ebay.co.uk/itm/{legacy_id}"
    return {
        "key": f"ebay:{legacy_id}",
        "external_id": legacy_id,
        "source_id": source["id"],
        "source_name": source["name"],
        "title": title[:350],
        "url": url,
        "price_gbp": price_gbp,
        "context": " | ".join(part.strip() for part in context_parts if part.strip())[:1800],
        "vendor": str(seller.get("username") or ""),
        "tags": " ".join(str(value) for value in buying_options),
    }


_DEFAULT_CLIENT: EbayBrowseClient | None = None


def search_listings(
    query: str,
    source: dict[str, Any],
    *,
    limit: int = 50,
    category_ids: str | None = None,
    fixed_price_only: bool = True,
) -> list[dict[str, Any]]:
    """Search newest-first and convert summaries to the monitor's row shape."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = EbayBrowseClient()
    rows = _DEFAULT_CLIENT.search(
        query,
        limit=limit,
        category_ids=category_ids,
        fixed_price_only=fixed_price_only,
    )
    converted = [listing_from_summary(row, source) for row in rows]
    return [row for row in converted if row is not None]

