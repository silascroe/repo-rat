"""Deliver release judgments to Discord or a local JSON Lines log."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .models import Judgment
from .state import StoredRelease


_DISCORD_CONTENT_LIMIT = 2_000
_HTTP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    detail: str


class DeliveryRouter:
    """Route one release judgment to Discord, or to the local log without a webhook."""

    def __init__(
        self,
        webhook_url: str | None,
        log_path: Path,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        self._webhook_url = webhook_url
        self._log_path = Path(log_path)
        self._opener = opener

    def deliver(self, release: StoredRelease, judgment: Judgment) -> DeliveryResult:
        if not self._webhook_url:
            self._write_local(release, judgment, "local")
            return DeliveryResult(channel="local", detail="written to local log")

        # A failed post raises so the release stays undelivered and is retried
        # on the next poll. Writing it to the local log instead would mark it
        # delivered, and on a throwaway runner that log is gone after the job.
        try:
            self._post_discord(release, judgment)
        except _DiscordStatusError as exc:
            raise DeliveryError(f"Discord returned HTTP {exc.status}") from None
        except urllib.error.HTTPError as exc:
            raise DeliveryError(f"Discord returned HTTP {exc.code}") from None
        except Exception as exc:
            # Never chain the original: it can carry the webhook URL (a secret).
            raise DeliveryError(f"Discord delivery failed ({type(exc).__name__})") from None
        return DeliveryResult(channel="discord", detail="posted to Discord")

    def _post_discord(self, release: StoredRelease, judgment: Judgment) -> None:
        payload = {"content": self._discord_content(release, judgment)}
        request = urllib.request.Request(
            self._webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "repo-rat",
            },
            method="POST",
        )
        with self._opener(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= status < 300:
                raise _DiscordStatusError(status)

    @staticmethod
    def _discord_content(release: StoredRelease, judgment: Judgment) -> str:
        prefix = f"{release.repository} {release.tag_name} — {release.name}\n"
        suffix = f"\n{release.html_url}"
        available = _DISCORD_CONTENT_LIMIT - 1 - len(prefix) - len(suffix)
        if available < 0:
            return (prefix + suffix)[: _DISCORD_CONTENT_LIMIT - 1]
        return prefix + judgment.summary[:available] + suffix

    def _write_local(
        self,
        release: StoredRelease,
        judgment: Judgment,
        delivery: str,
    ) -> None:
        record = {
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "repository": release.repository,
            "tag_name": release.tag_name,
            "release_name": release.name,
            "summary": judgment.summary,
            "reason": judgment.reason,
            "release_url": release.html_url,
            "delivery": delivery,
        }
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as log:
            json.dump(record, log, ensure_ascii=False)
            log.write("\n")


class DeliveryError(RuntimeError):
    """Raised when a configured webhook could not accept a report."""


class _DiscordStatusError(RuntimeError):
    """Internal marker for a Discord response outside the 2xx range."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status
