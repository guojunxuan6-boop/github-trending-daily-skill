#!/usr/bin/env python3
"""Discover GitHub Trending candidates with the Repository Search API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from github_client import GitHubClient


def normalize(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "repo": item["full_name"],
        "github_url": item["html_url"],
        "description": item.get("description") or "",
        "stars": int(item.get("stargazers_count", 0)),
        "forks": int(item.get("forks_count", 0)),
        "language": item.get("language") or "Unknown",
        "topics": item.get("topics") or [],
        "license": (item.get("license") or {}).get("spdx_id"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "pushed_at": item.get("pushed_at"),
        "open_issues": int(item.get("open_issues_count", 0)),
        "archived": bool(item.get("archived", False)),
    }


def search(client: GitHubClient, query: str, limit: int) -> List[Dict[str, Any]]:
    return [normalize(item) for item in client.search_repositories(query, limit) if not item.get("archived")]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", help="Full GitHub repository search query; overrides generated query")
    parser.add_argument("--window-days", type=int, default=7, help="Look for repositories created in this window")
    parser.add_argument("--min-stars", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/github-trending-daily"))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.window_days < 1 or args.min_stars < 0 or not 1 <= args.limit <= 1000:
        raise SystemExit("window-days must be >= 1, min-stars >= 0, and limit between 1 and 1000")
    since = (datetime.now(timezone.utc) - timedelta(days=args.window_days)).date().isoformat()
    query = args.query or f"created:>={since} stars:>={args.min_stars} archived:false is:public"
    client = GitHubClient(args.cache_dir)
    if client.auth_mode == "anonymous":
        print("warning: no GitHub token found; unauthenticated API limits apply", file=sys.stderr)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "auth_mode": client.auth_mode,
        "items": search(client, query, args.limit),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(data['items'])} candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
