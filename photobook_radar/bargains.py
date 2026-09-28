"""Conservative, source-backed resale screen for collector phone alerts."""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .store import safe_url

MARKET_DOMAINS = {
    "ebay.co.uk", "ebay.com", "abebooks.co.uk", "abebooks.com", "biblio.com",
    "abebooks.de", "abaa.org", "pbfa.org", "vialibri.net", "liveauctioneers.com",
    "ha.com", "heritageauctions.com", "bonhams.com", "christies.com",
    "sothebys.com", "catawiki.com", "forumauctions.co.uk", "invaluable.com",
    "photobookstore.co.uk", "setantabooks.com", "fosterbooks.co.uk",
    "rrbphotobooks.com", "mackbooks.co.uk", "stanleybarker.co.uk",
}
SOLD_MARKER = re.compile(r"\b(?:this listing sold|sold on|sold for|sold price|winning bid|hammer price|realized price|realised price)\b", re.I)
STOP = {"the", "and", "book", "books", "photo", "photographs", "photography", "edition", "signed", "first"}
FEE_FRACTION = Decimal("0.20")
OUTBOUND_POSTAGE_GBP = Decimal("10")
UNKNOWN_INBOUND_POSTAGE_GBP = Decimal("20")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def check_comparable(url: str, title: str, amount_gbp: Decimal, kind: str, sold_date: str) -> bool:
    """Require an actual marketplace page showing this work and stated GBP price."""
    if safe_url(url) != url:
        return False
    host = urllib.parse.urlsplit(url).hostname or ""
    if not any(host == domain or host.endswith("." + domain) for domain in MARKET_DOMAINS):
        return False
    request = urllib.request.Request(url, headers={"User-Agent": "PhotobookRadar/1.0", "Accept": "text/html"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
            if response.status != 200 or "html" not in response.headers.get("Content-Type", "").lower():
                return False
            body = response.read(250_000).decode("utf-8", errors="replace")
    except Exception:
        return False
    visible = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).casefold()
    words = [word for word in re.findall(r"[a-z0-9]+", title.casefold()) if len(word) >= 4 and word not in STOP]
    if not words or sum(word in visible for word in words) < min(3, len(words)):
        return False
    prices = []
    for raw in re.findall(r"(?:£|gbp\s*)\s*([\d,]+(?:\.\d{2})?)", visible, flags=re.I):
        try:
            prices.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            pass
    if not any(abs(price - amount_gbp) <= max(Decimal("1"), amount_gbp * Decimal("0.03")) for price in prices):
        return False
    if kind == "SOLD" and (not SOLD_MARKER.search(visible) or sold_date[:4] not in visible):
        return False
    return True


def marketplace_identity(url: str) -> str:
    """Treat country sites of one marketplace as one source of price evidence."""
    host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    for group, domains in (("ebay", ("ebay.co.uk", "ebay.com")),
                           ("abebooks", ("abebooks.co.uk", "abebooks.com", "abebooks.de"))):
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return group
    return host


def assess_bargain(comparables: object, *, title: str, price_minor: int | None, currency: str | None,
                   shipping_minor: int | None, shipping_currency: str | None,
                   max_buy_gbp: Decimal, min_profit_gbp: Decimal | None, min_discount_pct: int,
                   checker=check_comparable, min_comparables: int = 2, require_sold: bool = True) -> dict:
    """Use the lowest verified like-for-like comp and conservative selling costs."""
    outcome = {"accepted": False, "reason": "insufficient comparable sales" if require_sold else "insufficient comparable listings", "comparables": []}
    if currency != "GBP" or price_minor is None or price_minor < 0 or not isinstance(comparables, list):
        outcome["reason"] = "GBP purchase price or comparables unavailable"
        return outcome
    postage = (Decimal(shipping_minor) / 100 if shipping_minor is not None and shipping_currency == "GBP"
               else UNKNOWN_INBOUND_POSTAGE_GBP)
    landed = Decimal(price_minor) / 100 + postage
    outcome["landed_gbp"] = landed
    outcome["postage_estimated"] = shipping_minor is None or shipping_currency != "GBP"
    if landed > max_buy_gbp:
        outcome["reason"] = "purchase exceeds collector cash limit"
        return outcome
    seen = set()
    for comp in comparables[:4]:
        if not isinstance(comp, dict) or comp.get("kind") not in {"SOLD", "ASKING"}:
            continue
        if comp.get("same_edition") is not True or comp.get("condition_no_better") is not True:
            continue
        url = comp.get("url")
        if not isinstance(url, str) or url in seen:
            continue
        seen.add(url)
        try:
            price = Decimal(str(comp.get("price_gbp")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not price.is_finite() or price <= 0 or price > Decimal("100000"):
            continue
        kind = comp["kind"]
        sold_date = str(comp.get("sold_date") or "")
        if kind == "SOLD":
            try:
                sale_day = date.fromisoformat(sold_date)
            except ValueError:
                continue
            if not ((date.today() - timedelta(days=1096)) <= sale_day <= date.today()):
                continue
        if checker(url, title, price, kind, sold_date):
            outcome["comparables"].append({"url": url, "kind": kind, "price_gbp": price,
                                            "marketplace": marketplace_identity(url),
                                            "sold_date": sold_date, "note": str(comp.get("note") or "")[:180]})
    checked = outcome["comparables"]
    if len(checked) < min_comparables or (require_sold and not any(comp["kind"] == "SOLD" for comp in checked)):
        return outcome
    floor = min(comp["price_gbp"] for comp in checked)
    discount = (Decimal(1) - landed / floor) * 100
    net_profit = floor * (Decimal(1) - FEE_FRACTION) - landed - OUTBOUND_POSTAGE_GBP
    outcome.update({"comp_floor_gbp": floor, "discount_pct": discount, "net_profit_gbp": net_profit})
    if discount < min_discount_pct or (min_profit_gbp is not None and net_profit < min_profit_gbp):
        outcome["reason"] = "margin below collector bargain threshold"
        return outcome
    outcome.update({"accepted": True, "reason": "verified bargain margin"})
    return outcome
