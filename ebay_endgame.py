#!/usr/bin/env python3
"""Recall-first eBay Endgame Auction Radar.

The radar discovers auctions early, stores a persistent candidate pool, and
uses local deadlines for the final alerts. It deliberately searches every
seller type and does not apply a delivery-country filter during discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ebay_api
import ebay_search_checkpoint as checkpoint_search
import ebay_core_targets as core_targets
import ebay_private_seller_monitor as legacy
import ebay_private_recall_monitor as recall
import photobook_recognition as recognition
import photobook_target_books as target_books
import pre1970_unicorn_targets as unicorn_targets

DEFAULT_CONFIG = Path("data/ebay_endgame_targets.json")
DEFAULT_STATE = Path("data/ebay_endgame_state.json")
DEFAULT_RUNTIME = Path("runtime/ebay-endgame")
EXPECTED_TIER_COUNTS = {"1": 50, "2": 70, "3": 55}
EXPECTED_MARKETS = {
    "EBAY_AT", "EBAY_AU", "EBAY_BE", "EBAY_CA", "EBAY_CH", "EBAY_DE",
    "EBAY_ES", "EBAY_FR", "EBAY_GB", "EBAY_HK", "EBAY_IE", "EBAY_IT",
    "EBAY_NL", "EBAY_PL", "EBAY_SG", "EBAY_US",
}
PHOTO_EVIDENCE = {
    "photobook", "photo book", "photography book", "photographic book",
    "photography monograph", "photographs by", "photography by",
    "foto", "fotobuch", "fotoboek", "fotoksiążka", "fotolibro",
    "livre photo", "libro fotografico", "libro fotografia",
}
COLLECTIBLE_EVIDENCE = {
    "signed", "inscribed", "autograph", "limited edition", "numbered",
    "first edition", "first printing", "first impression", "with print",
    "original print", "slipcase", "artist proof", "job lot", "collection",
    "signiert", "signé", "firmato", "firmado", "gesigneerd", "podpisana",
}


def utc_stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_stamp(value: Any) -> datetime | None:
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


def normalized(value: Any) -> str:
    return core_targets.normalized(value)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def set_output(name: str, value: Any) -> None:
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def validate_config(config: dict[str, Any]) -> None:
    tiers = config.get("tiers")
    if not isinstance(tiers, dict):
        raise ValueError("Endgame config requires a tiers object")
    all_names: list[str] = []
    for tier, expected in EXPECTED_TIER_COUNTS.items():
        names = tiers.get(tier, {}).get("names") if isinstance(tiers.get(tier), dict) else None
        if not isinstance(names, list) or len(names) != expected:
            raise ValueError(f"Tier {tier} must contain exactly {expected} photographers")
        all_names.extend(str(name).strip() for name in names)
    identities = [normalized(name) for name in all_names]
    if len(all_names) != 175 or len(set(identities)) != 175:
        raise ValueError("Endgame requires 175 unique photographers across the three tiers")

    markets = config.get("markets")
    if not isinstance(markets, list):
        raise ValueError("Endgame config requires a markets list")
    market_ids = {str(row.get("marketplace") or "") for row in markets if isinstance(row, dict)}
    if market_ids != EXPECTED_MARKETS:
        missing = sorted(EXPECTED_MARKETS - market_ids)
        extra = sorted(market_ids - EXPECTED_MARKETS)
        raise ValueError(f"Endgame marketplace set is invalid; missing={missing}, extra={extra}")
    query_sets = config.get("query_sets")
    if not isinstance(query_sets, dict):
        raise ValueError("Endgame config requires query_sets")
    for row in markets:
        if row.get("query_set") not in query_sets:
            raise ValueError(f"Unknown query set for {row.get('marketplace')}")
    query_limit = int(config.get("query_character_limit") or 0)
    if query_limit < 20 or query_limit > 100:
        raise ValueError("query_character_limit must be between 20 and 100")
    if int(config.get("daily_call_cap") or 0) > 3600:
        raise ValueError("Endgame daily_call_cap cannot exceed its 3,600-call allocation")

    unicorn = config.get("unicorn_search") or {}
    if unicorn.get("enabled"):
        target_path = Path(str(unicorn.get("targets_path") or unicorn_targets.DEFAULT_PATH))
        tiers = {str(value).upper() for value in unicorn.get("tiers") or ["A"]}
        selected = unicorn_targets.targets_for_tiers(tiers, target_path)
        if not selected:
            raise ValueError("Enabled unicorn_search must select at least one target")
        if float(unicorn.get("revisit_hours") or 0) <= 0:
            raise ValueError("unicorn_search.revisit_hours must be positive")
        if float(unicorn.get("horizon_hours") or 0) <= 0:
            raise ValueError("unicorn_search.horizon_hours must be positive")


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = load_json(path)
    validate_config(config)
    return config


def blank_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_discovery_at": "",
        "schedule": {},
        "candidates": {},
        "usage": {"date": "", "calls": 0},
        "stats": {},
    }


def load_state(path: Path = DEFAULT_STATE) -> dict[str, Any]:
    state = blank_state()
    if path.exists():
        saved = load_json(path)
        state.update(saved)
    for key in ("schedule", "candidates", "stats"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("usage"), dict):
        state["usage"] = {"date": "", "calls": 0}
    return state


def reset_daily_usage(state: dict[str, Any], now: datetime) -> None:
    day = now.astimezone(timezone.utc).date().isoformat()
    if str(state["usage"].get("date") or "") != day:
        state["usage"] = {"date": day, "calls": 0}


def compile_or_queries(terms: list[str], character_limit: int) -> list[dict[str, Any]]:
    """Pack exact alternatives into eBay's ``(one,two)`` OR syntax."""
    return core_targets.compile_or_queries(terms, character_limit)


def photographer_terms(config: dict[str, Any], tier: str) -> list[str]:
    return core_targets.photographer_terms(config, tier)


def title_terms(config: dict[str, Any]) -> list[str]:
    settings = config.get("title_search") or {}
    if not settings.get("enabled"):
        return []
    tier = str(settings.get("tier") or "1")
    limit = max(1, int(settings.get("records_per_photographer") or 1))
    wanted = {normalized(name): name for name in config["tiers"][tier]["names"]}
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for person in wanted:
        owner = target_books.registry().get(person)
        for row in owner["books"] if owner else []:
            title = " ".join(str(row.get("Title") or "").split())
            if len(recognition.pb.useful_tokens(title)) >= 2 and len(normalized(title)) >= 12:
                by_person[person].append(row)

    result: list[str] = []
    for person in wanted:
        rows = sorted(
            by_person.get(person, []),
            key=lambda row: (
                int(str(row.get("Search priority") or "9"))
                if str(row.get("Search priority") or "").isdigit()
                else 9,
                0 if str(row.get("Collectibility tier") or "").upper() == "S" else 1,
                len(str(row.get("Title") or "")),
            ),
        )
        chosen: set[str] = set()
        for row in rows:
            title = " ".join(str(row.get("Title") or "").split())
            identity = normalized(title)
            if not identity or identity in chosen:
                continue
            chosen.add(identity)
            result.append(title)
            if len(chosen) >= limit:
                break
    return result


