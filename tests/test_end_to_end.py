import json
import tempfile
import unittest
from pathlib import Path

from repo_rat.config import load_config
from repo_rat.delivery import DeliveryRouter
from repo_rat.judgment import HeuristicJudge
from repo_rat.models import Release
from repo_rat.runner import RepoRat
from repo_rat.state import StateStore


class SequencedGitHub:
    """Return the release list for each successive poll without network access."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def fetch_releases(self, repository, include_prereleases, known_release_ids=None):
        self.calls.append((repository, include_prereleases))
        return next(self._responses)


def release(release_id, tag_name, body):
    return Release(
        repository="acme/tool",
        release_id=release_id,
        tag_name=tag_name,
        name=f"Tool {tag_name}",
        body=body,
        html_url=f"https://example.test/acme/tool/releases/{tag_name}",
        published_at="2026-09-20T12:00:00Z",
    )


class EndToEndTests(unittest.TestCase):
    def test_new_release_is_logged_once_after_the_normal_poll_baseline(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            config_path = directory / "config.json"
            state_path = directory / "runtime" / "state.db"
            log_path = directory / "runtime" / "release-log.jsonl"
            config_path.write_text(
                json.dumps(
                    {
                        "repositories": ["acme/tool"],
                        "state_path": str(state_path),
                        "local_log_path": str(log_path),
                        "llm": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            existing = release("1", "v1.0.0", "Initial stable release.")
            new_release = release(
                "2", "v1.1.0", "Security fix for configurable release summaries."
            )
            github = SequencedGitHub(
                [[existing], [existing, new_release], [existing, new_release]]
            )
            config = load_config(config_path)

            with StateStore(config.state_path) as state:
                rat = RepoRat(
                    config,
                    github,
                    state,
                    HeuristicJudge(),
                    DeliveryRouter(webhook_url=None, log_path=config.local_log_path),
                )

                self.assertEqual(rat.poll("normal").reported, 0)
                self.assertFalse(log_path.exists())

                self.assertEqual(rat.poll("normal").reported, 1)
                records = [json.loads(line) for line in log_path.read_text().splitlines()]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["tag_name"], "v1.1.0")

                self.assertEqual(rat.poll("normal").reported, 0)
                self.assertEqual(len(log_path.read_text().splitlines()), 1)

            self.assertEqual(github.calls, [("acme/tool", False)] * 3)


if __name__ == "__main__":
    unittest.main()
