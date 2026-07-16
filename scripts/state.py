#!/usr/bin/env python3
"""SQLite state, snapshots, resumable runs, migration, and report records."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_repo(repo: str) -> str:
    return repo.strip().casefold()


def semver_major(tag: Optional[str]) -> Optional[int]:
    if not tag:
        return None
    value = tag.strip().lstrip("vV")
    first = value.split(".", 1)[0]
    return int(first) if first.isdigit() else None


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        try:
            self.db.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        self._schema()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS repositories (
                repo TEXT PRIMARY KEY,
                display_repo TEXT NOT NULL,
                github_url TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_reported TEXT,
                status TEXT NOT NULL DEFAULT 'observed',
                last_release TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL REFERENCES repositories(repo) ON DELETE CASCADE,
                captured_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                stars INTEGER NOT NULL,
                forks INTEGER NOT NULL,
                rank INTEGER,
                source TEXT NOT NULL,
                UNIQUE(repo, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS snapshots_repo_time ON snapshots(repo, captured_at DESC);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                report_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                auth_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                query_metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_items (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                repo TEXT NOT NULL REFERENCES repositories(repo),
                selection_reason TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_category TEXT,
                evidence_json TEXT,
                PRIMARY KEY(run_id, repo)
            );
            CREATE TABLE IF NOT EXISTS reports (
                report_date TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        snapshot_columns = {row[1] for row in self.db.execute("PRAGMA table_info(snapshots)").fetchall()}
        if "snapshot_date" not in snapshot_columns:
            self.db.execute("ALTER TABLE snapshots ADD COLUMN snapshot_date TEXT")
            self.db.execute("UPDATE snapshots SET snapshot_date=substr(captured_at, 1, 10) WHERE snapshot_date IS NULL")
        # Schema v1 allowed several captures on the same day. Keep the newest one
        # before enforcing the daily baseline used for Star-growth calculations.
        self.db.execute(
            """DELETE FROM snapshots
               WHERE id NOT IN (
                   SELECT MAX(id) FROM snapshots GROUP BY repo, snapshot_date
               )"""
        )
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS snapshots_repo_date ON snapshots(repo, snapshot_date)")
        self.db.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.db.commit()

    def migrate_json(self, source: Path) -> int:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("legacy history must be a JSON array")
        records = []
        for index, item in enumerate(data):
            if not isinstance(item, dict) or not item.get("repo"):
                raise ValueError(f"invalid legacy record at index {index}")
            records.append(item)
        with self.db:
            for item in records:
                repo = normalize_repo(item["repo"])
                first_seen = item.get("first_seen") or date.today().isoformat()
                last_seen = item.get("last_analyzed") or first_seen
                url = item.get("github_url") or f"https://github.com/{item['repo']}"
                self.db.execute(
                    """INSERT INTO repositories(repo, display_repo, github_url, first_seen, last_seen, last_reported, status)
                       VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(repo) DO NOTHING""",
                    (repo, item["repo"], url, first_seen, last_seen, last_seen, item.get("status") or "analyzed"),
                )
                if item.get("last_star") is not None:
                    captured = f"{last_seen}T00:00:00+00:00"
                    self.db.execute(
                        "INSERT OR IGNORE INTO snapshots(repo, captured_at, snapshot_date, stars, forks, rank, source) VALUES(?, ?, ?, ?, 0, NULL, 'legacy')",
                        (repo, captured, last_seen, int(item.get("last_star", 0))),
                    )
            self.db.execute(
                "INSERT INTO events(event_type, created_at, details_json) VALUES('legacy_migration', ?, ?)",
                (utc_now(), json.dumps({"source": str(source), "count": len(records)})),
            )
        return len(records)

    def create_run(self, report_date: str, auth_mode: str, query_metadata: Dict[str, Any]) -> str:
        run_id = f"{report_date}-{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO runs(run_id, report_date, started_at, auth_mode, status, query_metadata_json) VALUES(?, ?, ?, ?, 'running', ?)",
            (run_id, report_date, utc_now(), auth_mode, json.dumps(query_metadata, ensure_ascii=False)),
        )
        self.db.commit()
        return run_id

    def latest_resumable_run(self) -> Optional[str]:
        row = self.db.execute(
            "SELECT run_id FROM runs WHERE status IN ('running', 'paused', 'failed') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None

    def run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def upsert_repository(self, item: Dict[str, Any], seen_date: str) -> str:
        key = normalize_repo(item["repo"])
        url = item.get("github_url") or f"https://github.com/{item['repo']}"
        self.db.execute(
            """INSERT INTO repositories(repo, display_repo, github_url, first_seen, last_seen, status)
               VALUES(?, ?, ?, ?, ?, 'observed')
               ON CONFLICT(repo) DO UPDATE SET display_repo=excluded.display_repo, github_url=excluded.github_url, last_seen=excluded.last_seen""",
            (key, item["repo"], url, seen_date, seen_date),
        )
        return key

    def prior_snapshot(self, repo: str, before_date: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            "SELECT * FROM snapshots WHERE repo=? AND snapshot_date < ? ORDER BY snapshot_date DESC, captured_at DESC LIMIT 1",
            (normalize_repo(repo), before_date),
        ).fetchone()
        return dict(row) if row else None

    def add_snapshot(
        self,
        item: Dict[str, Any],
        captured_at: str,
        rank: Optional[int],
        source: str,
        snapshot_date: Optional[str] = None,
    ) -> Optional[int]:
        key = normalize_repo(item["repo"])
        snapshot_day = snapshot_date or captured_at[:10]
        previous = self.prior_snapshot(key, snapshot_day)
        self.db.execute(
            """INSERT INTO snapshots(repo, captured_at, snapshot_date, stars, forks, rank, source)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo, snapshot_date) DO UPDATE SET captured_at=excluded.captured_at, stars=excluded.stars,
                 forks=excluded.forks, rank=excluded.rank, source=excluded.source""",
            (key, captured_at, snapshot_day, int(item.get("stars", 0)), int(item.get("forks", 0)), rank, source),
        )
        self.db.commit()
        return int(item.get("stars", 0)) - int(previous["stars"]) if previous else None

    def repository(self, repo: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM repositories WHERE repo=?", (normalize_repo(repo),)).fetchone()
        return dict(row) if row else None

    def classify(
        self,
        repo: str,
        report_date: str,
        star_growth: Optional[int],
        current_stars: int,
        release_tag: Optional[str] = None,
        growth_absolute: int = 100,
        growth_relative: float = 0.20,
        resurfacing_days: int = 30,
    ) -> str:
        record = self.repository(repo)
        if not record or not record.get("last_reported"):
            return "new"
        if release_tag and semver_major(release_tag) is not None:
            old_major = semver_major(record.get("last_release"))
            if old_major is not None and semver_major(release_tag) > old_major:
                return "major-release"
        if star_growth is not None:
            previous = max(1, current_stars - star_growth)
            if star_growth >= growth_absolute or star_growth / previous >= growth_relative:
                return "updated"
        last = date.fromisoformat(record["last_reported"])
        if (date.fromisoformat(report_date) - last).days >= resurfacing_days:
            return "resurfaced"
        return "unchanged"

    def add_run_item(
        self,
        run_id: str,
        repo: str,
        reason: str,
        status: str = "pending",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO run_items(run_id, repo, selection_reason, status, evidence_json)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(run_id, repo) DO UPDATE SET selection_reason=excluded.selection_reason,
                 evidence_json=COALESCE(run_items.evidence_json, excluded.evidence_json)""",
            (run_id, normalize_repo(repo), reason, status, json.dumps(evidence, ensure_ascii=False) if evidence else None),
        )
        self.db.commit()

    def checkpoint(self, run_id: str, repo: str, status: str, evidence: Optional[Dict[str, Any]], error: Optional[str] = None) -> None:
        self.db.execute(
            """UPDATE run_items SET status=?, attempts=attempts+1, error_category=?, evidence_json=?
               WHERE run_id=? AND repo=?""",
            (status, error, json.dumps(evidence, ensure_ascii=False) if evidence is not None else None, run_id, normalize_repo(repo)),
        )
        self.db.commit()

    def pending_items(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            """SELECT i.*, r.display_repo, r.github_url
               FROM run_items i JOIN repositories r ON r.repo=i.repo
               WHERE i.run_id=? AND i.status IN ('pending', 'partial', 'failed') ORDER BY i.rowid""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def evidence_items(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT evidence_json FROM run_items WHERE run_id=? AND status IN ('complete', 'partial') AND evidence_json IS NOT NULL ORDER BY rowid",
            (run_id,),
        ).fetchall()
        values = [json.loads(row["evidence_json"]) for row in rows]
        return [value for value in values if value.get("report_status") != "unchanged"]

    def pause_run(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET status='paused' WHERE run_id=?", (run_id,))
        self.db.commit()

    def finalize_report(self, run_id: str, report_date: str, path: Path, sha256: str, items: Iterable[Dict[str, Any]]) -> None:
        values = list(items)
        with self.db:
            self.db.execute(
                """INSERT INTO reports(report_date, run_id, path, sha256, item_count, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(report_date) DO UPDATE SET run_id=excluded.run_id, path=excluded.path, sha256=excluded.sha256,
                     item_count=excluded.item_count, created_at=excluded.created_at""",
                (report_date, run_id, str(path), sha256, len(values), utc_now()),
            )
            for item in values:
                self.db.execute(
                    "UPDATE repositories SET last_reported=?, status=?, last_release=COALESCE(?, last_release) WHERE repo=?",
                    (report_date, item.get("report_status", "analyzed"), (item.get("latest_release") or {}).get("tag"), normalize_repo(item["repo"])),
                )
            self.db.execute("UPDATE runs SET status='complete', completed_at=? WHERE run_id=?", (utc_now(), run_id))

    def status_summary(self) -> Dict[str, Any]:
        counts = self.db.execute("SELECT status, COUNT(*) count FROM runs GROUP BY status").fetchall()
        return {
            "database": str(self.path),
            "repositories": self.db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0],
            "snapshots": self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "reports": self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
            "runs": {row["status"]: row["count"] for row in counts},
        }
