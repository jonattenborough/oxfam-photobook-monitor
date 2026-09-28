"""Generate an evidence-linked source inventory from the checked-out repository."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from .store import now

SOURCE_MAP = {
    "monitor.yml": ("Oxfam Photography; Shelter/Crisis", "monitor.py; canon_runner.py; charity_monitor.py", "data/state.json; data/charity_state.json", "OXFAM_NEW; CHARITY_NEW"),
    "parent-monitor.yml": ("Oxfam broad Art and Photography", "parent_monitor.py; oxfam_parent_common.py", "data/parent_state.json", "OXFAM_ART_NEW"),
    "ebay-private-seller-monitor.yml": ("eBay private fixed price", "ebay_private_recall_monitor.py; ebay_private_alert_builder.py; ebay_private_issue_sync.py", "data/ebay_private_seller_state.json; data/ebay_private_recall_searches.json", "EBAY_PRIVATE_NEW"),
    "ebay-seller-monitor.yml": ("eBay charity/library sellers", "ebay_seller_monitor.py; ebay_charity_issue_sync.py", "data/ebay_seller_state.json; data/ebay_sellers.json", "CHARITY_NEW"),
    "ebay-endgame.yml": ("eBay Endgame auctions", "ebay_endgame.py; ebay_search_checkpoint.py", "data/ebay_endgame_state.json; data/ebay_endgame_targets.json", "ENDGAME_EARLY; ENDGAME_4H"),
    "market-monitor.yml": ("Four specialist feeds, two broad eBay feeds, AbeBooks targets", "market_monitor_safe.py; market_monitor.py; market_issue_batches.py", "data/market_state.json", "EXTERNAL_NEW"),
    "photobook-review-health.yml": ("Review health and handoff", "photobook_review_health.py; photobook_review_handoff.py", "data/photobook_review_index.json; data/photobook_review_handoff.json", "reporting only"),
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def generate(repo: Path) -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    workflows = []
    for path in sorted((repo / ".github/workflows").glob("*.yml")):
        raw = yaml.load(path.read_text(), Loader=yaml.BaseLoader) or {}
        trigger = raw.get("on") or {}
        schedules = [entry.get("cron") for entry in trigger.get("schedule", [])] if isinstance(trigger, dict) else []
        scripts = sorted(set(re.findall(r"\bpython(?:3)?\s+(?:-m\s+)?([\w./-]+\.py)", path.read_text())))
        area, entries, state, queue = SOURCE_MAP.get(path.name, ("Historical/manual workflow", "; ".join(scripts), "see workflow", "manual outputs"))
        workflows.append({"path": str(path.relative_to(repo)), "name": raw.get("name"), "schedules": schedules, "dispatch": isinstance(trigger, dict) and "workflow_dispatch" in trigger, "configured_as_recurring": bool(schedules), "remote_enabled_state": "NOT_VERIFIED", "area": area, "entry_points": entries, "state_and_config": state, "queue_prefixes": queue, "scripts": scripts, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    states = {}
    for name, key in (("state.json", "last_successful_fetch"), ("parent_state.json", "last_successful_fetch"), ("charity_state.json", "last_checked"), ("market_state.json", "last_successful_run"), ("ebay_private_seller_state.json", "last_run"), ("ebay_seller_state.json", "last_run"), ("ebay_endgame_state.json", "last_discovery_at"), ("external_state.json", "last_run")):
        path = repo / "data" / name
        data = _json(path)
        states[name] = {"last_reported_success_or_run": data.get(key), "timestamp_field": key, "bytes": path.stat().st_size if path.exists() else None, "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}
    endgame = _json(repo / "data/ebay_endgame_targets.json")
    private = _json(repo / "data/ebay_private_recall_searches.json")
    sellers = _json(repo / "data/ebay_sellers.json")
    market_text = (repo / "market_monitor_safe.py").read_text()
    raw_files = sorted(str(p.relative_to(repo)) for p in (repo / "data").rglob("*" ) if p.is_file())
    return {
        "generated_at": now(), "commit": commit, "remote_workflow_state_verified": False,
        "workflows": workflows, "state_files": states,
        "source_config": {"core_tiers": {k: len(v["names"]) for k, v in endgame["tiers"].items()}, "endgame_discovery_interval_minutes": endgame.get("discovery_interval_minutes"), "endgame_daily_cap": endgame.get("daily_call_cap"), "shared_reserve": endgame.get("shared_quota_reserve"), "endgame_alert_minutes": [endgame.get("initial_alert_minutes"), endgame.get("final_alert_minutes")], "private_max_logical_calls_per_run": private.get("max_api_calls_per_run"), "private_capture_max_price_gbp": private.get("max_price_gbp"), "market_retained_feed_ids": ["tpg_new", "photobookstore", "village", "setanta"], "market_exact_target_markets": ["abebooks"], "market_wrapper_evidence": "market_monitor_safe.py FEEDS filter and TARGET_MARKETS assignment", "seller_config_type": type(sellers).__name__},
        "data_files": raw_files,
        "manual_code": [str(p.relative_to(repo)) for p in sorted(repo.glob("*.py")) if any(term in p.name for term in ("full_scan", "backfill", "forensic", "audit", "initial_", "verify"))],
        "unmigrated_responsibilities": ["ChatGPT Wider Web task", "Publisher/Future Canon task", "Prize Watch task", "other ChatGPT review tasks: current state and full task export not accessible in this checkout"],
        "local_source_status": "SHADOW_IMPORT_ONLY",
    }


def write(repo: Path) -> tuple[Path, Path]:
    report = generate(repo)
    folder = repo / "docs/local"
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "source-inventory.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    lines = ["# Source inventory", "", f"Checked-out commit: `{report['commit']}`. Generated {report['generated_at']}.", "", "The workflow files below show configured schedules, not verified GitHub activation. Existing GitHub scanning was not changed. Local status is shadow import only.", "", "| Workflow | Configured schedule (UTC) | Responsibility | Entry point | State and output |", "|---|---|---|---|---|"]
    for item in report["workflows"]:
        lines.append(f"| `{Path(item['path']).name}` | {', '.join(item['schedules']) if item['schedules'] else 'Manual only'} | {item['area']} | {item['entry_points']} | {item['state_and_config']}; {item['queue_prefixes']} |")
    lines += ["", "## Last timestamps in retained state", "", "These are file contents at the checked-out commit, not independent proof of present live health.", "", "| File | Field | Value |", "|---|---|---|"]
    for name, state in report["state_files"].items():
        lines.append(f"| `data/{name}` | `{state['timestamp_field']}` | {state['last_reported_success_or_run'] or 'none'} |")
    config = report["source_config"]
    lines += ["", "## Important configuration", "", f"- Core tiers: {config['core_tiers']}; recognition library is loaded separately from checked-in sources.", f"- Endgame: {config['endgame_discovery_interval_minutes']}-minute code interval, {config['endgame_daily_cap']} daily cap, {config['shared_reserve']} shared reserve; alert thresholds {config['endgame_alert_minutes']} minutes.", f"- Private eBay: {config['private_max_logical_calls_per_run']} logical calls per run; GBP {config['private_capture_max_price_gbp']} discovery ceiling. This is not the GBP 150 recommendation cap.", "- Market wrapper retains four specialist feeds and two broad eBay feeds; exact-target market is AbeBooks only (see `market_monitor_safe.py`).", "- `external_monitor.py` exists, but `data/external_state.json` contains no active source result and no recurring workflow invokes it.", "", "## Migration boundaries", "", "- Manual full scans, forensic runs and backfills remain historical import sources only.", "- GitHub Issues and comments were exported separately; workflow artifacts still require an availability audit.", "- Wider Web, Publisher/Future Canon and Prize Watch were ChatGPT tasks in the handover. Their current task states are not verified here and remain unmigrated.", "- Every data path and manual code file appears in `source-inventory.json`.", ""]
    md_path = folder / "source-inventory.md"
    md_path.write_text("\n".join(lines))
    return md_path, json_path
