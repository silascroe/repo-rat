import http.client
import json
import unittest
from urllib.error import HTTPError

from repo_rat.github import GitHubClient, GitHubError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload):
    return FakeResponse(payload)


class GitHubClientTests(unittest.TestCase):
    def test_fetch_public_starred_repositories_paginates_and_filters_private(self):
        first_page = [
            {"full_name": f"owner/repo-{index}", "private": False}
            for index in range(99)
        ]
        first_page.append({"full_name": "owner/private", "private": True})
        opener = FakeOpener(
            [
                json_response(first_page),
                json_response([{"full_name": "owner/last", "private": False}]),
            ]
        )

        repositories = GitHubClient(opener=opener).fetch_public_starred_repositories(
            "silascroe"
        )

        self.assertEqual(len(repositories), 100)
        self.assertNotIn("owner/private", repositories)
        self.assertEqual(repositories[-1], "owner/last")
        self.assertIn("/users/silascroe/starred?per_page=100&page=1", opener.requests[0].full_url)
        self.assertIn("/users/silascroe/starred?per_page=100&page=2", opener.requests[1].full_url)

    def test_rejects_invalid_starred_username_before_request(self):
        opener = FakeOpener([])

        with self.assertRaises(GitHubError):
            GitHubClient(opener=opener).fetch_public_starred_repositories("not a user")

        self.assertEqual(opener.requests, [])

    def test_fetch_releases_converts_payload_and_filters_prereleases(self):
        opener = FakeOpener(
            [
                json_response(
                    [
                        {
                            "id": 11,
                            "tag_name": "v2.0.0",
                            "name": "Major release",
                            "body": "Breaking change and faster startup.",
                            "html_url": "https://github.com/acme/tool/releases/tag/v2.0.0",
                            "published_at": "2026-09-19T12:00:00Z",
                            "prerelease": False,
                            "draft": False,
                        },
                        {
                            "id": 12,
                            "tag_name": "v2.1.0-rc1",
                            "prerelease": True,
                            "draft": False,
                        },
                    ]
                )
            ]
        )

        releases = GitHubClient(opener=opener).fetch_releases("acme/tool", False)

        self.assertEqual([release.release_id for release in releases], ["11"])
        self.assertEqual(releases[0].repository, "acme/tool")
        self.assertEqual(releases[0].name, "Major release")
        self.assertEqual(
            opener.requests[0].headers["Accept"], "application/vnd.github+json"
        )
        self.assertEqual(
            opener.requests[0].headers["X-github-api-version"], "2022-11-28"
        )
        self.assertEqual(opener.requests[0].headers["User-agent"], "repo-rat")
        self.assertNotIn("Authorization", opener.requests[0].headers)

    def test_token_is_sent_as_bearer_header(self):
        opener = FakeOpener([json_response([])])

        GitHubClient(token="secret-token", opener=opener).fetch_releases(
            "acme/tool", True
        )

        self.assertEqual(opener.requests[0].headers["Authorization"], "Bearer secret-token")

    def test_fetches_a_second_page_when_first_page_is_full(self):
        first_page = [
            {
                "id": index,
                "tag_name": f"v{index}",
                "draft": False,
                "prerelease": False,
            }
            for index in range(100)
        ]
        opener = FakeOpener([json_response(first_page), json_response([])])

        releases = GitHubClient(opener=opener).fetch_releases("acme/tool", True)

        self.assertEqual(len(releases), 100)
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("per_page=100&page=1", opener.requests[0].full_url)
        self.assertIn("per_page=100&page=2", opener.requests[1].full_url)

    def test_fallback_identity_and_draft_filtering(self):
        opener = FakeOpener(
            [
                json_response(
                    [
                        {
                            "tag_name": "v1.0.0",
                            "published_at": "2026-09-19T12:00:00Z",
                            "html_url": "https://example.test/v1.0.0",
                            "draft": False,
                        },
                        {"id": 2, "draft": True},
                    ]
                )
            ]
        )

        releases = GitHubClient(opener=opener).fetch_releases("acme/tool", True)

        self.assertEqual(
            releases[0].release_id,
            "v1.0.0|2026-09-19T12:00:00Z|https://example.test/v1.0.0",
        )

    def test_rejects_malformed_repository_before_request(self):
        opener = FakeOpener([])

        with self.assertRaises(GitHubError):
            GitHubClient(opener=opener).fetch_releases("acme/tool/releases", False)

        self.assertEqual(opener.requests, [])

    def test_translates_http_error_without_exposing_token(self):
        token = "secret-token"
        opener = FakeOpener(
            [HTTPError("https://api.github.com", 401, token, {}, None)]
        )

        with self.assertRaises(GitHubError) as raised:
            GitHubClient(token=token, opener=opener).fetch_releases("acme/tool", False)

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("HTTP 401", str(raised.exception))

    def test_translates_malformed_json(self):
        class InvalidResponse(FakeResponse):
            def read(self):
                return b"not json"

        opener = FakeOpener([InvalidResponse(None)])

        with self.assertRaisesRegex(GitHubError, "invalid JSON"):
            GitHubClient(opener=opener).fetch_releases("acme/tool", False)


