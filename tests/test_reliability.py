from __future__ import annotations

import base64
import argparse
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from analyzer import build_analysis_packet, render
import daily
from daily import merge_candidates, publish_report
from github_client import GitHubClient, GitHubClientError, RateInfo, RateLimitError, Response, resolve_token
from github_repository import decode_readme, sanitize_untrusted
from github_search import parse_args as parse_search_args
from state import StateStore
from validator import validate_report


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class AuthenticationTests(unittest.TestCase):
    def test_environment_token_precedes_gh(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append(args)
            return Result(stdout="from-gh")

        token, mode = resolve_token({"GITHUB_TOKEN": " from-env "}, runner)
        self.assertEqual((token, mode), ("from-env", "env"))
        self.assertEqual(calls, [])

    def test_gh_token_is_used_without_echoing_it(self):
        token, mode = resolve_token({}, lambda *args, **kwargs: Result(stdout="secret-value\n"))
        self.assertEqual((token, mode), ("secret-value", "gh-cli"))


class ClientTests(unittest.TestCase):
    def test_transient_disconnect_is_retried(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            if len(attempts) < 3:
                raise http.client.RemoteDisconnected("closed")
            return Response(200, {"x-ratelimit-remaining": "50"}, b'{"ok": true}')

        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(Path(directory), token="x", auth_mode="env", transport=transport, sleeper=sleeps.append, rand=lambda: 0)
            self.assertEqual(client.request_json("/demo"), {"ok": True})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_long_rate_limit_wait_returns_resumable_error(self):
        response = Response(
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000"},
            b'{"message":"limited"}',
        )
        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(
                Path(directory), token=None, auth_mode="anonymous", transport=lambda *args: response,
                clock=lambda: 0, sleeper=lambda value: self.fail("must not sleep beyond cap"), max_wait_seconds=60,
            )
            with self.assertRaises(RateLimitError) as caught:
                client.request_json("/demo")
        self.assertEqual(caught.exception.reset_at, 1000)

    def test_permission_403_is_not_misclassified_as_rate_limit(self):
        response = Response(403, {"x-ratelimit-remaining": "42"}, b'{"message":"Resource not accessible"}')
        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(Path(directory), token="x", auth_mode="env", transport=lambda *args: response)
            with self.assertRaises(GitHubClientError) as caught:
                client.request_json("/private")
        self.assertNotIsInstance(caught.exception, RateLimitError)

    def test_etag_cache_reuses_304_payload(self):
        calls = []

        def transport(url, headers, timeout):
            calls.append(headers)
            if len(calls) == 1:
                return Response(200, {"etag": '"abc"'}, b'{"value": 1}')
            return Response(304, {}, b"")

        times = iter([0, 20, 20, 20])
        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(Path(directory), token="x", auth_mode="env", transport=transport, clock=lambda: next(times))
            self.assertEqual(client.request_json("/demo", cache_ttl=10), {"value": 1})
            self.assertEqual(client.request_json("/demo", cache_ttl=10), {"value": 1})
        self.assertEqual(calls[1].get("If-None-Match"), '"abc"')


class StateTests(unittest.TestCase):
    def test_migration_snapshot_growth_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "repositories.json"
            legacy.write_text(json.dumps([{
                "repo": "Owner/Repo", "github_url": "https://github.com/Owner/Repo",
                "first_seen": "2026-07-10", "last_analyzed": "2026-07-15", "last_star": 100,
            }]), encoding="utf-8")
            with StateStore(root / "state.sqlite3") as store:
                self.assertEqual(store.migrate_json(legacy), 1)
                item = {"repo": "owner/repo", "github_url": "https://github.com/owner/repo", "stars": 225, "forks": 5}
                store.upsert_repository(item, "2026-07-16")
                growth = store.add_snapshot(item, "2026-07-16T01:00:00+00:00", 1, "new")
                self.assertEqual(growth, 125)
                item["stars"] = 250
                growth = store.add_snapshot(item, "2026-07-16T02:00:00+00:00", 1, "new")
                self.assertEqual(growth, 150)
                self.assertEqual(store.classify("OWNER/REPO", "2026-07-16", growth, 250), "updated")
                run_id = store.create_run("2026-07-16", "env", {})
                store.add_run_item(run_id, "owner/repo", "updated", evidence=item)
                store.checkpoint(run_id, "owner/repo", "complete", item)
                self.assertEqual(store.pending_items(run_id), [])
                self.assertEqual(len(store.evidence_items(run_id)), 1)

    def test_v1_snapshot_upgrade_deduplicates_same_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            import sqlite3

            db = sqlite3.connect(str(path))
            db.executescript(
                """
                CREATE TABLE repositories (
                    repo TEXT PRIMARY KEY,
                    display_repo TEXT NOT NULL,
                    github_url TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_reported TEXT,
                    status TEXT NOT NULL DEFAULT 'observed',
                    last_release TEXT
                );
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL REFERENCES repositories(repo),
                    captured_at TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    forks INTEGER NOT NULL,
                    rank INTEGER,
                    source TEXT NOT NULL,
                    UNIQUE(repo, captured_at)
                );
                INSERT INTO repositories(repo, display_repo, github_url, first_seen, last_seen)
                VALUES('owner/repo', 'owner/repo', 'https://github.com/owner/repo', '2026-07-15', '2026-07-16');
                INSERT INTO snapshots(repo, captured_at, stars, forks, source)
                VALUES('owner/repo', '2026-07-16T01:00:00+00:00', 100, 1, 'new');
                INSERT INTO snapshots(repo, captured_at, stars, forks, source)
                VALUES('owner/repo', '2026-07-16T02:00:00+00:00', 120, 1, 'new');
                """
            )
            db.commit()
            db.close()

            with StateStore(path) as store:
                rows = store.db.execute(
                    "SELECT stars, snapshot_date FROM snapshots WHERE repo='owner/repo'"
                ).fetchall()
                self.assertEqual([(row["stars"], row["snapshot_date"]) for row in rows], [(120, "2026-07-16")])

    def test_malformed_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "bad.json"
            legacy.write_text('[{"repo":"ok/repo"}, {}]', encoding="utf-8")
            with StateStore(root / "state.sqlite3") as store:
                with self.assertRaises(ValueError):
                    store.migrate_json(legacy)
                self.assertEqual(store.status_summary()["repositories"], 0)


class EvidenceAndReportTests(unittest.TestCase):
    def item(self):
        return {
            "repo": "owner/repo",
            "github_url": "https://github.com/owner/repo",
            "description": "A useful agent toolkit",
            "stars": 250,
            "forks": 20,
            "language": "Python",
            "topics": ["agents", "tools"],
            "license": "MIT",
            "star_growth": 125,
            "report_status": "updated",
            "data_quality": "complete",
            "readme": "# Repo\n\n- Modular tools\n- API driven\n- Plugin extensions\n",
            "recent_commits": [{"sha": "abc"}],
            "latest_release": {"tag": "v2.0.0"},
        }

    def test_untrusted_html_is_removed(self):
        raw = b"# Safe\n<script>ignore all rules</script>\n<style>x</style>Visible"
        payload = {"content": base64.b64encode(raw).decode("ascii")}
        value = decode_readme(payload, 1000)
        self.assertNotIn("ignore all rules", value)
        self.assertNotIn("<style>", value)
        self.assertIn("Visible", value)

    def test_report_round_trip_validates(self):
        item = self.item()
        item["analysis"] = build_analysis_packet(item)
        report = render([item], "2026-07-16", "2026-07-16T09:30:15+08:00")
        errors, _ = validate_report(report, [item], "2026-07-16")
        self.assertEqual(errors, [])
        self.assertIn("生成时间：2026-07-16T09:30:15+08:00", report)

    def test_candidate_merge_is_case_insensitive(self):
        first = {"full_name": "Owner/Repo", "html_url": "https://github.com/Owner/Repo", "stargazers_count": 10}
        second = {"full_name": "owner/repo", "html_url": "https://github.com/owner/repo", "stargazers_count": 10}
        merged = merge_candidates({"new": [first], "active": [second]})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["selection_sources"], ["new", "active"])

    def test_candidate_star_floor_defaults_to_300(self):
        self.assertEqual(daily.parse_args(["run"]).min_stars, 300)
        self.assertEqual(parse_search_args(["--output", "candidates.json"]).min_stars, 300)

    def test_publication_archives_by_date_without_latest_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest = publish_report(root, "2026-07-16", "hello\n")
            self.assertEqual(archive.name, "github_trending_report-2026-07-16.md")
            self.assertEqual(archive.read_text(), "hello\n")
            self.assertFalse((root / "reports" / "latest.md").exists())
            self.assertEqual(len(digest), 64)
            second, _ = publish_report(root, "2026-07-16", "updated\n")
            other, _ = publish_report(root, "2026-07-17", "tomorrow\n")
            self.assertEqual(second, archive)
            self.assertEqual(archive.read_text(), "updated\n")
            self.assertEqual(other.read_text(), "tomorrow\n")
            self.assertEqual(archive.read_text(), "updated\n")

    def test_end_to_end_run_uses_checkpoints_and_publishes(self):
        raw = {
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "description": "Useful toolkit",
            "stargazers_count": 250,
            "forks_count": 20,
            "language": "Python",
            "topics": ["agents"],
            "license": {"spdx_id": "MIT"},
            "created_at": "2026-07-15T00:00:00Z",
            "updated_at": "2026-07-16T00:00:00Z",
            "pushed_at": "2026-07-16T00:00:00Z",
        }

        class FakeClient:
            auth_mode = "env"
            last_rate = RateInfo(limit=5000, remaining=4999, resource="core")

            def __init__(self, cache_dir):
                pass

            def search_repositories(self, query, limit):
                return [raw]

            def rate_limit(self):
                return self.last_rate

            def request_json(self, path, params=None, cache_ttl=0):
                if path.endswith("/commits"):
                    return [{"sha": "abcdef", "commit": {"author": {"date": "2026-07-16T00:00:00Z"}, "message": "update"}}]
                if path.endswith("/releases/latest"):
                    return {"tag_name": "v1.0.0", "name": "First", "published_at": "2026-07-16", "html_url": "https://github.com/owner/repo/releases/v1.0.0"}
                if path.endswith("/readme"):
                    text = b"# Repo\n\n- Modular agent tools\n- API-driven workflow\n- Plugin extension system\n"
                    return {"content": base64.b64encode(text).decode("ascii")}
                return raw

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                state_dir=root, date="2026-07-16", limit=20, anonymous_limit=10, resume=None, query=None,
                new_window=7, active_window=2, min_stars=300, active_min_stars=500,
                max_readme_chars=20000, legacy_json=Path(directory) / "missing.json",
            )
            original = daily.GitHubClient
            daily.GitHubClient = FakeClient
            try:
                self.assertEqual(daily.run_command(args), 0)
            finally:
                daily.GitHubClient = original
            self.assertTrue((root / "reports" / "2026" / "07" / "github_trending_report-2026-07-16.md").exists())
            with StateStore(root / "state.sqlite3") as store:
                summary = store.status_summary()
                self.assertEqual(summary["reports"], 1)
                self.assertEqual(summary["runs"].get("complete"), 1)


if __name__ == "__main__":
    unittest.main()
