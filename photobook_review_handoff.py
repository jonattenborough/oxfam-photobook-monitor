#!/usr/bin/env python3
"""Publish bounded queue pages so reviewers never need to load the full index.

This is an index of existing unreviewed issues, not a gem rating. No candidate,
issue, or uncertain price is deleted. The manifest identifies the current pages.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from photobook_review_health import PREFIXES, atomic_json


def publish(index: dict[str, Any], runtime: Path, page_size: int = 10) -> dict[str, Any]:
    if not 1 <= page_size <= 20:
        raise ValueError('Queue pages must contain 1 to 20 issue summaries')
    manifest: dict[str, Any] = {
        'checked_at': index.get('checked_at'),
        'index_scan_complete': index.get('index_scan_complete', False),
        'limitations': 'Discovery index only. Re-fetch issue and comments; verify eBay availability and prices.',
        'queues': {},
    }
    for prefix in PREFIXES:
        source = prefix[:-1]
        entries = list(index.get('queues', {}).get(source, []))
        pages = []
        for offset in range(0, len(entries), page_size):
            subset = entries[offset:offset + page_size]
            filename = f'{source.lower()}-{offset // page_size + 1:04d}.json'
            relative = f'data/photobook_review_queue/{filename}'
            atomic_json(runtime / 'queue' / filename, {
                'checked_at': index.get('checked_at'), 'source': source,
                'issues': subset,
            })
            amounts = [e['lowest_observed_gbp'] for e in subset
                       if e.get('lowest_observed_gbp') is not None]
            pages.append({
                'path': relative, 'issue_numbers': [e['number'] for e in subset],
                'lowest_observed_gbp': min(amounts) if amounts else None,
                'oldest_created_at': min(e['created_at'] for e in subset),
                'contains_unknown_price': any(e.get('lowest_observed_gbp') is None for e in subset),
            })
        def ref(entry):
            return {'number': entry['number'], 'url': entry['url'],
                    'page': next(p['path'] for p in pages if entry['number'] in p['issue_numbers'])}
        affordable = [e for e in entries if e.get('lowest_observed_gbp') is not None
                      and e['lowest_observed_gbp'] <= 150]
        oldest = min(affordable, key=lambda e: e['created_at'], default=None)
        manifest['queues'][source] = {
            'issue_count': len(entries), 'pages': pages,
            'oldest_affordable': ref(oldest) if oldest else None,
            'freshest_issues': [ref(e) for e in sorted(entries, key=lambda e: e['created_at'], reverse=True)[:10]],
        }
    atomic_json(runtime / 'handoff.json', manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path('runtime/review-health'))
    args = parser.parse_args()
    index = json.loads((args.runtime / 'index.json').read_text(encoding='utf-8'))
    manifest = publish(index, args.runtime)
    print('Review handoff pages:', sum(len(q['pages']) for q in manifest['queues'].values()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
