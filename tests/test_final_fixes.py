"""Offline regressions for the coordinated final review findings."""

import json
import math
import multiprocessing
import tempfile
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from repo_rat.delivery import DeliveryError, DeliveryRouter
from repo_rat.github import GitHubClient, GitHubError
from repo_rat.judgment import (
    FallbackJudge, HeuristicJudge, JudgmentError, OpenAICompatibleJudge,
)
from repo_rat.models import Judgment
from repo_rat.runner import RepoRat
from repo_rat.state import StateStore
from test_judgment import FakeTransport, llm_config
from test_runner import FakeGitHub, FakeJudge, make_config, make_release


APPROVED = Judgment(True, "Previously approved summary", "Security improvement")


def held_poll(db_path, pipe):
    """Hold a real poll at judgment while another process attempts a poll."""
    class HeldJudge:
        def judge(self, release):
            pipe.send("judging")
            if not pipe.poll(15):
                raise RuntimeError("test coordination timed out")
            pipe.recv()
            return APPROVED

    try:
        with StateStore(Path(db_path)) as state:
            config = make_config(("acme/tool",), state.path)
            rat = RepoRat(
                config, FakeGitHub({"acme/tool": [make_release()]}), state,
                HeldJudge(), DeliveryRouter(None, config.local_log_path),
            )
            pipe.send(rat.poll("backfill"))
    finally:
        pipe.close()


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.config = make_config(("acme/tool",), self.path)
        self.github = FakeGitHub({"acme/tool": [make_release()]})
        self.judge = FakeJudge({"1": APPROVED})

    def rat(self, state, delivery=None):
        return RepoRat(
            self.config, self.github, state, self.judge,
            delivery or DeliveryRouter(None, self.config.local_log_path),
        )

    def records(self):
        return [json.loads(line) for line in self.config.local_log_path.read_text().splitlines()]

    def test_interruption_after_persisting_judgment_resumes_without_rejudging(self):
        class InterruptedDelivery:
            def deliver(self, release, judgment):
                raise KeyboardInterrupt()

        with StateStore(self.path) as state:
            with self.assertRaises(KeyboardInterrupt):
                self.rat(state, InterruptedDelivery()).poll("backfill")
        self.judge.judgments["1"] = Judgment(False, "Changed decision", "Changed")
        # Recovery must not depend on the release remaining in GitHub's response.
        self.github.releases_by_repository["acme/tool"] = []
        with StateStore(self.path) as state:
            recovered = self.rat(state).poll("normal")
            repeated = self.rat(state).poll("backfill")
            row = state.get_releases("acme/tool")[0]
        self.assertEqual(recovered.reported, 1)
        self.assertEqual(recovered.judged, 0)
        self.assertEqual(repeated.reported, 0)
        self.assertEqual(self.judge.calls, ["1"])
        self.assertEqual(self.records()[0]["summary"], APPROVED.summary)
        self.assertEqual(row.delivered_via, "local")

    def test_delivery_failure_uses_original_judgment_even_if_judge_changes(self):
        class FailedDelivery:
            def deliver(self, release, judgment):
                raise OSError("disk unavailable")

        with StateStore(self.path) as state:
            self.assertEqual(self.rat(state, FailedDelivery()).poll("backfill").errors, 1)
        self.judge.judgments["1"] = Judgment(False, "Changed decision", "Changed")
        with StateStore(self.path) as state:
            summary = self.rat(state).poll("normal")
        self.assertEqual(summary.reported, 1)
        self.assertEqual(summary.judged, 0)
        self.assertEqual(self.judge.calls, ["1"])
        self.assertEqual(self.records()[0]["summary"], APPROVED.summary)

    def test_legacy_failed_delivery_row_is_not_rejudged(self):
        with StateStore(self.path) as state:
            state.insert_release(make_release())
            state.save_judgment("acme/tool", "1", APPROVED)
            state.connection.execute("UPDATE releases SET status = 'failed'")
            state.connection.commit()
        self.judge.judgments["1"] = Judgment(False, "Changed decision", "Changed")
        with StateStore(self.path) as state:
            summary = self.rat(state).poll("normal")
            row = state.get_releases("acme/tool")[0]
        self.assertEqual(summary.reported, 1)
        self.assertEqual(summary.judged, 0)
        self.assertEqual(self.judge.calls, [])
        self.assertEqual(row.status, "interesting")
        self.assertEqual(self.records()[0]["summary"], APPROVED.summary)

    def test_judgment_failure_still_retries_judgment(self):
        self.judge.errors.add("1")
        with StateStore(self.path) as state:
            self.assertEqual(self.rat(state).poll("backfill").errors, 1)
            self.judge.errors.clear()
            summary = self.rat(state).poll("normal")
        self.assertEqual(summary.judged, 1)
        self.assertEqual(summary.reported, 1)
        self.assertEqual(self.judge.calls, ["1", "1"])

    def test_failed_save_judgment_never_delivers_unsaved_decision(self):
        class RejectedState(StateStore):
            def save_judgment(self, *args, **kwargs):
                return False

        with RejectedState(self.path) as state:
            summary = self.rat(state).poll("backfill")
        self.assertEqual(summary.reported, 0)
        self.assertEqual(summary.errors, 1)
        self.assertFalse(self.config.local_log_path.exists())

    def start_held_poll(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=held_poll, args=(str(self.path), child))
        process.start()
        child.close()
        self.addCleanup(parent.close)
        self.addCleanup(self.stop_process, process)
        self.assertTrue(parent.poll(10), "child did not reach judgment")
        self.assertEqual(parent.recv(), "judging")
        return process, parent

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.terminate()
        process.join(10)
        process.close()

    def test_overlapping_process_polls_cannot_judge_or_deliver_twice(self):
        process, pipe = self.start_held_poll()
        try:
            with StateStore(self.path) as state:
                overlap = self.rat(state).poll("backfill")
        finally:
            pipe.send("resume")
        self.assertTrue(pipe.poll(10), "child did not finish")
        owner = pipe.recv()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(overlap.reported, 0)
        self.assertEqual(overlap.judged, 0)
        self.assertEqual(overlap.errors, 1)
        self.assertEqual(owner.reported, 1)
        with StateStore(self.path) as state:
            self.assertEqual(self.rat(state).poll("normal").reported, 0)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.judge.calls, [])

    def test_terminated_process_releases_poll_ownership(self):
        process, pipe = self.start_held_poll()
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        with StateStore(self.path) as state:
            self.assertEqual(self.rat(state).poll("normal").reported, 1)
        self.assertEqual(len(self.records()), 1)


