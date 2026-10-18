"""Regressions for running Repo Rat on throwaway GitHub Actions runners."""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from repo_rat.config import load_config
from repo_rat.delivery import DeliveryRouter
from repo_rat.models import Judgment
from repo_rat.runner import RepoRat
from repo_rat.state import StateStore
from test_runner import FakeGitHub, FakeJudge, make_config, make_release


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/repo-rat.yml"


def workflow_config_script() -> str:
    """Return the inline Python the workflow uses to write its runner config."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)\n\s*PY\n", text, re.DOTALL)
    if match is None:
        raise AssertionError("workflow config script not found")
    return textwrap.dedent(match.group(1))


class WorkflowStatePathTests(unittest.TestCase):
    def test_runner_config_keeps_state_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as runner_temp:
            checkout = Path(checkout).resolve()
            (checkout / "config.json").write_text(
                (ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            runner_config = Path(runner_temp) / "repo-rat.json"
            env = {**os.environ, "REPO_RAT_STARRED_USERNAME": "octocat"}

            subprocess.run(
                [sys.executable, "-", str(runner_config)],
                input=workflow_config_script(),
                text=True,
                cwd=checkout,
                env=env,
                check=True,
            )
            config = load_config(runner_config)

        # The persist step commits ./state.db from the checkout; the database
        # must land there, not next to the temporary config file.
        self.assertEqual(config.state_path, checkout / "state.db")
        self.assertEqual(config.local_log_path, checkout / "release-log.jsonl")
        self.assertEqual(config.starred_username, "octocat")
        self.assertIn('git add -f state.db', WORKFLOW.read_text(encoding="utf-8"))


class FlakyDiscord:
    """Fail the first webhook post with HTTP 500, then accept posts."""

    def __init__(self):
        self.posts = []
        self.failures_left = 1

    def __call__(self, request, *, timeout=None):
        if self.failures_left:
            self.failures_left -= 1
            return _Response(500)
        self.posts.append(json.loads(request.data))
        return _Response(204)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DiscordRetryTests(unittest.TestCase):
    def test_failed_discord_post_is_retried_on_the_next_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.db"
            config = make_config(["acme/tool"], state_path)
            github = FakeGitHub({"acme/tool": []})
            judge = FakeJudge({"1": Judgment(True, "Big release.", "major")})
            discord = FlakyDiscord()
            delivery = DeliveryRouter(
                "https://discord.test/webhook", config.local_log_path, discord
            )

            with StateStore(state_path) as state:
                rat = RepoRat(config, github, state, judge, delivery)
                rat.poll("normal")  # establish an empty baseline

                github.releases_by_repository["acme/tool"] = [make_release()]
                first = rat.poll("normal")
                stored = state.get_releases("acme/tool")[0]
                run = state.get_run(2)  # run 1 was the baseline

                self.assertEqual(first.reported, 0)
                self.assertEqual(first.errors, 1)
                self.assertIsNone(stored.delivered_at)
                self.assertEqual(stored.status, "interesting")
                self.assertIn("HTTP 500", run["error_text"])
                self.assertFalse(config.local_log_path.exists())

                second = rat.poll("normal")
                stored = state.get_releases("acme/tool")[0]

            self.assertEqual(second.reported, 1)
            self.assertEqual(stored.delivered_via, "discord")
            self.assertEqual(len(discord.posts), 1)
            self.assertIn("Big release.", discord.posts[0]["content"])
            # Judged once; the retry reuses the saved judgment.
            self.assertEqual(judge.calls, ["1"])


if __name__ == "__main__":
    unittest.main()
