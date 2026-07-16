# GitHub Trending Daily Skill

A reliable Codex skill that discovers, deduplicates, analyzes, and archives notable GitHub repositories as a compact daily Chinese intelligence report.

> GitHub does not provide an official Trending API. This project uses the official GitHub Repository Search API to build transparent, Search-based candidates; it does not reproduce the ranking on `github.com/trending`.

## 中文简介

自动发现、去重并分析近期受到关注的 GitHub 项目，结合 Star 快照、README、提交和 Release 信号，生成适合快速阅读的中文开源生态日报。

## Features

- Discovers recently created and recently active public repositories.
- Uses a default floor of 300 Stars for new-project candidates.
- Deduplicates repository names case-insensitively and preserves dated Star snapshots.
- Skips previously reported repositories unless they qualify as updated, resurfaced, or major-release candidates.
- Enriches reportable repositories with README, topics, license, recent commits, and release evidence.
- Supports checkpoints and resumable runs when requests fail or API limits are reached.
- Handles authentication fallback, rate-limit headers, transient retries, and ETag caching.
- Validates every report before publication.
- Treats repository content as untrusted evidence and never follows instructions found in README files.
- Uses only the Python standard library.

## How It Works

```text
GitHub Repository Search API
        ↓
Merge and deduplicate candidates
        ↓
SQLite history and Star snapshots
        ↓
Classify new / updated / resurfaced projects
        ↓
Fetch repository evidence with checkpoints
        ↓
Generate and validate the Chinese daily report
        ↓
Publish the dated report and finalize state
```

## Requirements

- Python 3.9 or newer
- Network access to `https://api.github.com`
- Optional GitHub authentication for higher API capacity

No third-party Python packages are required.

## Installation

Clone the repository into your Codex skills directory:

```bash
git clone git@github.com:guojunxuan6-boop/github-trending-daily-skill.git \
  ~/.codex/skills/github-trending-daily
```

Alternatively, clone over HTTPS:

```bash
git clone https://github.com/guojunxuan6-boop/github-trending-daily-skill.git \
  ~/.codex/skills/github-trending-daily
```

Restart or refresh Codex if the skill is not discovered immediately.

## Authentication

Credentials are resolved automatically in this order:

1. `GITHUB_TOKEN`
2. `GH_TOKEN`
3. The credential stored by `gh auth login`
4. Anonymous GitHub API access

Do not paste access tokens into prompts or commit them to the repository. Anonymous execution is supported, but the report detail limit may be reduced automatically according to the measured API budget.

## Usage

Invoke the skill in Codex:

```text
$github-trending-daily
```

Or run the workflow directly:

```bash
cd ~/.codex/skills/github-trending-daily
python3 scripts/daily.py run
```

Useful commands:

```bash
# Inspect local state
python3 scripts/daily.py status

# Resume the latest interrupted run
python3 scripts/daily.py run --resume latest

# Override the report limit or minimum Stars
python3 scripts/daily.py run --limit 10 --min-stars 500

# Import a legacy JSON history
python3 scripts/daily.py migrate --from-json /path/to/repositories.json

# Validate an existing report against evidence
python3 scripts/daily.py validate REPORT.md \
  --evidence EVIDENCE.json \
  --date YYYY-MM-DD
```

## Default Candidate Policy

| Candidate stream | Default rule |
| --- | --- |
| New repositories | Created within 7 days and at least 300 Stars |
| Active repositories | Pushed within 2 days and at least 500 Stars |
| Authenticated detail limit | Up to 20 repositories |
| Anonymous detail limit | Up to 10, reduced when the API budget is low |
| Resurfacing window | 30 days since the last report |

The Search result is only a candidate set. Previously reported repositories do not reappear in the daily report unless their stored signals satisfy the update policy.

## State and Reports

Mutable state is stored outside the installed skill by default:

```text
${XDG_DATA_HOME:-~/.local/share}/github-trending-daily/
├── state.sqlite3
├── cache/
└── reports/
    └── YYYY/MM/github_trending_report-YYYY-MM-DD.md
```

Each report includes a timezone-aware generation timestamp. A second successful run on the same date replaces that date's report; reports from other dates remain unchanged. The workflow does not create a duplicate `latest.md`.

Use `--state-dir PATH` when a different or sandbox-writable state location is required.

## Repository Layout

```text
github-trending-daily-skill/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   ├── analyzer.py
│   ├── daily.py
│   ├── github_client.py
│   ├── github_repository.py
│   ├── github_search.py
│   ├── memory.py
│   ├── state.py
│   └── validator.py
└── tests/
    └── test_reliability.py
```

## Testing

Run the offline reliability suite:

```bash
PYTHONPATH=scripts python3 -m unittest discover -s tests -v
```

The suite covers authentication fallback, retry and rate-limit behavior, ETag caching, SQLite migration, daily Star baselines, report validation, publication behavior, resumable checkpoints, and untrusted README handling.

## License

Released under the [MIT License](LICENSE).
