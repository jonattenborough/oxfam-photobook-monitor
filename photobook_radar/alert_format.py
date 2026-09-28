"""Short Telegram lead summaries from facts actually present in the listing and canon."""
from __future__ import annotations

import re


def _short(value: object, limit: int = 160) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned[:limit - 1] + "…" if len(cleaned) > limit else cleaned


def format_lead(row, result: dict) -> tuple[str, str]:
    """Return headline and compact body; no unsupported market-value claims."""
    matches = result.get("matches") or []
    best = matches[0] if matches and int(matches[0].get("score") or 0) >= 80 else None
    sources = str(best.get("canon_sources") or "") if best else ""
    badges = []
    if "Parr/Badger" in sources:
        volume = re.search(r"Parr/Badger\s+V([1-3](?:/[1-3])*)", sources)
        badges.append("Parr/Badger V" + volume.group(1) if volume else "Parr/Badger")
    if "Roth 101" in sources:
        badges.append("Roth 101")
    tier = result.get("core_tier")
    if tier in {"1", "2", "3", 1, 2, 3}:
        badges.insert(0, f"Photographer Tier {tier}")
    icon = "💎 " if badges and best else "🔥 "
    title = _short(row["title"], 130)
    headline = icon + title
    price = f"£{row['price_minor'] / 100:.2f}" if row["price_minor"] is not None and row["currency"] == "GBP" else _short(row["currency"] or "Price unknown")
    postage = f" + £{row['shipping_minor'] / 100:.2f} postage" if row["shipping_minor"] is not None and row["shipping_currency"] == "GBP" else " + postage unknown"
    market = _short(row["platform"].capitalize(), 35)
    body = [f"💷 {price}{postage} · {market}"]
    if badges:
        body.append("📚 " + " · ".join(badges))
    elif result.get("photographer"):
        body.append("📷 " + _short(result["photographer"], 65) + " · outside the 175-name tiers")
    else:
        body.append("📷 Unfamiliar photobook candidate")
    if best and best.get("contributor"):
        context = _short(best.get("collector_profile") or best.get("documentary_relevance") or "", 130)
        if context and context != "UNKNOWN":
            body.append("Why it matters: " + context)
    if row["auction_end_at"]:
        body.append("⏰ Auction ends " + _short(row["auction_end_at"], 35))
    else:
        body.append("🛒 Fixed price; check seller page")
    body.append("⚠️ Live status and exact edition are not yet verified.")
    return headline, "\n".join(body)