class TruncatedResponse(FakeResponse):
    """A response whose body is cut off mid-read, like the Oct 14 poll failure."""

    def __init__(self):
        super().__init__(None)

    def read(self):
        raise http.client.IncompleteRead(b'[{"id": 1', 4096)


def release_page(*tags):
    return json_response(
        [{"id": index, "tag_name": tag, "html_url": f"https://example.test/{tag}"}
         for index, tag in enumerate(tags, start=1)]
    )


class TransientFailureTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def client(self, opener, token=None):
        return GitHubClient(token=token, opener=opener, sleep=self.sleeps.append)

    def test_incomplete_read_is_retried_and_recovers(self):
        opener = FakeOpener([TruncatedResponse(), release_page("v1.0.0")])

        releases = self.client(opener).fetch_releases("acme/tool", False)

        self.assertEqual([release.tag_name for release in releases], ["v1.0.0"])
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_persistent_incomplete_read_becomes_github_error(self):
        opener = FakeOpener([TruncatedResponse() for _ in range(3)])

        with self.assertRaises(GitHubError) as raised:
            self.client(opener, token="secret-token").fetch_releases("acme/tool", False)

        self.assertIn("IncompleteRead", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_remote_disconnect_is_retried(self):
        opener = FakeOpener(
            [http.client.RemoteDisconnected("closed"), release_page("v2.0.0")]
        )

        releases = self.client(opener).fetch_releases("acme/tool", False)

        self.assertEqual([release.tag_name for release in releases], ["v2.0.0"])
        self.assertEqual(len(opener.requests), 2)

    def test_gateway_errors_are_retried(self):
        opener = FakeOpener(
            [
                HTTPError("https://api.github.com", 503, "Unavailable", None, None),
                release_page("v3.0.0"),
            ]
        )

        releases = self.client(opener).fetch_releases("acme/tool", False)

        self.assertEqual([release.tag_name for release in releases], ["v3.0.0"])
        self.assertEqual(self.sleeps, [1.0])

    def test_client_errors_are_not_retried(self):
        opener = FakeOpener(
            [HTTPError("https://api.github.com", 404, "Not Found", None, None)]
        )

        with self.assertRaisesRegex(GitHubError, "HTTP 404"):
            self.client(opener).fetch_releases("acme/tool", False)

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_retry_resumes_on_the_failing_page_only(self):
        full_page = [
            {"id": index, "tag_name": f"v{index}", "html_url": f"https://example.test/{index}"}
            for index in range(100)
        ]
        opener = FakeOpener(
            [json_response(full_page), TruncatedResponse(), release_page("v-last")]
        )

        releases = self.client(opener).fetch_releases("acme/tool", False)

        self.assertEqual(len(releases), 101)
        self.assertIn("page=1", opener.requests[0].full_url)
        self.assertIn("page=2", opener.requests[1].full_url)
        self.assertIn("page=2", opener.requests[2].full_url)


if __name__ == "__main__":
    unittest.main()