def task_identity(lane: str, marketplace: str, query: str | None, tier: str = "") -> str:
    raw = "|".join((lane, marketplace, tier, query or "category-only"))
    return f"{lane}:{marketplace}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:14]}"


def build_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(config["query_character_limit"])
    markets = list(config["markets"])
    tasks: list[dict[str, Any]] = []

    for tier in ("1", "2", "3"):
        settings = config["tiers"][tier]
        groups = compile_or_queries(photographer_terms(config, tier), limit)
        for market in markets:
            major = bool(market.get("major"))
            interval = float(settings["major_revisit_hours"] if major else settings["revisit_hours"])
            for group in groups:
                query = group["query"]
                marketplace = market["marketplace"]
                tasks.append(
                    {
                        "key": task_identity("known", marketplace, query, tier),
                        "lane": "known",
                        "marketplace": marketplace,
                        "query": query,
                        "terms": list(group["terms"]),
                        "tier": tier,
                        "interval_hours": interval,
                        "horizon_hours": float(settings["horizon_hours"]),
                        "category_ids": None,
                        "search_in_description": True,
                    }
                )

    broad = config["broad_search"]
    query_sets = config["query_sets"]
    for market in markets:
        major = bool(market.get("major"))
        interval = float(broad["major_revisit_hours"] if major else broad["revisit_hours"])
        horizon = float(broad["major_horizon_hours"] if major else broad["horizon_hours"])
        for query in query_sets[market["query_set"]]:
            marketplace = market["marketplace"]
            tasks.append(
                {
                    "key": task_identity("broad", marketplace, query),
                    "lane": "broad",
                    "marketplace": marketplace,
                    "query": query,
                    "terms": [query],
                    "tier": "",
                    "interval_hours": interval,
                    "horizon_hours": horizon,
                    "category_ids": None,
                    "search_in_description": True,
                }
            )

    unicorn_settings = config.get("unicorn_search") or {}
    if unicorn_settings.get("enabled"):
        unicorn_rows = unicorn_targets.targets_for_tiers(
            {str(value).upper() for value in unicorn_settings.get("tiers") or ["A"]},
            Path(str(unicorn_settings.get("targets_path") or unicorn_targets.DEFAULT_PATH)),
        )
        for market in markets:
            if not unicorn_settings.get("all_markets", True) and not market.get("major"):
                continue
            marketplace = market["marketplace"]
            for target in unicorn_rows:
                query = unicorn_targets.search_query(target)
                tasks.append(
                    {
                        "key": task_identity("unicorn", marketplace, query, target["Unicorn tier"]),
                        "lane": "unicorn",
                        "marketplace": marketplace,
                        "query": query,
                        "terms": unicorn_targets.visible_terms(target),
                        "tier": "",
                        "unicorn_tier": target["Unicorn tier"],
                        "unicorn_target": {
                            "Contributor": target["Contributor"],
                            "Contributor aliases": target.get("Contributor aliases", ""),
                            "Title": target["Title"],
                            "Title aliases": target.get("Title aliases", ""),
                            "Year": target["Year"],
                            "Radar notes": target.get("Radar notes", ""),
                        },
                        "interval_hours": float(unicorn_settings["revisit_hours"]),
                        "horizon_hours": float(unicorn_settings["horizon_hours"]),
                        "category_ids": None,
                        "search_in_description": True,
                    }
                )

    title_settings = config.get("title_search") or {}
    if title_settings.get("enabled"):
        groups = compile_or_queries(title_terms(config), limit)
        for market in markets:
            if title_settings.get("major_markets_only") and not market.get("major"):
                continue
            for group in groups:
                query = group["query"]
                marketplace = market["marketplace"]
                tasks.append(
                    {
                        "key": task_identity("title", marketplace, query, "1"),
                        "lane": "title",
                        "marketplace": marketplace,
                        "query": query,
                        "terms": list(group["terms"]),
                        "tier": "1",
                        "interval_hours": float(title_settings["revisit_hours"]),
                        "horizon_hours": float(title_settings["horizon_hours"]),
                        "category_ids": str(config["book_category_ids"]),
                        "search_in_description": True,
                    }
                )

    category = config.get("category_sweep") or {}
    if category.get("enabled"):
        for market in markets:
            if market.get("category_sweep") is False:
                continue
            major = bool(market.get("major"))
            interval = float(category["major_revisit_hours"] if major else category["revisit_hours"])
            horizon = float(category["major_horizon_hours"] if major else category["horizon_hours"])
            marketplace = market["marketplace"]
            tasks.append(
                {
                    "key": task_identity("category", marketplace, None),
                    "lane": "category",
                    "marketplace": marketplace,
                    "query": None,
                    "terms": [],
                    "tier": "",
                    "interval_hours": interval,
                    "horizon_hours": horizon,
                    "category_ids": str(config["book_category_ids"]),
                    "search_in_description": False,
                }
            )
    return tasks


def projected_primary_calls_per_day(tasks: list[dict[str, Any]]) -> float:
    return sum(24.0 / float(task["interval_hours"]) for task in tasks)


def task_is_due(task: dict[str, Any], schedule: dict[str, Any], now: datetime) -> bool:
    last = parse_stamp(schedule.get(task["key"]))
    if last is None:
        return True
    return now - last >= timedelta(hours=float(task["interval_hours"]))


def _due_sort_key(task: dict[str, Any], schedule: dict[str, Any], now: datetime) -> tuple[Any, ...]:
    last = parse_stamp(schedule.get(task["key"]))
    missing = last is None
    overdue = 10**9 if missing else (now - last).total_seconds() / (3600 * float(task["interval_hours"]))
    tier_text = str(task.get("tier") or "")
    tier = int(tier_text) if tier_text.isdigit() else 9
    return (0 if missing else 1, -overdue, tier)


