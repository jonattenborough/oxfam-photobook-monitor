#!/usr/bin/env python3
"""Select a deterministic fixed-price wider-web source batch.

This helper does not scrape the sites. It makes the source rotation explicit so
the ChatGPT wider-web task can load a durable dealer universe and avoid ad-hoc
coverage. After a search pass, source ids can be marked checked to keep the
rotation state useful across runs.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = "data/external_buy_now_sources.json"
DEFAULT_STATE = "data/external_buy_now_rotation.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return payload


def parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def select_batch(registry: dict[str, Any], state: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    sources = registry.get("sources")
    if not isinstance(sources, list):
        raise RuntimeError("registry.sources is not a list")

    state_sources = state.setdefault("sources", {})
    if not isinstance(state_sources, dict):
        raise RuntimeError("state.sources is not an object")

    always = [s for s in sources if isinstance(s, dict) and s.get("always_each_run")]
    rotating = [s for s in sources if isinstance(s, dict) and not s.get("always_each_run")]

    def rotating_key(source: dict[str, Any]) -> tuple[Any, ...]:
        source_id = str(source["id"])
        source_state = state_sources.get(source_id)
        if not isinstance(source_state, dict):
            source_state = {}
        last = parse_stamp(source_state.get("last_checked"))
        never = last is None
        priority = int(source.get("priority", 3))
        return (
            0 if never else 1,
            priority,
            last or datetime.min.replace(tzinfo=timezone.utc),
            source_id,
        )

    rotating.sort(key=rotating_key)
    selected = always[:batch_size]
    remaining = max(0, batch_size - len(selected))
    selected.extend(rotating[:remaining])
    return selected


def mark_checked(state: dict[str, Any], source_ids: list[str], stamp: str) -> None:
    state_sources = state.setdefault("sources", {})
    for source_id in source_ids:
        row = state_sources.setdefault(source_id, {})
        row["last_checked"] = stamp
        row["check_count"] = int(row.get("check_count", 0)) + 1
    state["last_updated"] = stamp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--mark-checked", nargs="*", default=None)
    args = parser.parse_args()

    registry_path = Path(args.registry)
    state_path = Path(args.state)
    registry = load_json(registry_path)
    state = load_json(state_path) if state_path.exists() else {"version": 1, "sources": {}}

    if args.mark_checked is not None:
        stamp = utc_now()
        mark_checked(state, args.mark_checked, stamp)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rotation = registry.get("rotation") or {}
    default_batch = int(rotation.get("default_batch_size", 24))
    batch_size = args.batch_size or default_batch
    selected = select_batch(registry, state, batch_size)

    if args.format == "json":
        print(json.dumps(selected, indent=2, ensure_ascii=False))
    else:
        print("# Buy-now wider-web source batch")
        print()
        for source in selected:
            flag = "always" if source.get("always_each_run") else f"priority {source.get('priority', 3)}"
            print(f"- {source['name']} ({flag}) - {source['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
