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
from datetime import datetime, timezone
from typing import Any

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
API_SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_MARKETPLACE = "EBAY_GB"
LEGACY_ITEM_ID = re.compile(r"^v1\|([^|]+)\|")
ALLOWED_BUYING_OPTIONS = {"FIXED_PRICE", "AUCTION", "BEST_OFFER"}
ALLOWED_SELLER_ACCOUNT_TYPES = {"INDIVIDUAL", "BUSINESS"}
ALLOWED_CONDITIONS = {"NEW", "USED", "UNSPECIFIED"}


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


def _clean_enum(values: list[str] | tuple[str, ...] | set[str] | None, allowed: set[str], label: str) -> list[str]:
    cleaned: list[str] = []
    for raw in values or []:
        value = str(raw).strip().upper()
        if not value:
            continue
        if value not in allowed:
            raise ValueError(f"Unsupported {label}: {value}")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token()}",
            "Accept": "application/json",
            "Accept-Language": "en-GB",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "User-Agent": "photobook-listing-monitor/2.0",
        }

    def search(
        self,
        query: str | None = None,
        *,
        limit: int = 50,
        category_ids: str | None = None,
        fixed_price_only: bool = True,
        buying_options: list[str] | tuple[str, ...] | None = None,
        seller_ids: list[str] | tuple[str, ...] | None = None,
        exclude_seller_ids: list[str] | tuple[str, ...] | None = None,
        seller_account_type: str | None = None,
        delivery_country: str | None = None,
        item_start_date: str | None = None,
        item_end_date: str | None = None,
        ending_start_date: str | None = None,
        ending_end_date: str | None = None,
        search_in_description: bool = False,
        price_min: float | None = None,
        price_max: float | None = None,
        price_currency: str | None = None,
        conditions: list[str] | tuple[str, ...] | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "limit": str(max(1, min(int(limit), 200))),
            "sort": "newlyListed",
            "fieldgroups": "EXTENDED",
            "offset": str(max(0, min(int(offset), 9999))),
        }
        if query and query.strip():
            params["q"] = query.strip()[:100]
        if category_ids:
            params["category_ids"] = category_ids
        if "q" not in params and "category_ids" not in params:
            raise ValueError("An eBay search requires a query or category_ids")
        if search_in_description:
            if len(params.get("q", "")) < 2:
                raise ValueError("search_in_description requires a query of at least two characters")
            params["searchInDescription"] = "true"

        filters: list[str] = []
        requested_options = _clean_enum(buying_options, ALLOWED_BUYING_OPTIONS, "buying option")
        if requested_options:
            filters.append(f"buyingOptions:{{{'|'.join(requested_options)}}}")
        elif fixed_price_only:
            filters.append("buyingOptions:{FIXED_PRICE}")

        if seller_ids:
            cleaned_sellers = [str(value).strip() for value in seller_ids if str(value).strip()]
            if len(cleaned_sellers) > 250:
                raise ValueError("eBay supports at most 250 seller IDs per search")
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", seller) for seller in cleaned_sellers):
                raise ValueError("An eBay seller ID contains unsupported characters")
            if cleaned_sellers:
                filters.append(f"sellers:{{{'|'.join(cleaned_sellers)}}}")
        if exclude_seller_ids:
            cleaned_excluded = [str(value).strip() for value in exclude_seller_ids if str(value).strip()]
            if len(cleaned_excluded) > 250:
                raise ValueError("eBay supports at most 250 excluded seller IDs per search")
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", seller) for seller in cleaned_excluded):
                raise ValueError("An excluded eBay seller ID contains unsupported characters")
            if cleaned_excluded:
                filters.append(f"excludeSellers:{{{'|'.join(cleaned_excluded)}}}")

        if seller_account_type:
            account_type = str(seller_account_type).strip().upper()
            if account_type not in ALLOWED_SELLER_ACCOUNT_TYPES:
                raise ValueError(f"Unsupported seller account type: {account_type}")
            filters.append(f"sellerAccountTypes:{{{account_type}}}")

        if delivery_country:
            country = delivery_country.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError("delivery_country must be a two-letter country code")
            filters.append(f"deliveryCountry:{country}")
        if item_start_date or item_end_date:
            start = item_start_date.strip() if item_start_date else ""
            end = item_end_date.strip() if item_end_date else ""
            filters.append(f"itemStartDate:[{start}..{end}]")
        if ending_start_date or ending_end_date:
            start = ending_start_date.strip() if ending_start_date else ""
            end = ending_end_date.strip() if ending_end_date else ""
            filters.append(f"itemEndDate:[{start}..{end}]")

        if price_min is not None or price_max is not None:
            currency = str(price_currency or "GBP").strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("price_currency must be a three-letter currency code")
            if price_min is None:
                filters.append(f"price:[..{float(price_max):g}]")
            elif price_max is None:
                filters.append(f"price:[{float(price_min):g}..]")
            else:
                filters.append(f"price:[{float(price_min):g}..{float(price_max):g}]")
            filters.append(f"priceCurrency:{currency}")

        requested_conditions = _clean_enum(conditions, ALLOWED_CONDITIONS, "condition")
        if requested_conditions:
            filters.append(f"conditions:{{{'|'.join(requested_conditions)}}}")

        if filters:
            params["filter"] = ",".join(filters)
        request = urllib.request.Request(
            SEARCH_URL + "?" + urllib.parse.urlencode(params),
            headers=self._headers(),
        )
        payload = self._json_request(request, "eBay Browse search")
        rows = payload.get("itemSummaries", [])
        if not isinstance(rows, list):
            raise EbayApiError("eBay Browse search returned an invalid itemSummaries value")
        return [row for row in rows if isinstance(row, dict)]

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch the live Browse item record used immediately before alerting."""
        cleaned = str(item_id or "").strip()
        if not cleaned:
            raise ValueError("item_id is required")
        request = urllib.request.Request(
            ITEM_URL.format(item_id=urllib.parse.quote(cleaned, safe="")),
            headers=self._headers(),
        )
        return self._json_request(request, "eBay Browse item")

    def live_status(self, item_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Return whether a listing is currently available, plus the fetched item."""
        item = self.get_item(item_id)
        estimated = str(item.get("estimatedAvailabilityStatus") or "").upper()
        if estimated in {"OUT_OF_STOCK", "UNAVAILABLE"}:
            return False, estimated.lower().replace("_", " "), item
        ended = _parse_iso(item.get("itemEndDate"))
        if ended is not None and ended <= datetime.now(timezone.utc):
            return False, "listing ended", item
        buying_options = item.get("buyingOptions")
        if isinstance(buying_options, list) and not buying_options:
            return False, "no buying option returned", item
        return True, "live", item


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
    image = item.get("image") if isinstance(item.get("image"), dict) else {}
    category_path = item.get("categoryPath") or ""
    categories = item.get("categories") if isinstance(item.get("categories"), list) else []
    try:
        price_value = round(float(price.get("value")), 2)
    except (TypeError, ValueError):
        price_value = None
    price_currency = str(price.get("currency") or "").upper()
    price_gbp = price_value if price_currency == "GBP" else None
    buying_options = item.get("buyingOptions") if isinstance(item.get("buyingOptions"), list) else []
    context_parts = [
        str(item.get("shortDescription") or ""),
        str(item.get("condition") or ""),
        str(item.get("itemCreationDate") or ""),
        str(item.get("itemEndDate") or ""),
        " ".join(str(value) for value in buying_options),
        str(category_path),
    ]
    url = str(item.get("itemWebUrl") or "").strip()
    if not url and legacy_id.isdigit():
        domain = "www.ebay.com" if source.get("marketplace") == "EBAY_US" else "www.ebay.co.uk"
        url = f"https://{domain}/itm/{legacy_id}"
    category_id = ""
    if categories and isinstance(categories[0], dict):
        category_id = str(categories[0].get("categoryId") or "")
    return {
        "key": f"ebay:{legacy_id}",
        "external_id": legacy_id,
        "rest_item_id": item_id,
        "source_id": source["id"],
        "source_name": source["name"],
        "title": title[:350],
        "url": url,
        "price_gbp": price_gbp,
        "price_value": price_value,
        "price_currency": price_currency,
        "context": " | ".join(part.strip() for part in context_parts if part.strip())[:1800],
        "vendor": str(seller.get("username") or ""),
        "seller_feedback_percentage": seller.get("feedbackPercentage"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "seller_account_type": str(seller.get("sellerAccountType") or item.get("sellerAccountType") or ""),
        "tags": " ".join(str(value) for value in buying_options),
        "buying_options": [str(value) for value in buying_options],
        "condition": str(item.get("condition") or ""),
        "item_creation_date": str(item.get("itemCreationDate") or ""),
        "item_end_date": str(item.get("itemEndDate") or ""),
        "image_url": str(image.get("imageUrl") or ""),
        "category_id": category_id,
        "category_path": str(category_path),
    }


_DEFAULT_CLIENT: EbayBrowseClient | None = None


def search_listings(
    query: str | None,
    source: dict[str, Any],
    *,
    limit: int = 50,
    category_ids: str | None = None,
    fixed_price_only: bool = True,
    buying_options: list[str] | tuple[str, ...] | None = None,
    seller_ids: list[str] | tuple[str, ...] | None = None,
    exclude_seller_ids: list[str] | tuple[str, ...] | None = None,
    seller_account_type: str | None = None,
    delivery_country: str | None = None,
    item_start_date: str | None = None,
    item_end_date: str | None = None,
    ending_start_date: str | None = None,
    ending_end_date: str | None = None,
    search_in_description: bool = False,
    price_min: float | None = None,
    price_max: float | None = None,
    price_currency: str | None = None,
    conditions: list[str] | tuple[str, ...] | None = None,
    offset: int = 0,
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
        buying_options=buying_options,
        seller_ids=seller_ids,
        exclude_seller_ids=exclude_seller_ids,
        seller_account_type=seller_account_type,
        delivery_country=delivery_country,
        item_start_date=item_start_date,
        item_end_date=item_end_date,
        ending_start_date=ending_start_date,
        ending_end_date=ending_end_date,
        search_in_description=search_in_description,
        price_min=price_min,
        price_max=price_max,
        price_currency=price_currency,
        conditions=conditions,
        offset=offset,
    )
    converted = [listing_from_summary(row, source) for row in rows]
    return [row for row in converted if row is not None]