class HTTPAndModelRegressions(unittest.TestCase):
    def timeout_transport(self, seen):
        def transport(request, *, timeout=None):
            seen.append(timeout)
            raise TimeoutError("sensitive-endpoint sensitive-api-key")
        return transport

    def assert_finite_timeout(self, seen):
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], (int, float))
        self.assertTrue(math.isfinite(seen[0]))
        self.assertGreater(seen[0], 0)

    def test_github_passes_finite_timeout_and_translates_timeout_error(self):
        seen = []
        with self.assertRaises(GitHubError) as error:
            GitHubClient(token="sensitive-api-key", opener=self.timeout_transport(seen)).fetch_releases("acme/tool", False)
        self.assertNotIn("sensitive-api-key", str(error.exception))
        self.assert_finite_timeout(seen)

    @mock.patch.dict("os.environ", {"TEST_LLM_API_KEY": "sensitive-api-key"}, clear=True)
    def test_model_timeout_is_sanitized_and_falls_back(self):
        seen = []
        primary = OpenAICompatibleJudge(llm_config(), self.timeout_transport(seen))
        with self.assertRaises(JudgmentError) as error:
            primary.judge(make_release())
        self.assert_finite_timeout(seen)
        formatted = "".join(traceback.format_exception(error.exception))
        self.assertNotIn("sensitive-api-key", formatted)
        self.assertNotIn("sensitive-endpoint", formatted)
        result = FallbackJudge(primary, HeuristicJudge()).judge(make_release(body="Security fix."))
        self.assertEqual(result.summary, "Security fix.")

    def test_discord_passes_finite_timeout_and_raises_sanitized_error(self):
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            with self.assertRaises(DeliveryError) as error:
                DeliveryRouter(
                    "https://discord.test/secret", path, self.timeout_transport(seen)
                ).deliver(make_release(), APPROVED)
            self.assertFalse(path.exists())
        self.assert_finite_timeout(seen)
        formatted = "".join(traceback.format_exception(error.exception))
        self.assertNotIn("discord.test/secret", formatted)

    @mock.patch.dict("os.environ", {"TEST_LLM_API_KEY": "test-key"}, clear=True)
    def test_default_model_request_omits_unsupported_temperature(self):
        transport = FakeTransport('{"interesting": true, "summary": "Approved"}')
        config = replace(llm_config(), default_model="gpt-5-mini")
        OpenAICompatibleJudge(config, transport).judge(make_release())
        payload = json.loads(transport.requests[0].data)
        self.assertEqual(payload["model"], "gpt-5-mini")
        self.assertNotIn("temperature", payload)

    def test_invalid_endpoint_and_model_are_sanitized_and_use_fallback(self):
        cases = [
            ("TEST_LLM_BASE_URL", value) for value in (
                "", "   ", "sensitive-endpoint", "https://", "ftp://host/v1",
                "https://[broken", "https://host:bad/v1", "https://host/v1?token=sensitive-endpoint",
                "https://user:sensitive-endpoint@host/v1", "https://bad host/v1",
                "https://host/\nsecret", "https://host/v1#fragment",
            )
        ] + [("TEST_LLM_MODEL", value) for value in ("", "  ", "bad\nmodel")]
        for variable, value in cases:
            with self.subTest(variable=variable, value=value), mock.patch.dict(
                "os.environ", {"TEST_LLM_API_KEY": "sensitive-api-key", variable: value}, clear=True
            ):
                transport = FakeTransport('{"interesting": false, "summary": "Model result"}')
                primary = OpenAICompatibleJudge(llm_config(), transport)
                with self.assertRaises(JudgmentError) as error:
                    primary.judge(make_release())
                formatted = "".join(traceback.format_exception(error.exception))
                self.assertNotIn("sensitive-endpoint", formatted)
                self.assertNotIn("sensitive-api-key", formatted)
                result = FallbackJudge(primary, HeuristicJudge()).judge(make_release(body="Security fix."))
                self.assertEqual(result.summary, "Security fix.")
                self.assertEqual(transport.requests, [])

    @mock.patch.dict("os.environ", {
        "TEST_LLM_API_KEY": "test-key",
        "TEST_LLM_BASE_URL": "  https://llm.example.test/v1/  ",
        "TEST_LLM_MODEL": "  gpt-5-mini  ",
    }, clear=True)
    def test_endpoint_and_model_surrounding_spaces_are_normalized(self):
        transport = FakeTransport('{"interesting": true, "summary": "Approved"}')
        OpenAICompatibleJudge(llm_config(), transport).judge(make_release())
        self.assertEqual(transport.requests[0].full_url, "https://llm.example.test/v1/chat/completions")
        self.assertEqual(json.loads(transport.requests[0].data)["model"], "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()
