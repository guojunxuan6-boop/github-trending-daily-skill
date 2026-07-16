#!/usr/bin/env python3
"""Run, resume, inspect, validate, and migrate GitHub Trending Daily."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from analyzer import build_analysis_packet, render
from github_client import GitHubClient, GitHubClientError, RateLimitError
from github_repository import enrich_repository
from github_search import normalize
from state import StateStore, utc_now
from validator import validate_report


def default_state_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME")
    return Path(root) / "github-trending-daily" if root else Path.home() / ".local" / "share" / "github-trending-daily"


def merge_candidates(streams: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for source, items in streams.items():
        for raw in items:
            item = normalize(raw) if "full_name" in raw else dict(raw)
            key = item["repo"].casefold()
            if key not in merged:
                item["selection_sources"] = []
                merged[key] = item
            if source not in merged[key]["selection_sources"]:
                merged[key]["selection_sources"].append(source)
    return list(merged.values())


def discover(client: GitHubClient, args: argparse.Namespace) -> tuple:
    today = date.fromisoformat(args.date)
    if args.query:
        streams = {"custom": client.search_repositories(args.query, args.limit * 2)}
        queries = {"custom": args.query}
    else:
        new_since = (today - timedelta(days=args.new_window)).isoformat()
        active_since = (today - timedelta(days=args.active_window)).isoformat()
        queries = {
            "new": f"created:>={new_since} stars:>={args.min_stars} archived:false is:public",
            "active": f"pushed:>={active_since} stars:>={args.active_min_stars} archived:false is:public",
        }
        streams = {
            name: client.search_repositories(query, args.limit * 2)
            for name, query in queries.items()
        }
    return merge_candidates(streams), queries


def effective_limit(client: GitHubClient, requested: int, anonymous_limit: int) -> tuple:
    try:
        rate = client.rate_limit()
    except GitHubClientError:
        rate = client.last_rate
    if rate.remaining is None:
        return (requested if client.auth_mode != "anonymous" else min(requested, anonymous_limit)), rate
    budget_limit = max(1, (rate.remaining - 2) // 4)
    if client.auth_mode != "anonymous":
        return min(requested, budget_limit), rate
    return min(requested, anonymous_limit, budget_limit), rate


def prepare_run(store: StateStore, client: GitHubClient, args: argparse.Namespace) -> str:
    if args.resume:
        run_id = store.latest_resumable_run() if args.resume == "latest" else args.resume
        if not run_id or not store.run(run_id):
            raise ValueError("no resumable run found")
        return run_id

    candidates, queries = discover(client, args)
    run_id = store.create_run(args.date, client.auth_mode, {"queries": queries})
    captured_at = utc_now()
    ranked = sorted(candidates, key=lambda item: int(item.get("stars", 0)), reverse=True)
    prepared = []
    for rank, item in enumerate(ranked, 1):
        store.upsert_repository(item, args.date)
        growth = store.add_snapshot(
            item,
            captured_at,
            rank,
            ",".join(item.get("selection_sources", [])),
            snapshot_date=args.date,
        )
        status = store.classify(item["repo"], args.date, growth, int(item.get("stars", 0)))
        if status == "new" and "new" not in item.get("selection_sources", []) and "custom" not in item.get("selection_sources", []):
            status = "unchanged"
        item["star_growth"] = growth
        item["report_status"] = status
        priority = {
            "new": 0,
            "updated": 1,
            "resurfaced": 2,
            "unchanged": 3,
        }.get(status, 4)
        prepared.append((priority, -int(growth or 0), -int(item.get("stars", 0)), item))
    limit, rate = effective_limit(client, args.limit, args.anonymous_limit)
    selected = sorted(prepared, key=lambda value: value[:3])[:limit]
    for _, _, _, item in selected:
        reason = item["report_status"]
        if reason == "unchanged":
            reason = "check-release"
        store.add_run_item(run_id, item["repo"], reason, evidence=item)
    if client.auth_mode == "anonymous" and limit < args.limit:
        print(
            f"anonymous mode: detail limit reduced from {args.limit} to {limit}; "
            f"core remaining={rate.remaining if rate.remaining is not None else 'unknown'}",
            file=sys.stderr,
        )
    return run_id


def publish_report(state_dir: Path, report_date: str, text: str) -> tuple:
    report_dir = state_dir / "reports" / report_date[:4] / report_date[5:7]
    report_dir.mkdir(parents=True, exist_ok=True)
    archive = report_dir / f"github_trending_report-{report_date}.md"
    fd, temporary_name = tempfile.mkstemp(prefix=".daily-report-", suffix=".md", dir=report_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, archive)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return archive, digest


def run_command(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    database_existed = (state_dir / "state.sqlite3").exists()
    client = GitHubClient(state_dir / "cache")
    with StateStore(state_dir / "state.sqlite3") as store:
        default_legacy = Path(__file__).resolve().parents[1] / "data" / "repositories.json"
        legacy_json = args.legacy_json or default_legacy
        if not database_existed and legacy_json.exists():
            migrated = store.migrate_json(legacy_json)
            print(f"migrated {migrated} legacy records")
        run_id = prepare_run(store, client, args)
        run = store.run(run_id)
        report_date = run["report_date"]
        paused = False
        for row in store.pending_items(run_id):
            seed = json.loads(row["evidence_json"]) if row.get("evidence_json") else {
                "repo": row["display_repo"],
                "github_url": row["github_url"],
            }
            print(f"enriching {seed['repo']}")
            try:
                enriched = enrich_repository(client, seed, args.max_readme_chars)
                release_tag = (enriched.get("latest_release") or {}).get("tag")
                enriched["report_status"] = store.classify(
                    enriched["repo"],
                    report_date,
                    enriched.get("star_growth"),
                    int(enriched.get("stars", 0)),
                    release_tag,
                )
                enriched["analysis"] = build_analysis_packet(enriched)
                checkpoint_status = "partial" if enriched.get("data_quality") == "partial" else "complete"
                store.checkpoint(run_id, enriched["repo"], checkpoint_status, enriched)
            except RateLimitError as exc:
                store.checkpoint(run_id, seed["repo"], "failed", seed, "rate_limit")
                store.pause_run(run_id)
                reset = f" reset={exc.reset_at}" if exc.reset_at else ""
                print(f"rate limited; resume with --resume {run_id}.{reset}", file=sys.stderr)
                paused = True
                break
            except GitHubClientError as exc:
                store.checkpoint(run_id, seed["repo"], "failed", seed, type(exc).__name__)
                print(f"failed {seed['repo']}: {exc}", file=sys.stderr)
        if paused:
            return 2

        evidence = store.evidence_items(run_id)
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        report = render(evidence, report_date, generated_at)
        errors, warnings = validate_report(report, evidence, report_date)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if errors:
            draft = state_dir / "runs" / run_id / "invalid-report.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(report, encoding="utf-8")
            print("report validation failed: " + "; ".join(errors), file=sys.stderr)
            store.pause_run(run_id)
            return 3
        archive, digest = publish_report(state_dir, report_date, report)
        store.finalize_report(run_id, report_date, archive, digest, evidence)
        print(f"report={archive}")
        print(f"run_id={run_id}")
        print(f"items={len(evidence)}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    with StateStore(args.state_dir.expanduser() / "state.sqlite3") as store:
        print(json.dumps(store.status_summary(), ensure_ascii=False, indent=2))
    return 0


def migrate_command(args: argparse.Namespace) -> int:
    with StateStore(args.state_dir.expanduser() / "state.sqlite3") as store:
        print(f"migrated={store.migrate_json(args.from_json)}")
    return 0


def validate_command(args: argparse.Namespace) -> int:
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    evidence = payload if isinstance(payload, list) else payload.get("items", [])
    errors, warnings = validate_report(args.report.read_text(encoding="utf-8"), evidence, args.date)
    for value in warnings:
        print(f"warning: {value}")
    for value in errors:
        print(f"error: {value}")
    return 1 if errors else 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run or resume the complete daily workflow")
    add_common(run)
    run.add_argument("--date", default=date.today().isoformat())
    run.add_argument("--limit", type=int, default=20)
    run.add_argument("--anonymous-limit", type=int, default=10)
    run.add_argument("--resume", nargs="?", const="latest")
    run.add_argument("--query")
    run.add_argument("--new-window", type=int, default=7)
    run.add_argument("--active-window", type=int, default=2)
    run.add_argument("--min-stars", type=int, default=300)
    run.add_argument("--active-min-stars", type=int, default=500)
    run.add_argument("--max-readme-chars", type=int, default=20000)
    run.add_argument("--legacy-json", type=Path)
    run.set_defaults(handler=run_command)
    status = sub.add_parser("status", help="Show local state summary")
    add_common(status)
    status.set_defaults(handler=status_command)
    migrate = sub.add_parser("migrate", help="Import legacy repositories.json")
    add_common(migrate)
    migrate.add_argument("--from-json", type=Path, required=True)
    migrate.set_defaults(handler=migrate_command)
    validate = sub.add_parser("validate", help="Validate a report against an evidence JSON file")
    validate.add_argument("report", type=Path)
    validate.add_argument("--evidence", type=Path, required=True)
    validate.add_argument("--date", required=True)
    validate.set_defaults(handler=validate_command)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return args.handler(args)
    except (GitHubClientError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
