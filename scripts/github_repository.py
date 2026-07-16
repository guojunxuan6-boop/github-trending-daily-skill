#!/usr/bin/env python3
"""Enrich repositories with checkpoint-friendly GitHub evidence."""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from github_client import GitHubClient, GitHubClientError, RateLimitError


def sanitize_untrusted(text: str, max_chars: int = 20000) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    return text[:max_chars]


def decode_readme(payload: Any, max_chars: int) -> str:
    if not isinstance(payload, dict):
        return ""
    encoded = str(payload.get("content", "")).replace("\n", "")
    try:
        text = base64.b64decode(encoded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""
    return sanitize_untrusted(text, max_chars)


def optional(client: GitHubClient, path: str, params: Optional[Dict[str, Any]], ttl: int) -> Tuple[Any, Optional[str]]:
    try:
        return client.request_json(path, params, cache_ttl=ttl), None
    except RateLimitError:
        raise
    except GitHubClientError as exc:
        if "HTTP 404" in str(exc) or "HTTP 409" in str(exc):
            return None, None
        return None, type(exc).__name__


def enrich_repository(client: GitHubClient, item: Dict[str, Any], max_readme_chars: int = 20000) -> Dict[str, Any]:
    repo = item["repo"]
    errors: List[str] = []
    metadata = client.request_json(f"/repos/{repo}", cache_ttl=21600)
    commits, error = optional(client, f"/repos/{repo}/commits", {"per_page": 5}, 21600)
    if error:
        errors.append(f"commits:{error}")
    release, error = optional(client, f"/repos/{repo}/releases/latest", None, 21600)
    if error:
        errors.append(f"release:{error}")
    readme_payload, error = optional(client, f"/repos/{repo}/readme", None, 86400)
    if error:
        errors.append(f"readme:{error}")
    commits = commits or []
    enriched = dict(item)
    enriched.update(
        {
            "description": metadata.get("description") or item.get("description", ""),
            "stars": int(metadata.get("stargazers_count", item.get("stars", 0))),
            "forks": int(metadata.get("forks_count", item.get("forks", 0))),
            "language": metadata.get("language") or item.get("language") or "Unknown",
            "topics": metadata.get("topics") or item.get("topics") or [],
            "license": (metadata.get("license") or {}).get("spdx_id") or item.get("license"),
            "updated_at": metadata.get("updated_at") or item.get("updated_at"),
            "pushed_at": metadata.get("pushed_at") or item.get("pushed_at"),
            "homepage": metadata.get("homepage") or "",
            "readme": decode_readme(readme_payload, max_readme_chars),
            "recent_commits": [
                {
                    "sha": commit.get("sha", "")[:12],
                    "date": (((commit.get("commit") or {}).get("author") or {}).get("date")),
                    "message": sanitize_untrusted(((commit.get("commit") or {}).get("message") or "").splitlines()[0], 240),
                    "author": ((commit.get("author") or {}).get("login")),
                }
                for commit in commits
            ],
            "latest_release": (
                {
                    "tag": release.get("tag_name"),
                    "name": sanitize_untrusted(release.get("name") or "", 200),
                    "published_at": release.get("published_at"),
                    "url": release.get("html_url"),
                }
                if release
                else None
            ),
            "source_urls": [f"https://github.com/{repo}"],
            "data_quality": "partial" if errors else "complete",
            "retrieval_errors": errors,
            "untrusted_content": True,
        }
    )
    return enriched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/github-trending-daily"))
    parser.add_argument("--max-readme-chars", type=int, default=20000)
    parser.add_argument("--resume", action="store_true", help="Reuse completed items already present in output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("items", [])
    existing: Dict[str, Dict[str, Any]] = {}
    if args.resume and args.output.exists():
        old = json.loads(args.output.read_text(encoding="utf-8"))
        existing = {value["repo"].casefold(): value for value in old.get("items", [])}
    client = GitHubClient(args.cache_dir)
    result = {
        "generated_at": None if isinstance(payload, list) else payload.get("generated_at"),
        "query": None if isinstance(payload, list) else payload.get("query"),
        "auth_mode": client.auth_mode,
        "items": list(existing.values()),
        "errors": [],
    }
    completed = set(existing)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(items, 1):
        if item["repo"].casefold() in completed:
            continue
        print(f"enriching {index}/{len(items)}: {item['repo']}")
        try:
            result["items"].append(enrich_repository(client, item, args.max_readme_chars))
        except GitHubClientError as exc:
            result["errors"].append({"repo": item["repo"], "error": type(exc).__name__, "message": str(exc)})
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if isinstance(exc, RateLimitError):
                print(f"paused at {item['repo']}: {exc}")
                return 2
            continue
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(result['items'])} enriched repositories to {args.output}")
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
