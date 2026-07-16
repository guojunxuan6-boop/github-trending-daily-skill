#!/usr/bin/env python3
"""Authenticated, cached, rate-limit-aware GitHub REST client."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubClientError(RuntimeError):
    """Base error safe to surface without exposing credentials."""


class RateLimitError(GitHubClientError):
    def __init__(self, message: str, reset_at: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes


@dataclass
class RateInfo:
    limit: Optional[int] = None
    remaining: Optional[int] = None
    used: Optional[int] = None
    reset: Optional[int] = None
    resource: Optional[str] = None
    retry_after: Optional[int] = None


def resolve_token(env: Optional[Dict[str, str]] = None, runner: Callable[..., Any] = subprocess.run) -> Tuple[Optional[str], str]:
    values = os.environ if env is None else env
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = values.get(name)
        if token and token.strip():
            return token.strip(), "env"
    try:
        result = runner(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None, "anonymous"
    token = result.stdout.strip() if result.returncode == 0 else ""
    return (token, "gh-cli") if token else (None, "anonymous")


def default_transport(url: str, headers: Dict[str, str], timeout: int) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())


class GitHubClient:
    def __init__(
        self,
        cache_dir: Path,
        token: Optional[str] = None,
        auth_mode: Optional[str] = None,
        transport: Callable[[str, Dict[str, str], int], Response] = default_transport,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
        max_attempts: int = 3,
        max_wait_seconds: int = 60,
        timeout: int = 30,
    ):
        if token is None and auth_mode is None:
            token, auth_mode = resolve_token()
        self.token = token
        self.auth_mode = auth_mode or ("env" if token else "anonymous")
        self.cache_dir = Path(cache_dir)
        self.transport = transport
        self.sleeper = sleeper
        self.clock = clock
        self.rand = rand
        self.max_attempts = max_attempts
        self.max_wait_seconds = max_wait_seconds
        self.timeout = timeout
        self.last_rate = RateInfo()

    def _headers(self, etag: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-trending-daily-skill",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _url(self, path_or_url: str, params: Optional[Dict[str, Any]]) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"{API_ROOT}{path_or_url}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def _cache_path(self, url: str) -> Path:
        identity = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12] if self.token else "anonymous"
        key = hashlib.sha256(f"{API_VERSION}\0{self.auth_mode}\0{identity}\0{url}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _load_cache(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (FileNotFoundError, OSError, ValueError):
            return None

    def _save_cache(self, path: Path, payload: Any, etag: Optional[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"fetched_at": self.clock(), "etag": etag, "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _rate_from_headers(self, headers: Dict[str, str]) -> RateInfo:
        def integer(name: str) -> Optional[int]:
            try:
                return int(headers[name]) if name in headers else None
            except (TypeError, ValueError):
                return None

        return RateInfo(
            limit=integer("x-ratelimit-limit"),
            remaining=integer("x-ratelimit-remaining"),
            used=integer("x-ratelimit-used"),
            reset=integer("x-ratelimit-reset"),
            resource=headers.get("x-ratelimit-resource"),
            retry_after=integer("retry-after"),
        )

    def _delay(self, response: Response, attempt: int) -> float:
        rate = self._rate_from_headers(response.headers)
        if rate.retry_after is not None:
            return float(rate.retry_after)
        if rate.remaining == 0 and rate.reset is not None:
            return max(0.0, rate.reset - self.clock())
        base = 60.0 if response.status in (403, 429) else min(60.0, float(2**attempt))
        return base + base * 0.25 * self.rand()

    def request_json(
        self,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        cache_ttl: int = 0,
    ) -> Any:
        url = self._url(path_or_url, params)
        cache_path = self._cache_path(url)
        cached = self._load_cache(cache_path)
        if cached and cache_ttl > 0 and self.clock() - float(cached.get("fetched_at", 0)) <= cache_ttl:
            return cached.get("payload")

        etag = cached.get("etag") if cached else None
        for attempt in range(self.max_attempts):
            try:
                response = self.transport(url, self._headers(etag), self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.RemoteDisconnected, OSError) as exc:
                if attempt + 1 >= self.max_attempts:
                    raise GitHubClientError(f"GitHub connection failed after {self.max_attempts} attempts: {type(exc).__name__}") from exc
                delay = min(self.max_wait_seconds, (2**attempt) * (1 + 0.25 * self.rand()))
                self.sleeper(delay)
                continue

            self.last_rate = self._rate_from_headers(response.headers)
            if response.status == 304 and cached:
                cached["fetched_at"] = self.clock()
                self._save_cache(cache_path, cached.get("payload"), cached.get("etag"))
                return cached.get("payload")
            if 200 <= response.status < 300:
                try:
                    payload = json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise GitHubClientError(f"GitHub returned invalid JSON for {urllib.parse.urlsplit(url).path}") from exc
                if cache_ttl > 0 or response.headers.get("etag"):
                    self._save_cache(cache_path, payload, response.headers.get("etag"))
                return payload
            body_lower = response.body.decode("utf-8", errors="replace").casefold()
            is_limited = (
                response.status == 429
                or (response.status == 403 and self.last_rate.remaining == 0)
                or (response.status == 403 and self.last_rate.retry_after is not None)
                or (response.status == 403 and ("rate limit" in body_lower or "secondary rate" in body_lower))
            )
            if is_limited:
                delay = self._delay(response, attempt)
                if delay > self.max_wait_seconds or attempt + 1 >= self.max_attempts:
                    raise RateLimitError(
                        "GitHub rate limit reached; resume after the reset time",
                        self.last_rate.reset,
                        self.last_rate.retry_after,
                    )
                self.sleeper(delay)
                continue
            if response.status in (502, 503, 504) and attempt + 1 < self.max_attempts:
                self.sleeper(min(self.max_wait_seconds, self._delay(response, attempt)))
                continue
            detail = response.body.decode("utf-8", errors="replace")[:300]
            raise GitHubClientError(f"GitHub API HTTP {response.status} for {urllib.parse.urlsplit(url).path}: {detail}")
        raise GitHubClientError("GitHub request failed")

    def search_repositories(self, query: str, limit: int) -> list:
        results = []
        page = 1
        while len(results) < limit:
            per_page = min(100, limit - len(results))
            payload = self.request_json(
                "/search/repositories",
                {"q": query, "sort": "stars", "order": "desc", "per_page": per_page, "page": page},
                cache_ttl=900,
            )
            items = payload.get("items", [])
            results.extend(items)
            if len(items) < per_page:
                break
            page += 1
        return results[:limit]

    def rate_limit(self) -> RateInfo:
        payload = self.request_json("/rate_limit", cache_ttl=30)
        resource = (payload.get("resources") or {}).get("core") or {}
        self.last_rate = RateInfo(
            limit=resource.get("limit"),
            remaining=resource.get("remaining"),
            used=resource.get("used"),
            reset=resource.get("reset"),
            resource="core",
        )
        return self.last_rate
