#!/usr/bin/env python3
"""Filter and atomically update the repository history database."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple


def today_iso() -> str:
    return date.today().isoformat()


def load(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"history database must contain a JSON array: {path}")
    return data


def payload_items(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {}, payload
    return payload, payload.get("items", [])


def atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def filter_command(args: argparse.Namespace) -> None:
    history = load(args.database)
    known = {record.get("repo", "").casefold() for record in history}
    envelope, items = payload_items(args.input)
    fresh = []
    for item in items:
        key = item.get("repo", "").casefold()
        if key and key not in known:
            fresh.append(item)
            known.add(key)
    output = dict(envelope)
    output["items"] = fresh
    output["filtered_existing"] = len(items) - len(fresh)
    atomic_write(args.output, output)
    print(f"kept {len(fresh)} new repositories; skipped {len(items) - len(fresh)} existing")


def commit_command(args: argparse.Namespace) -> None:
    history = load(args.database)
    _, items = payload_items(args.input)
    by_repo = {record.get("repo", "").casefold(): record for record in history}
    today = args.date or today_iso()
    for item in items:
        key = item["repo"].casefold()
        previous = by_repo.get(key)
        if previous:
            previous.update({"github_url": item.get("github_url"), "last_analyzed": today, "last_star": item.get("stars", 0), "status": "analyzed"})
        else:
            record = {
                "repo": item["repo"],
                "github_url": item.get("github_url", f"https://github.com/{item['repo']}"),
                "first_seen": today,
                "last_analyzed": today,
                "last_star": item.get("stars", 0),
                "status": "analyzed",
            }
            history.append(record)
            by_repo[key] = record
    history.sort(key=lambda record: (record.get("first_seen", ""), record.get("repo", "").casefold()))
    atomic_write(args.database, history)
    print(f"history now contains {len(history)} repositories")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    filter_parser = subparsers.add_parser("filter", help="Remove repositories already in history")
    filter_parser.add_argument("--database", type=Path, required=True)
    filter_parser.add_argument("--input", type=Path, required=True)
    filter_parser.add_argument("--output", type=Path, required=True)
    filter_parser.set_defaults(handler=filter_command)
    commit_parser = subparsers.add_parser("commit", help="Record successfully analyzed repositories")
    commit_parser.add_argument("--database", type=Path, required=True)
    commit_parser.add_argument("--input", type=Path, required=True)
    commit_parser.add_argument("--date", help="Override first-seen date (YYYY-MM-DD)")
    commit_parser.set_defaults(handler=commit_command)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
