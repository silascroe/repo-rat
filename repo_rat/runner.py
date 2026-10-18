"""Polling coordinator for Repo Rat's release-processing pipeline."""

from dataclasses import dataclass

from .config import AppConfig
from .delivery import DeliveryError, DeliveryRouter
from .github import GitHubClient, GitHubError
from .judgment import Judge, JudgmentError
from .models import Judgment, Release
from .state import StateStore, StoredRelease


@dataclass(frozen=True)
class RunSummary:
    fetched: int = 0
    seeded: int = 0
    judged: int = 0
    reported: int = 0
    ignored: int = 0
    errors: int = 0
    repository_errors: int = 0


class RepoRat:
    """Coordinate release polling without owning HTTP or persistence details."""

    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        state: StateStore,
        judge: Judge,
        delivery: DeliveryRouter,
    ):
        self._config = config
        self._github = github
        self._state = state
        self._judge = judge
        self._delivery = delivery

    def poll(self, mode: str) -> RunSummary:
        if mode not in {"normal", "backfill"}:
            raise ValueError("mode must be 'normal' or 'backfill'")

        with self._state.poll_lock() as acquired:
            if not acquired:
                # No fetching, judging, delivery or run row while another poll owns it.
                return RunSummary(errors=1)
            return self._poll_owned(mode)

    def _poll_owned(self, mode: str) -> RunSummary:
        counts = {
            "fetched": 0,
            "seeded": 0,
            "judged": 0,
            "reported": 0,
            "ignored": 0,
            "errors": 0,
            "repository_errors": 0,
        }
        errors = []
        run_id = self._state.start_run(mode)
        try:
            repositories = list(self._config.repositories)
            if self._config.starred_username:
                try:
                    starred_repositories = self._github.fetch_public_starred_repositories(
                        self._config.starred_username
                    )
                except GitHubError as exc:
                    counts["errors"] += 1
                    counts["repository_errors"] += 1
                    errors.append(
                        f"starred repositories for {self._config.starred_username}: {exc}"
                    )
                else:
                    for repository in starred_repositories:
                        if repository not in repositories:
                            repositories.append(repository)

            for repository in repositories:
                had_state = bool(
                    self._state.get_releases(repository)
                    or self._state.is_repository_initialized(repository)
                )
                try:
                    releases = self._github.fetch_releases(
                        repository, self._config.include_prereleases
                    )
                except GitHubError as exc:
                    counts["errors"] += 1
                    counts["repository_errors"] += 1
                    errors.append(f"{repository}: {exc}")
                    continue

                counts["fetched"] += len(releases)
                self._state.insert_releases(releases)
                if mode == "normal" and not had_state:
                    counts["seeded"] += self._state.seed_repository(repository)
                    self._state.mark_repository_initialized(repository)

                candidates = self._state.candidates(
                    repository, include_seeded=mode == "backfill"
                )
                for stored_release in candidates:
                    if stored_release.status == "interesting":
                        judgment = Judgment(
                            True, stored_release.summary or "", stored_release.reason or ""
                        )
                    else:
                        try:
                            judgment = self._judge.judge(self._release_from(stored_release))
                        except JudgmentError as exc:
                            self._state.save_judgment(
                                repository, stored_release.release_id, None, error=str(exc)
                            )
                            counts["errors"] += 1
                            errors.append(f"{stored_release.identity}: {exc}")
                            continue

                        counts["judged"] += 1
                        if not self._state.save_judgment(
                            repository, stored_release.release_id, judgment
                        ):
                            counts["errors"] += 1
                            errors.append(f"{stored_release.identity}: judgment was not saved")
                            continue
                    if not judgment.interesting:
                        counts["ignored"] += 1
                        continue

                    try:
                        delivery = self._delivery.deliver(stored_release, judgment)
                        self._state.mark_delivery(
                            repository, stored_release.release_id, delivery.channel
                        )
                    except Exception as exc:
                        self._state.mark_delivery_failed(
                            repository, stored_release.release_id
                        )
                        counts["errors"] += 1
                        # DeliveryError text is already sanitized; anything else
                        # could carry the webhook URL, so report only its type.
                        detail = (
                            str(exc)
                            if isinstance(exc, DeliveryError)
                            else type(exc).__name__
                        )
                        errors.append(
                            f"{stored_release.identity}: delivery failed ({detail})"
                        )
                        continue
                    counts["reported"] += 1
        finally:
            self._state.finish_run(
                run_id,
                fetched=counts["fetched"],
                judged=counts["judged"],
                reported=counts["reported"],
                errors=counts["errors"],
                error_text="; ".join(errors) or None,
            )

        return RunSummary(**counts)

    @staticmethod
    def _release_from(stored_release: StoredRelease) -> Release:
        return Release(
            repository=stored_release.repository,
            release_id=stored_release.release_id,
            tag_name=stored_release.tag_name,
            name=stored_release.name,
            body=stored_release.body,
            html_url=stored_release.html_url,
            published_at=stored_release.published_at,
            prerelease=stored_release.prerelease,
        )
