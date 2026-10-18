"""GitHub REST API release ingestion."""

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .models import Release


class GitHubError(RuntimeError):
    """Raised when GitHub releases cannot be fetched or decoded."""


_REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")
_USERNAME = re.compile(r"^[A-Za-z0-9-]+$")
_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_PAGE_SIZE = 100
# Incremental polls usually find zero or a few new releases, and release
# bodies can be large, so they ask for small pages.
_INCREMENTAL_PAGE_SIZE = 20
_HTTP_TIMEOUT_SECONDS = 30
# Transient failures (truncated bodies, dropped connections, gateway errors)
# get a few quick retries. Timeouts are deliberately not retried: each one
# already costs _HTTP_TIMEOUT_SECONDS.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_RETRYABLE_HTTP_STATUSES = frozenset({502, 503, 504})


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = token
        self._opener = opener
        self._sleep = sleep

    def fetch_releases(
        self,
        repository: str,
        include_prereleases: bool,
        known_release_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Release]:
        """Fetch a repository's releases, newest first.

        Without ``known_release_ids`` every page is fetched. With them, paging
        stops after the first page that contains an already-known release:
        GitHub lists releases newest first, so later pages hold only history
        the caller has already seen.
        """
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise GitHubError(f"invalid repository: {repository!r}")

        page_size = _INCREMENTAL_PAGE_SIZE if known_release_ids else _PAGE_SIZE
        releases = []
        page = 1
        while True:
            payload = self._fetch_page(repository, page, page_size)
            if not isinstance(payload, list):
                raise GitHubError("GitHub releases response must be a JSON list")

            reached_known = False
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if known_release_ids and self._release_id(item) in known_release_ids:
                    reached_known = True
                if item.get("draft", False) or (
                    item.get("prerelease", False) and not include_prereleases
                ):
                    continue
                releases.append(self._to_release(repository, item))

            if reached_known or len(payload) < page_size:
                break
            page += 1

        return releases

    def fetch_public_starred_repositories(self, username: str) -> list[str]:
        """Return the public repositories starred by a GitHub user."""
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            raise GitHubError(f"invalid GitHub username: {username!r}")

        repositories = []
        seen = set()
        page = 1
        while True:
            payload = self._fetch_starred_page(username, page)
            if not isinstance(payload, list):
                raise GitHubError("GitHub starred repositories response must be a JSON list")

            for item in payload:
                if not isinstance(item, dict) or item.get("private", False):
                    continue
                repository = item.get("full_name")
                if (
                    isinstance(repository, str)
                    and _REPOSITORY.fullmatch(repository)
                    and repository not in seen
                ):
                    repositories.append(repository)
                    seen.add(repository)

            if len(payload) < _PAGE_SIZE:
                break
            page += 1

        return repositories

    def _fetch_page(self, repository: str, page: int, page_size: int = _PAGE_SIZE) -> Any:
        url = f"{_API_URL}/repos/{repository}/releases?per_page={page_size}&page={page}"
        return self._fetch_json(url)

    def _fetch_starred_page(self, username: str, page: int) -> Any:
        url = f"{_API_URL}/users/{username}/starred?per_page={_PAGE_SIZE}&page={page}"
        return self._fetch_json(url)

    def _fetch_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "repo-rat",
            },
        )
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")

        raw_body = self._read_with_retries(request)

        try:
            return json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise GitHubError(f"GitHub returned invalid JSON: {exc}") from exc

    def _read_with_retries(self, request: urllib.request.Request) -> bytes:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                with self._opener(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                error = GitHubError(
                    self._safe_message(
                        f"GitHub request failed with HTTP {exc.code}: {exc.reason}"
                    )
                )
                retryable = exc.code in _RETRYABLE_HTTP_STATUSES
                cause: BaseException = exc
            except urllib.error.URLError as exc:
                error = GitHubError(self._safe_message(f"GitHub request failed: {exc.reason}"))
                retryable = isinstance(exc.reason, ConnectionResetError)
                cause = exc
            except http.client.HTTPException as exc:
                # IncompleteRead, RemoteDisconnected and friends: the server
                # hung up mid-response. Worth another try.
                error = GitHubError(
                    self._safe_message(
                        f"GitHub response was interrupted ({type(exc).__name__})"
                    )
                )
                retryable = True
                cause = exc
            except OSError as exc:
                error = GitHubError(self._safe_message(f"GitHub request failed: {exc}"))
                retryable = isinstance(exc, ConnectionResetError)
                cause = exc

            if not retryable or attempt == _MAX_ATTEMPTS:
                raise error from cause
            self._sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])

        raise AssertionError("unreachable")

    def _safe_message(self, message: str) -> str:
        return message.replace(self._token, "[REDACTED]") if self._token else message

    @staticmethod
    def _release_id(item: dict[str, Any]) -> str:
        release_id = item.get("id")
        if release_id is None:
            tag_name = item.get("tag_name") or ""
            published_at = item.get("published_at")
            html_url = item.get("html_url") or ""
            release_id = f"{tag_name}|{published_at or ''}|{html_url}"
        return str(release_id)

    @classmethod
    def _to_release(cls, repository: str, item: dict[str, Any]) -> Release:
        tag_name = item.get("tag_name") or ""
        published_at = item.get("published_at")
        html_url = item.get("html_url") or ""

        return Release(
            repository=repository,
            release_id=cls._release_id(item),
            tag_name=str(tag_name),
            name=str(item.get("name") or ""),
            body=str(item.get("body") or ""),
            html_url=str(html_url),
            published_at=str(published_at) if published_at is not None else None,
            prerelease=bool(item.get("prerelease", False)),
        )
