import json
import tempfile
import unittest
from pathlib import Path

from repo_rat.delivery import DeliveryError, DeliveryRouter
from repo_rat.models import Judgment
from repo_rat.state import StoredRelease


def make_release(**overrides):
    values = {
        "repository": "acme/tool",
        "release_id": "1",
        "tag_name": "v1.2.3",
        "name": "Tool release",
        "body": "New feature.",
        "html_url": "https://example.test/releases/v1.2.3",
        "published_at": "2026-09-20T12:00:00Z",
        "prerelease": False,
        "first_seen_at": "2026-09-20T12:00:00Z",
        "status": "interesting",
        "reason": "meaningful feature",
        "summary": "New feature.",
        "delivered_at": None,
        "delivered_via": None,
    }
    values.update(overrides)
    return StoredRelease(**values)


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeOpener:
    def __init__(self, status=204):
        self.status = status
        self.requests = []

    def __call__(self, request, *, timeout=None):
        self.requests.append(request)
        return FakeResponse(self.status)


class FailingOpener:
    def __call__(self, request, *, timeout=None):
        raise TimeoutError("request timed out")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "nested" / "releases.jsonl"
        self.release = make_release()
        self.judgment = Judgment(
            interesting=True,
            summary="A meaningful feature is now available.",
            reason="user-facing change",
        )
        self.opener = FakeOpener()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_webhook_writes_one_local_json_record(self):
        router = DeliveryRouter(webhook_url=None, log_path=self.log_path, opener=self.opener)

        result = router.deliver(self.release, self.judgment)

        self.assertEqual(result.channel, "local")
        records = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["release_url"], self.release.html_url)
        self.assertEqual(records[0]["summary"], self.judgment.summary)
        self.assertEqual(records[0]["reason"], self.judgment.reason)
        self.assertEqual(self.opener.requests, [])

    def test_successful_webhook_posts_json_payload(self):
        router = DeliveryRouter(
            webhook_url="https://discord.test/webhook",
            log_path=self.log_path,
            opener=self.opener,
        )

        result = router.deliver(self.release, self.judgment)

        self.assertEqual(result.channel, "discord")
        self.assertEqual(len(self.opener.requests), 1)
        request = self.opener.requests[0]
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data)
        self.assertIn(self.judgment.summary, payload["content"])
        self.assertIn(self.release.html_url, payload["content"])
        self.assertFalse(self.log_path.exists())

    def test_webhook_content_stays_below_discord_limit(self):
        judgment = Judgment(
            interesting=True,
            summary="x" * 10_000,
            reason="reason",
        )
        router = DeliveryRouter(
            webhook_url="https://discord.test/webhook",
            log_path=self.log_path,
            opener=self.opener,
        )

        router.deliver(self.release, judgment)

        content = json.loads(self.opener.requests[0].data)["content"]
        self.assertLess(len(content), 2_000)
        self.assertIn(self.release.html_url, content)

    def test_failed_discord_post_raises_without_marking_it_delivered(self):
        router = DeliveryRouter(
            webhook_url="https://discord.test/webhook/secret-token",
            log_path=self.log_path,
            opener=FailingOpener(),
        )

        with self.assertRaises(DeliveryError) as error:
            router.deliver(self.release, self.judgment)

        # Nothing is written locally: the runner keeps the release pending and
        # retries Discord next poll, instead of burying it in a log file.
        self.assertFalse(self.log_path.exists())
        self.assertNotIn("secret-token", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertTrue(error.exception.__suppress_context__)

    def test_non_2xx_webhook_response_raises(self):
        router = DeliveryRouter(
            webhook_url="https://discord.test/webhook",
            log_path=self.log_path,
            opener=FakeOpener(status=500),
        )

        with self.assertRaisesRegex(DeliveryError, "HTTP 500"):
            router.deliver(self.release, self.judgment)

        self.assertFalse(self.log_path.exists())


if __name__ == "__main__":
    unittest.main()
