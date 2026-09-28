"""Read-only, paginated GitHub Issue and comment snapshot with resume files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .store import now

API = "https://api.github.com/repos/jonattenborough/oxfam-photobook-monitor"


def _next(headers) -> str | None:
    link = headers.get("Link", "")
    for part in link.split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            return match.group(1) if match else None
    return None


def export(destination: Path) -> dict:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    manifest = {"repository": "jonattenborough/oxfam-photobook-monitor", "started_at": now(), "endpoints": {}, "complete": False}
    for label, initial in (
        ("issues", API + "/issues?state=all&per_page=100&sort=updated&direction=asc"),
        ("comments", API + "/issues/comments?per_page=100&sort=updated&direction=asc"),
    ):
        folder = destination / label
        folder.mkdir(mode=0o700, exist_ok=True)
        url = initial
        index = 1
        pages = []
        complete = False
        while url:
            path = folder / f"page-{index:05d}.json"
            meta_path = folder / f"page-{index:05d}.meta.json"
            if path.exists() and meta_path.exists():
                metadata = json.loads(meta_path.read_text())
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
                    raise RuntimeError(f"Export page hash changed: {path}")
                url = metadata["next"]
                pages.append(metadata)
                index += 1
                continue
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "photobook-radar-read-only-export", "X-GitHub-Api-Version": "2022-11-28"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    remaining = int(response.headers.get("X-RateLimit-Remaining", "0"))
                    next_url = _next(response.headers)
            except urllib.error.HTTPError as exc:
                manifest["endpoints"][label] = {"pages": pages, "complete": False, "error": f"HTTP {exc.code}"}
                break
            payload = json.loads(raw)
            if not isinstance(payload, list):
                raise RuntimeError("GitHub endpoint did not return a page list")
            metadata = {"page": index, "source_url": url, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "objects": len(payload), "next": next_url, "captured_at": now(), "remaining": remaining}
            path.write_bytes(raw)
            path.chmod(0o600)
            meta_path.write_text(json.dumps(metadata, indent=2))
            meta_path.chmod(0o600)
            pages.append(metadata)
            index += 1
            url = next_url
            if remaining < 2 and url:
                manifest["endpoints"][label] = {"pages": pages, "complete": False, "error": "GitHub API rate limit nearly exhausted; resume later"}
                break
        else:
            complete = True
        if label not in manifest["endpoints"]:
            manifest["endpoints"][label] = {"pages": pages, "complete": complete, "error": None}
        if not complete:
            break
    manifest["complete"] = all(manifest["endpoints"].get(label, {}).get("complete") for label in ("issues", "comments"))
    manifest["finished_at"] = now()
    destination.joinpath("manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