def select_due_tasks(
    tasks: list[dict[str, Any]],
    schedule: dict[str, Any],
    now: datetime,
    config: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    due = [task for task in tasks if task_is_due(task, schedule, now)]
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in due:
        by_lane[task["lane"]].append(task)
    market_rank = {row["marketplace"]: (0 if row.get("major") else 1, index)
                   for index, row in enumerate(config["markets"])}
    for lane in by_lane:
        by_lane[lane].sort(key=lambda task: (
            _due_sort_key(task, schedule, now), market_rank[task["marketplace"]], task["key"]
        ))

    # Reserve turns for every tier, including while the initial matrix fills.
    # Previously all never-searched Tier 1 routes preceded every Tier 2/3 route.
    known_by_tier = {tier: [task for task in by_lane.get("known", []) if task["tier"] == tier]
                     for tier in ("1", "2", "3")}
    by_lane["known"] = []
    while any(known_by_tier.values()):
        for tier, slots in (("1", 6), ("2", 4), ("3", 2)):
            by_lane["known"].extend(known_by_tier[tier][:slots])
            del known_by_tier[tier][:slots]

    selected: list[dict[str, Any]] = []
    # Repeat the lane allocation during catch-up instead of giving all extra
    # slots to one lane. Empty lanes donate their slots to the remaining lanes.
    while len(selected) < limit:
        previous_count = len(selected)
        for lane in ("known", "broad", "unicorn", "title", "category"):
            count = min(max(0, int(config["lane_slots"].get(lane, 0))), limit - len(selected))
            selected.extend(by_lane.get(lane, [])[:count])
            del by_lane[lane][:count]
        if len(selected) == previous_count:
            break
    return selected


def coverage_status(tasks: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    schedule = state["schedule"]
    return {
        "never_searched": sum(parse_stamp(schedule.get(task["key"])) is None for task in tasks),
        "never_searched_by_tier": {
            tier: sum(task["lane"] == "known" and task["tier"] == tier
                      and parse_stamp(schedule.get(task["key"])) is None for task in tasks)
            for tier in ("1", "2", "3")
        },
        "due": sum(task_is_due(task, schedule, now) for task in tasks),
        "unfinished_windows": len(state.get("search_windows", {})),
    }


def discovery_limits(config: dict[str, Any], state: dict[str, Any], now: datetime) -> tuple[int, int]:
    """Recover missed cycles within the existing daily and shared quota caps."""
    primary = max(1, int(config["max_primary_searches_per_discovery"]))
    interval = max(5, int(config["discovery_interval_minutes"])) * 60
    last = parse_stamp(state.get("last_discovery_at"))
    cycles = max(1, int((now - last).total_seconds() / interval)) if last else 1
    coverage = coverage_status(build_tasks(config), state, now)
    if coverage["never_searched"]:
        # Bootstrap must cover all tiers promptly rather than taking dozens of
        # successful scheduled runs before the last tier is even attempted.
        cycles = max(cycles, (coverage["due"] + primary - 1) // primary)
    cycles = min(cycles, max(1, int(config.get("max_catchup_cycles", 1))))
    return primary * cycles, max(0, int(config["max_pagination_calls_per_discovery"])) * cycles


class ClientPool:
    """Marketplace-specific clients sharing one application access token."""

    def __init__(self) -> None:
        self.clients: dict[str, ebay_api.EbayBrowseClient] = {}
        self.shared_token: str | None = None

    def get(self, marketplace: str) -> ebay_api.EbayBrowseClient:
        if marketplace not in self.clients:
            client = ebay_api.EbayBrowseClient(marketplace=marketplace)
            if self.shared_token:
                client._access_token = self.shared_token
            self.clients[marketplace] = client
        return self.clients[marketplace]

    def sync_token(self, client: ebay_api.EbayBrowseClient) -> None:
        if client._access_token:
            self.shared_token = client._access_token
            for other in self.clients.values():
                if not other._access_token:
                    other._access_token = self.shared_token

    @property
    def calls(self) -> int:
        return sum(client.browse_calls for client in self.clients.values())

    def quota(self) -> dict[str, Any]:
        client = self.get("EBAY_GB")
        quota = client.browse_quota()
        self.sync_token(client)
        return quota


def _search_page(
    client: ebay_api.EbayBrowseClient,
    task: dict[str, Any],
    config: dict[str, Any],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    return client.search_page(
        task.get("query"),
        limit=int(config["page_size"]),
        category_ids=task.get("category_ids"),
        fixed_price_only=False,
        buying_options=["AUCTION"],
        ending_start_date=utc_stamp(start),
        ending_end_date=utc_stamp(end),
        search_in_description=bool(task.get("search_in_description")),
        sort="endingSoonest",
    )


def search_task(
    client: ebay_api.EbayBrowseClient,
    task: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
    extra_call_budget: int,
    checkpoint: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Resume the same frozen window until every page has been drained."""
    if checkpoint is None:
        checkpoint = checkpoint_search.new_checkpoint(
            utc_stamp(now), utc_stamp(now + timedelta(hours=float(task["horizon_hours"])))
        )

    def fetch_window(start: str, end: str, offset: int) -> dict[str, Any]:
        return client.search_page(
            task.get("query"), limit=int(config["page_size"]),
            category_ids=task.get("category_ids"), fixed_price_only=False,
            buying_options=["AUCTION"], ending_start_date=start,
            ending_end_date=end, offset=offset,
            search_in_description=bool(task.get("search_in_description")),
            sort="endingSoonest",
        )

    rows, calls, complete = checkpoint_search.drain(
        checkpoint, fetch_window, client.search_next,
        max_calls=1 + max(0, extra_call_budget), page_size=int(config["page_size"]),
        retryable_errors=(ebay_api.EbayApiError, ValueError),
    )
    return rows, max(0, calls - 1), complete


def _auction_price(item: dict[str, Any]) -> tuple[float | None, str]:
    for value_key, currency_key in (
        ("current_bid_value", "current_bid_currency"),
        ("price_value", "price_currency"),
    ):
        try:
            value = float(item.get(value_key)) if item.get(value_key) is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None:
            return value, str(item.get(currency_key) or "").upper()
    return None, ""


def _visible_terms(item: dict[str, Any], terms: list[str]) -> list[str]:
    haystack = normalized(
        " ".join(
            str(item.get(field) or "")
            for field in ("title", "context", "description", "author", "publisher")
        )
    )
    return [term for term in terms if normalized(term) and normalized(term) in haystack]


def _has_discovery_evidence(item: dict[str, Any]) -> bool:
    text = normalized(" ".join(str(item.get(field) or "") for field in ("title", "context")))
    return any(normalized(term) in text for term in PHOTO_EVIDENCE | COLLECTIBLE_EVIDENCE)


def _target_collision(
    item: dict[str, Any],
    lane: str,
    visible_terms: list[str],
) -> tuple[bool, str]:
    quality = core_targets.target_object_context(item)
    if not visible_terms:
        return False, quality
    if lane == "title":
        # Single title phrases such as Small World, The Valley and The British
        # Isles are intentionally broad. Generic Books context is not enough.
        return quality != "supported", quality
    if lane == "known":
        if quality == "name_only":
            return True, quality
        if (
            quality == "book_context"
            and core_targets.target_names_need_photo_evidence(visible_terms)
        ):
            return True, quality
    return False, quality


def _unicorn_match_quality(
    target: dict[str, Any],
    visible_terms: list[str],
) -> str:
    visible = {normalized(value) for value in visible_terms if normalized(value)}
    contributor_terms = [
        str(target.get("Contributor") or ""),
        *str(target.get("Contributor aliases") or "").split("|"),
    ]
    title_terms = [
        str(target.get("Title") or ""),
        *str(target.get("Title aliases") or "").split("|"),
    ]
    contributor_hit = any(normalized(value) in visible for value in contributor_terms if normalized(value))
    title_hit = any(normalized(value) in visible for value in title_terms if normalized(value))
    if contributor_hit and title_hit:
        return "exact_pair"
    if visible:
        return "partial_target"
    return "hidden_description"


def _demote_persisted_target_collision(candidate: dict[str, Any]) -> dict[str, Any]:
    """Re-score old candidate state so pre-fix junk cannot keep alerting."""
    lane = str(candidate.get("discovery_lane") or "")
    if lane not in {"known", "title"}:
        return candidate
    visible = [str(value) for value in candidate.get("matched_target_terms") or []]
    collision, quality = _target_collision(candidate, lane, visible)
    if not collision:
        return candidate
    tier = str(candidate.get("query_target_tier") or "")
    cap = 57 if lane == "title" else {"1": 57, "2": 55, "3": 53}.get(tier, 57)
    result = dict(candidate)
    result["opportunity_score"] = min(int(result.get("opportunity_score") or 0), cap)
    score = int(result["opportunity_score"])
    result["score_band"] = (
        "urgent" if score >= 90 else "alert" if score >= 72
        else "review" if score >= 55 else "reject"
    )
    result["target_match_quality"] = quality
    reasons = [str(value) for value in result.get("opportunity_reasons") or []]
    reasons.append(
        "persisted target-name/title collision rechecked and retained below alert threshold "
        "pending photographic evidence"
    )
    result["opportunity_reasons"] = list(dict.fromkeys(reasons))
    return result


def apply_book_target(
    item: dict[str, Any], score: int, reasons: list[str],
    initial_alert_score: int, lane: str,
) -> int:
    judgment = target_books.assess_listing(item)
    item["book_judgment"] = judgment
    special = bool(recall.collectible_signals(item) & recall.STRONG_SPECIAL_SIGNALS)
    if judgment.get("known_later_edition") and not special:
        reasons.append("known later edition is distinct from the curated target")
        return min(score, initial_alert_score - 1)
    if judgment.get("target_book"):
        reasons.append(f"curated target book: {judgment['target_book']}")
        return max(score, 86 if judgment.get("title_only") else 82)
    if judgment.get("non_target_book") and lane == "known" and not special:
        reasons.append("known other book does not inherit target first-edition priority")
        return min(score, initial_alert_score - 1)
    return score


def candidate_from_summary(
    raw: dict[str, Any],
    task: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    source = {
        "id": f"endgame_{task['lane']}_{task['marketplace'].lower()}",
        "name": f"eBay Endgame {task['lane']} {task['marketplace']}",
        "marketplace": task["marketplace"],
    }
    item = ebay_api.listing_from_summary(raw, source)
    if item is None:
        return None
    options = {str(value).upper() for value in item.get("buying_options") or []}
    if "AUCTION" not in options:
        return None
    ending = parse_stamp(item.get("item_end_date"))
    if ending is None or ending <= now:
        return None

    classified = legacy.classify(item)
    lane = str(task["lane"])
    tier = str(task.get("tier") or "")
    visible = _visible_terms(classified, list(task.get("terms") or []))
    score = int(classified.get("opportunity_score") or 0)
    reasons = [str(reason) for reason in classified.get("opportunity_reasons") or []]
    collision, target_quality = _target_collision(classified, lane, visible)
    if lane == "known":
        provisional = {"1": 66, "2": 63, "3": 60}[tier]
        confirmed = {"1": 88, "2": 82, "3": 76}[tier]
        collision_cap = {"1": 57, "2": 55, "3": 53}[tier]
        if visible and not collision:
            score = max(score, confirmed)
            reasons.append(f"Tier {tier} photographer query visibly matched {', '.join(visible[:3])}")
        elif visible:
            score = min(score, collision_cap)
            reasons.append(
                f"Tier {tier} visible name match lacks enough photographic identity evidence; "
                "retained below alert threshold pending richer detail"
            )
        else:
            score = max(score, provisional)
            target_quality = "hidden_description"
            reasons.append(f"Tier {tier} photographer query matched title or seller description")
    elif lane == "title":
        if visible and not collision:
            score = max(score, 86)
            reasons.append(f"Tier 1 photobook-title query visibly matched {', '.join(visible[:3])}")
        elif visible:
            score = min(score, 57)
            reasons.append(
                "Visible title-term match lacks photographic/art-book context; "
                "retained below alert threshold pending richer detail"
            )
        else:
            score = max(score, 64)
            target_quality = "hidden_description"
            reasons.append("Tier 1 photobook-title query matched title or seller description")
    elif lane == "unicorn":
        target_quality = _unicorn_match_quality(task.get("unicorn_target") or {}, visible)
        if target_quality == "exact_pair":
            score = max(score, 92)
            reasons.append("Pre-1970 unicorn query visibly matched both photographer and target title")
        elif visible:
            score = max(score, 68)
            reasons.append(
                "Pre-1970 unicorn query matched one visible target component; "
                "retained for detail verification"
            )
        else:
            score = max(score, 66)
            reasons.append("Pre-1970 unicorn query matched hidden seller description")
    elif lane in {"broad", "category"}:
        minimum = 42 if lane == "broad" else 50
        if not classified.get("recognized") and score < minimum and not _has_discovery_evidence(classified):
            return None

    score = apply_book_target(classified, score, reasons,
                              int(config["initial_alert_score"]), lane)

    classified["opportunity_score"] = score
    classified["opportunity_reasons"] = list(dict.fromkeys(reasons))
    classified["score_band"] = "urgent" if score >= 90 else "alert" if score >= 72 else "review" if score >= 55 else "reject"
    classified["marketplace"] = task["marketplace"]
    classified["query_target_tier"] = tier
    classified["unicorn_target_tier"] = str(task.get("unicorn_tier") or "")
    classified["unicorn_target"] = dict(task.get("unicorn_target") or {})
    classified["discovery_lane"] = lane
    classified["discovery_query"] = task.get("query") or "category-only Books sweep"
    classified["matched_target_terms"] = visible
    classified["target_match_quality"] = target_quality if lane in {"known", "title", "unicorn"} else ""
    classified["target_query_terms"] = (
        list(task.get("terms") or []) if lane in {"known", "title", "unicorn"} else []
    )
    classified["first_seen"] = utc_stamp(now)
    classified["last_seen"] = utc_stamp(now)
    classified["initial_alerted_at"] = ""
    classified["final_alerted_at"] = ""
    classified["live_verification"] = "search result only"
    classified["discovery_paths"] = [f"{lane}:{task['marketplace']}:{task.get('query') or 'category'}"]
    classified["marketplaces"] = [task["marketplace"]]
    classified["recognition_matches"] = list(classified.get("recognition_matches") or [])[:3]
    if isinstance(classified.get("description"), str):
        classified["description"] = classified["description"][:5000]
    return classified


def merge_candidate(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return incoming
    merged = dict(existing)
    protected = {
        "first_seen", "initial_alerted_at", "final_alerted_at", "live_verification",
    }
    for key, value in incoming.items():
        if key in protected:
            continue
        if value not in (None, "", [], {}):
            merged[key] = value
    merged["first_seen"] = existing.get("first_seen") or incoming.get("first_seen")
    merged["last_seen"] = incoming.get("last_seen")
    merged["initial_alerted_at"] = existing.get("initial_alerted_at") or ""
    merged["final_alerted_at"] = existing.get("final_alerted_at") or ""
    merged["live_verification"] = existing.get("live_verification") or incoming.get("live_verification")
    merged["marketplaces"] = sorted(
        set(str(value) for value in (existing.get("marketplaces") or []) + (incoming.get("marketplaces") or []))
    )
    merged["discovery_paths"] = list(
        dict.fromkeys((existing.get("discovery_paths") or []) + (incoming.get("discovery_paths") or []))
    )[-20:]
    merged["matched_target_terms"] = list(
        dict.fromkeys((existing.get("matched_target_terms") or []) + (incoming.get("matched_target_terms") or []))
    )
    merged["target_query_terms"] = list(
        dict.fromkeys((existing.get("target_query_terms") or []) + (incoming.get("target_query_terms") or []))
    )
    lane_rank = {"unicorn": 0, "known": 1, "title": 2, "broad": 3, "category": 4}
    existing_lane = str(existing.get("discovery_lane") or "")
    incoming_lane = str(incoming.get("discovery_lane") or "")
    if lane_rank.get(existing_lane, 9) < lane_rank.get(incoming_lane, 9):
        for key in (
            "discovery_lane", "discovery_query", "query_target_tier",
            "unicorn_target_tier", "unicorn_target",
        ):
            if existing.get(key) not in (None, ""):
                merged[key] = existing[key]
    if int(existing.get("opportunity_score") or 0) > int(incoming.get("opportunity_score") or 0):
        for key in (
            "opportunity_score", "score_band", "opportunity_reasons", "recognized",
            "recognition_matches", "best_recognition", "collecting_lane", "opportunity_kind",
        ):
            if key in existing:
                merged[key] = existing[key]
    return merged


def discover(
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    pool: ClientPool,
    call_budget: int,
) -> dict[str, Any]:
    primary_cap, pagination_cap = discovery_limits(config, state, now)
    primary_limit = min(primary_cap, max(0, call_budget))
    tasks = build_tasks(config)
    selected = select_due_tasks(tasks, state["schedule"], now, config, primary_limit)
    pagination_remaining = min(
        pagination_cap,
        max(0, call_budget - len(selected)),
    )
    stats: dict[str, Any] = {
        "task_count": len(tasks),
        "due_count": sum(task_is_due(task, state["schedule"], now) for task in tasks),
        "selected_count": len(selected),
        "successful_tasks": 0,
        "search_calls": 0,
        "pagination_calls": 0,
        "raw_results": 0,
        "accepted_results": 0,
        "new_candidates": 0,
        "dense_incomplete": [],
        "errors": [],
        "lane_calls": {},
    }
    lane_calls: dict[str, int] = defaultdict(int)
    start_calls = pool.calls

    for task in selected:
        if pool.calls - start_calls >= call_budget:
            break
        client = pool.get(task["marketplace"])
        extra_for_task = min(pagination_remaining, int(config["max_pagination_calls_per_discovery"]),
                             max(0, call_budget - (pool.calls - start_calls) - 1))
        windows = state.setdefault("search_windows", {})
        checkpoint = windows.get(task["key"])
        if not isinstance(checkpoint, dict) or checkpoint.get("completed"):
            checkpoint = checkpoint_search.new_checkpoint(
                utc_stamp(now), utc_stamp(now + timedelta(hours=float(task["horizon_hours"])))
            )
            windows[task["key"]] = checkpoint
        previous_pages = int(checkpoint.get("successful_pages", 0))
        try:
            rows, extra_used, complete = search_task(client, task, config, now, extra_for_task, checkpoint)
            pool.sync_token(client)
        except (ebay_api.EbayApiError, ValueError) as exc:
            stats["errors"].append(f"{task['key']}: {exc}")
            # Some eBay marketplaces do not expose the shared Books category.
            # Avoid letting one unsupported category task occupy every cycle.
            if task["lane"] == "category" and "valid 'q', 'category_ids'" in str(exc):
                state["schedule"][task["key"]] = utc_stamp(now)
            continue
        if int(checkpoint.get("successful_pages", 0)) > previous_pages:
            stats["successful_tasks"] += 1
        if checkpoint.get("last_error"):
            stats["errors"].append(f"{task['key']}: {checkpoint['last_error']}")
        pagination_remaining -= extra_used
        stats["pagination_calls"] += extra_used
        lane_calls[task["lane"]] += 1 + extra_used
        stats["raw_results"] += len(rows)
        if not complete:
            stats["dense_incomplete"].append(task["key"])
            retry_after = timedelta(minutes=int(config["discovery_interval_minutes"]))
            state["schedule"][task["key"]] = utc_stamp(
                now - timedelta(hours=float(task["interval_hours"])) + retry_after
            )
        else:
            # This completed the original frozen window, not a fresh one.
            state["schedule"][task["key"]] = str(checkpoint["start"])
            windows.pop(task["key"], None)

        for raw in rows:
            candidate = candidate_from_summary(raw, task, config, now)
            if candidate is None:
                continue
            stats["accepted_results"] += 1
            key = str(candidate["key"])
            existing = state["candidates"].get(key)
            if not isinstance(existing, dict):
                existing = None
                stats["new_candidates"] += 1
            state["candidates"][key] = merge_candidate(existing, candidate)

    stats["search_calls"] = pool.calls - start_calls
    stats["lane_calls"] = dict(lane_calls)
    stats["coverage"] = coverage_status(tasks, state, now)
    state["last_discovery_attempt_at"] = utc_stamp(now)
    if stats["successful_tasks"]:
        state["last_discovery_at"] = utc_stamp(now)
    stats["unfinished_windows"] = len(state.get("search_windows", {}))
    return stats


def _detail_money(detail: dict[str, Any], field: str) -> tuple[float | None, str]:
    value = detail.get(field) if isinstance(detail.get(field), dict) else {}
    try:
        amount = round(float(value.get("value")), 2)
    except (TypeError, ValueError):
        amount = None
    return amount, str(value.get("currency") or "").upper()


def enrich_candidate(candidate: dict[str, Any], detail: dict[str, Any], now: datetime,
                     initial_alert_score: int = 58) -> tuple[dict[str, Any], bool, str]:
    merged = legacy._merge_live_detail(candidate, detail)
    merged["item_end_date"] = str(detail.get("itemEndDate") or merged.get("item_end_date") or "")
    current_bid, current_currency = _detail_money(detail, "currentBidPrice")
    minimum_bid, minimum_currency = _detail_money(detail, "minimumPriceToBid")
    if current_bid is not None:
        merged["current_bid_value"] = current_bid
        merged["current_bid_currency"] = current_currency
    if minimum_bid is not None:
        merged["minimum_bid_value"] = minimum_bid
        merged["minimum_bid_currency"] = minimum_currency
    try:
        merged["bid_count"] = max(0, int(detail.get("bidCount") or merged.get("bid_count") or 0))
    except (TypeError, ValueError):
        pass
    if "reservePriceMet" in detail:
        merged["reserve_price_met"] = detail.get("reservePriceMet")

    estimated = str(detail.get("estimatedAvailabilityStatus") or "").upper()
    end = parse_stamp(merged.get("item_end_date"))
    options = {str(value).upper() for value in detail.get("buyingOptions") or merged.get("buying_options") or []}
    live = True
    reason = "live auction verified"
    if estimated in {"OUT_OF_STOCK", "UNAVAILABLE"}:
        live = False
        reason = estimated.lower().replace("_", " ")
    elif end is not None and end <= now:
        live = False
        reason = "listing ended"
    elif options and "AUCTION" not in options:
        live = False
        reason = "auction buying option no longer present"

    rescored = legacy.classify(merged)
    tier = str(merged.get("query_target_tier") or "")
    terms = list(merged.get("target_query_terms") or [])
    visible = _visible_terms(rescored, terms)
    score = int(rescored.get("opportunity_score") or 0)
    reasons = [str(value) for value in rescored.get("opportunity_reasons") or []]
    lane = str(merged.get("discovery_lane") or "")
    collision, target_quality = _target_collision(rescored, lane, visible)
    # Title tasks also carry tier=1. Test the lane first so a generic book
    # called Small World or The British Isles cannot be re-promoted merely
    # because a detail request succeeded.
    if lane == "title":
        if visible and not collision:
            score = max(score, 86)
            reasons.append("Tier 1 title-targeted auction with photographic/art-book context")
        elif visible:
            score = min(score, 57)
            reasons.append("Visible title match still lacks photographic/art-book context after detail refresh")
        else:
            score = max(score, 64)
            target_quality = "hidden_description"
            reasons.append("Tier 1 title-targeted auction matched hidden seller text")
    elif lane == "unicorn":
        target_quality = _unicorn_match_quality(merged.get("unicorn_target") or {}, visible)
        if target_quality == "exact_pair":
            score = max(score, 92)
            reasons.append("Pre-1970 unicorn auction verified both photographer and target title")
        elif visible:
            score = max(score, 68)
            reasons.append("Pre-1970 unicorn auction still has only a partial visible target match")
        else:
            score = max(score, 66)
            target_quality = "hidden_description"
            reasons.append("Pre-1970 unicorn auction matched hidden seller text")
    elif tier in {"1", "2", "3"}:
        if visible and not collision:
            score = max(score, {"1": 88, "2": 82, "3": 76}[tier])
            reasons.append(f"Tier {tier} targeted auction with sufficient photographic/book identity evidence")
        elif visible:
            score = min(score, {"1": 57, "2": 55, "3": 53}[tier])
            reasons.append(
                f"Tier {tier} visible name match still lacks enough photographic identity evidence after detail refresh"
            )
        else:
            score = max(score, {"1": 66, "2": 63, "3": 60}[tier])
            target_quality = "hidden_description"
            reasons.append(f"Tier {tier} targeted auction matched hidden seller text")
    score = apply_book_target(rescored, score, reasons,
                              initial_alert_score, lane)
    rescored["opportunity_score"] = score
    rescored["opportunity_reasons"] = list(dict.fromkeys(reasons))
    rescored["score_band"] = "urgent" if score >= 90 else "alert" if score >= 72 else "review" if score >= 55 else "reject"
    rescored["matched_target_terms"] = list(dict.fromkeys((merged.get("matched_target_terms") or []) + visible))
    rescored["target_match_quality"] = (
        target_quality if tier in {"1", "2", "3"} or lane in {"title", "unicorn"} else ""
    )
    rescored["last_detail_at"] = utc_stamp(now)
    rescored["live_verification"] = reason
    rescored["active"] = live
    if isinstance(rescored.get("description"), str):
        rescored["description"] = rescored["description"][:5000]
    rescored["recognition_matches"] = list(rescored.get("recognition_matches") or [])[:3]
    return rescored, live, reason


def due_phase(candidate: dict[str, Any], now: datetime, config: dict[str, Any]) -> tuple[str, float] | None:
    ending = parse_stamp(candidate.get("item_end_date"))
    if ending is None:
        return None
    minutes = (ending - now).total_seconds() / 60.0
    if minutes <= 0:
        return None
    if minutes <= float(config["final_alert_minutes"]) and not candidate.get("final_alerted_at"):
        return "final", minutes
    if minutes <= float(config["initial_alert_minutes"]) and not candidate.get("initial_alerted_at"):
        return "initial", minutes
    return None


def collect_deadline_alerts(
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    pool: ClientPool,
    detail_budget: int,
) -> tuple[list[dict[str, Any]], int]:
    due: list[tuple[int, int, float, int, str, dict[str, Any]]] = []
    for key, candidate in list(state["candidates"].items()):
        if not isinstance(candidate, dict) or candidate.get("active") is False:
            continue
        candidate = _demote_persisted_target_collision(candidate)
        state["candidates"][key] = candidate
        phase = due_phase(candidate, now, config)
        if phase is None:
            continue
        name, minutes = phase
        threshold = int(config["final_alert_score"] if name == "final" else config["initial_alert_score"])
        if name == "final" and not candidate.get("initial_alerted_at"):
            threshold = min(threshold, int(config["initial_alert_score"]))
        score = int(candidate.get("opportunity_score") or 0)
        due.append((0 if name == "final" else 1, 0 if score >= threshold else 1, minutes, -score, key, candidate))
    due.sort(key=lambda value: value[:4])

    alerts: list[dict[str, Any]] = []
    detail_calls = 0
    for _, _, minutes, _, key, candidate in due:
        working = dict(candidate)
        if detail_calls < detail_budget:
            client = pool.get(str(working.get("marketplace") or "EBAY_GB"))
            try:
                detail = client.get_item(str(working.get("rest_item_id") or working.get("external_id") or ""))
                pool.sync_token(client)
                working, live, _ = enrich_candidate(
                    working, detail, now, int(config["initial_alert_score"])
                )
                detail_calls += 1
                state["candidates"][key] = working
                if not live:
                    continue
            except (ebay_api.EbayApiError, ValueError) as exc:
                detail_calls += 1
                working["live_verification"] = f"LIVE STATUS NOT VERIFIED - CHECK BEFORE BIDDING ({exc})"
                working["last_detail_at"] = utc_stamp(now)
                state["candidates"][key] = working
        else:
            working["live_verification"] = "LIVE STATUS NOT VERIFIED - CHECK BEFORE BIDDING (detail budget reserved)"
            state["candidates"][key] = working

        phase_info = due_phase(working, now, config)
        if phase_info is None:
            continue
        phase, minutes = phase_info
        threshold = int(config["final_alert_score"] if phase == "final" else config["initial_alert_score"])
        if phase == "final" and not working.get("initial_alerted_at"):
            # A late discovery must still get one warning even when its detail
            # refresh could not complete before the final window.
            threshold = min(threshold, int(config["initial_alert_score"]))
        if int(working.get("opportunity_score") or 0) < threshold:
            continue
        alert = dict(working)
        alert["alert_phase"] = phase
        alert["minutes_remaining"] = max(0.0, minutes)
        alerts.append(alert)
        if phase == "final":
            working["final_alerted_at"] = utc_stamp(now)
            working["initial_alerted_at"] = working.get("initial_alerted_at") or utc_stamp(now)
        else:
            working["initial_alerted_at"] = utc_stamp(now)
        state["candidates"][key] = working
    return alerts, detail_calls


def prune_candidates(state: dict[str, Any], now: datetime, retention_hours: float) -> int:
    removed = 0
    keep: dict[str, Any] = {}
    cutoff = now - timedelta(hours=retention_hours)
    for key, candidate in state["candidates"].items():
        if not isinstance(candidate, dict):
            removed += 1
            continue
        ending = parse_stamp(candidate.get("item_end_date"))
        last_seen = parse_stamp(candidate.get("last_seen"))
        if ending is not None and ending < cutoff:
            removed += 1
            continue
        if ending is None and last_seen is not None and last_seen < cutoff:
            removed += 1
            continue
        keep[key] = candidate
    state["candidates"] = keep
    return removed


def _money_line(candidate: dict[str, Any]) -> str:
    amount, currency = _auction_price(candidate)
    if amount is None:
        return "price not returned"
    shipping = candidate.get("shipping_value")
    if shipping is None:
        return f"{currency} {amount:.2f} plus shipping not returned"
    try:
        return f"{currency} {amount:.2f} plus {currency} {float(shipping):.2f} shipping"
    except (TypeError, ValueError):
        return f"{currency} {amount:.2f} plus shipping not returned"


def _candidate_markdown(candidate: dict[str, Any]) -> str:
    ending = parse_stamp(candidate.get("item_end_date"))
    end_label = utc_stamp(ending) if ending else "unknown"
    minutes = float(candidate.get("minutes_remaining") or 0)
    score = int(candidate.get("opportunity_score") or 0)
    matches = candidate.get("matched_target_terms") or []
    best = candidate.get("best_recognition") if isinstance(candidate.get("best_recognition"), dict) else {}
    recognition_line = "none"
    if best:
        recognition_line = f"{best.get('contributor')}, *{best.get('title')}* (match {best.get('score')}/100)"
    reasons = [str(value) for value in candidate.get("opportunity_reasons") or []]
    reason_line = "; ".join(reasons[:7]) or "recall-first auction discovery"
    judgment = candidate.get("book_judgment") or {}
    book_line = (
        f"- **Target book:** {judgment['target_book']}\n"
        f"- **Collectibility:** {judgment['collectibility']}\n"
        f"- **Identification confidence:** {judgment['identification_confidence']}\n"
        f"- **Price opportunity:** {judgment['price_opportunity']}\n"
        if judgment.get("target_book") else ""
    )
    verification = str(candidate.get("live_verification") or "LIVE STATUS NOT VERIFIED - CHECK BEFORE BIDDING")
    image = str(candidate.get("image_url") or "")
    image_line = f"\n![Listing image]({image})\n" if image else ""
    return (
        f"### [{candidate.get('title')}]({candidate.get('url')})\n\n"
        f"- **Ends:** {end_label} - about {minutes:.0f} minutes remaining when checked\n"
        f"- **Bid/price:** {_money_line(candidate)} - {int(candidate.get('bid_count') or 0)} bids\n"
        f"- **Seller:** {candidate.get('vendor') or 'not returned'} ({candidate.get('seller_account_type') or 'account type unknown'})\n"
        f"- **Marketplace:** {candidate.get('marketplace')}\n"
        f"- **Endgame score:** {score}/100\n"
        f"- **Priority target:** {', '.join(matches) if matches else 'query or local recognition match'}"
        f"{f' (Tier {candidate.get("query_target_tier")})' if candidate.get('query_target_tier') else ''}\n"
        f"- **Best library recognition:** {recognition_line}\n"
        f"{book_line}"
        f"- **Why surfaced:** {reason_line}\n"
        f"- **Live check:** {verification}\n"
        f"- **Discovery:** {candidate.get('discovery_query')}\n"
        f"{image_line}\n"
    )


def write_alert_packets(alerts: list[dict[str, Any]], runtime: Path, config: dict[str, Any], now: datetime) -> int:
    alert_dir = runtime / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    for old in alert_dir.iterdir():
        if old.is_file():
            old.unlink()
    count = 0
    batch_size = max(1, int(config["issue_batch_size"]))
    for phase in ("final", "initial"):
        items = [item for item in alerts if item.get("alert_phase") == phase]
        items.sort(key=lambda item: (float(item.get("minutes_remaining") or 0), -int(item.get("opportunity_score") or 0)))
        for index in range(0, len(items), batch_size):
            chunk = items[index:index + batch_size]
            count += 1
            label = "4H" if phase == "final" else "EARLY"
            stem = alert_dir / f"{count:03d}-{phase}"
            noun = "auction" if len(chunk) == 1 else "auctions"
            (stem.with_suffix(".title")).write_text(
                f"ENDGAME_{label}: {len(chunk)} collectible photobook {noun} ending soon\n",
                encoding="utf-8",
            )
            intro = (
                f"Endgame Auction Radar {label} candidate packet for AI review. "
                "Recall is intentionally favoured over precision, so check the exact edition, shipping and live bid state before bidding.\n\n"
                f"Detected at {utc_stamp(now)}.\n\n"
            )
            body = intro + "\n---\n\n".join(_candidate_markdown(item) for item in chunk)
            stem.with_suffix(".md").write_text(body, encoding="utf-8")
    return count


def write_summary(
    runtime: Path,
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    quota: dict[str, Any] | None,
    quota_error: str,
    discovery_stats: dict[str, Any] | None,
    alerts: list[dict[str, Any]],
    calls_used: int,
    removed: int,
) -> None:
    tasks = build_tasks(config)
    coverage = coverage_status(tasks, state, now)
    lines = [
        "# eBay Endgame Auction Radar",
        "",
        f"- Run: {utc_stamp(now)}",
        f"- Browse calls this run: {calls_used}",
        f"- Endgame calls today: {state['usage'].get('calls', 0)} / {config['daily_call_cap']}",
        f"- Persistent candidates: {len(state['candidates'])}",
        f"- Alerts generated: {len(alerts)}",
        f"- Expired candidates pruned: {removed}",
        f"- Configured primary demand: {projected_primary_calls_per_day(tasks):.0f} calls/day before pagination and details",
        f"- Never-searched name routes by tier: {coverage['never_searched_by_tier']}",
        f"- Never-searched routes (all lanes): {coverage['never_searched']}; routes still due: {coverage['due']}",
        f"- Unfinished search windows retained for recovery: {coverage['unfinished_windows']}",
    ]
    if quota:
        lines.append(f"- Shared Browse quota remaining at start: {quota.get('remaining')} / {quota.get('limit')}")
    if quota_error:
        lines.append(f"- Quota telemetry warning: {quota_error}")
    if discovery_stats is None:
        lines.append("- Discovery: not due; deadline worker only")
    else:
        lines.extend(
            [
                f"- Discovery tasks selected: {discovery_stats['selected_count']} / {discovery_stats['due_count']} due",
                f"- Search rows: {discovery_stats['raw_results']}; accepted: {discovery_stats['accepted_results']}; new: {discovery_stats['new_candidates']}",
                f"- Discovery calls by lane: {discovery_stats['lane_calls']}",
                f"- Dense searches not fully drained: {len(discovery_stats['dense_incomplete'])}",
                f"- Search errors: {len(discovery_stats['errors'])}",
            ]
        )
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def discovery_due(state: dict[str, Any], config: dict[str, Any], now: datetime, force: bool) -> bool:
    if force:
        return True
    last = parse_stamp(state.get("last_discovery_at"))
    if last is None:
        return True
    interval = max(5, int(config["discovery_interval_minutes"]))
    return now - last >= timedelta(minutes=interval - 1)


def run_cycle(
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    pool: ClientPool,
    available_calls: int,
    *,
    force_discovery: bool = False,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, int]:
    start_calls = pool.calls
    alerts: list[dict[str, Any]] = []
    detail_limit = min(int(config["max_detail_calls_per_run"]), max(0, available_calls))
    early_alerts, details_used = collect_deadline_alerts(config, state, now, pool, detail_limit)
    alerts.extend(early_alerts)

    remaining = max(0, available_calls - (pool.calls - start_calls))
    discovery_stats = None
    if discovery_due(state, config, now, force_discovery) and remaining > 0:
        discovery_cap = sum(discovery_limits(config, state, now))
        discovery_stats = discover(config, state, now, pool, min(remaining, discovery_cap))

    remaining = max(0, available_calls - (pool.calls - start_calls))
    unused_detail = max(0, detail_limit - details_used)
    if remaining > 0 and unused_detail > 0:
        new_alerts, _ = collect_deadline_alerts(config, state, now, pool, min(remaining, unused_detail))
        alerts.extend(new_alerts)
    return discovery_stats, alerts, pool.calls - start_calls, details_used


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--force-discovery", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    config = load_config(args.config)
    state = load_state(args.state)
    reset_daily_usage(state, now)
    before = json.dumps(state, sort_keys=True, ensure_ascii=False)
    pool = ClientPool()
    quota: dict[str, Any] | None = None
    quota_error = ""
    try:
        quota = pool.quota()
    except ebay_api.EbayApiError as exc:
        quota_error = str(exc)

    used_today = max(0, int(state["usage"].get("calls") or 0))
    own_remaining = max(0, int(config["daily_call_cap"]) - used_today)
    available = own_remaining
    if quota is not None:
        available = min(available, max(0, int(quota.get("remaining") or 0) - int(config["shared_quota_reserve"])))

    discovery_stats, alerts, calls_used, _ = run_cycle(
        config,
        state,
        now,
        pool,
        available,
        force_discovery=args.force_discovery,
    )
    state["usage"]["calls"] = used_today + calls_used
    removed = prune_candidates(state, now, float(config["candidate_retention_hours"]))
    prior_quota_error = str(state.get("stats", {}).get("quota_error") or "")
    if discovery_stats is not None or calls_used or alerts or removed or quota_error != prior_quota_error:
        state["stats"] = {
            "last_run_at": utc_stamp(now),
            "last_calls_used": calls_used,
            "last_alert_count": len(alerts),
            "last_candidate_count": len(state["candidates"]),
            "last_discovery": (discovery_stats if discovery_stats is not None
                               else state.get("stats", {}).get("last_discovery", {})),
            "quota_error": quota_error,
        }

    packet_count = write_alert_packets(alerts, args.runtime, config, now)
    write_summary(args.runtime, config, state, now, quota, quota_error, discovery_stats, alerts, calls_used, removed)
    proposed = args.runtime / "proposed-state.json"
    write_json(proposed, state)
    changed = before != json.dumps(state, sort_keys=True, ensure_ascii=False)

    set_output("state_changed", str(changed).lower())
    set_output("alert_count", len(alerts))
    set_output("alert_packet_count", packet_count)
    set_output("calls_used", calls_used)
    set_output("candidate_count", len(state["candidates"]))
    set_output("new_candidate_count", (discovery_stats or {}).get("new_candidates", 0))
    set_output("discovery_ran", str(discovery_stats is not None).lower())
    print((args.runtime / "summary.md").read_text(encoding="utf-8"))
    if discovery_stats and discovery_stats["errors"] and not discovery_stats.get("successful_tasks"):
        # Partial successes are useful discoveries and must still be published.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
