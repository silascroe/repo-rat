from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGE = DOCS / "index.html"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.hrefs = []
        self.sources = []
        self.images_missing_alt = []
        self.headings_one = 0
        self.landmarks = set()
        self.scripts = 0
        self.text = []
        self.lang = None
        self.title_depth = 0
        self.title = []
        self.description = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "html":
            self.lang = attrs.get("lang")
        if tag in {"header", "nav", "main", "footer"}:
            self.landmarks.add(tag)
        if tag == "h1":
            self.headings_one += 1
        if tag == "title":
            self.title_depth += 1
        if tag == "meta" and attrs.get("name", "").lower() == "description":
            self.description = attrs.get("content")
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag == "img":
            if attrs.get("src"):
                self.sources.append(attrs["src"])
            if "alt" not in attrs:
                self.images_missing_alt.append(attrs.get("src", "<missing src>"))
        if tag == "link" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)

    def handle_data(self, data):
        self.text.append(data)
        if self.title_depth:
            self.title.append(data)


class RepoRatSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.page = PageParser()
        cls.page.feed(cls.html)

    def test_document_has_language_title_description_and_landmarks(self):
        self.assertEqual(self.page.lang, "en")
        self.assertTrue("".join(self.page.title).strip())
        self.assertTrue(self.page.description)
        self.assertEqual(self.page.headings_one, 1)
        self.assertTrue({"header", "nav", "main", "footer"} <= self.page.landmarks)

    def test_images_have_alt_text_and_local_assets_exist(self):
        self.assertEqual(self.page.images_missing_alt, [])
        for source in self.page.sources:
            parsed = urlsplit(source)
            self.assertFalse(parsed.scheme or parsed.netloc, source)
            self.assertTrue((DOCS / parsed.path).is_file(), source)

    def test_local_links_and_assets_resolve(self):
        for target in [*self.page.hrefs, *self.page.sources]:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            if parsed.fragment and not parsed.path:
                self.assertEqual(self.page.ids.count(parsed.fragment), 1, target)
            elif parsed.path:
                self.assertTrue((DOCS / parsed.path).resolve().is_file(), target)
                if parsed.fragment and parsed.path.endswith(".html"):
                    linked = PageParser()
                    linked.feed((DOCS / parsed.path).read_text(encoding="utf-8"))
                    self.assertIn(parsed.fragment, linked.ids, target)

    def test_fragment_links_have_unique_targets(self):
        self.assertEqual(len(self.page.ids), len(set(self.page.ids)))
        for anchor in ("how-it-works", "setup", "backfill-warning"):
            self.assertEqual(self.page.ids.count(anchor), 1)
            self.assertIn(f"#{anchor}", self.page.hrefs)

    def test_footer_uses_canonical_github_links(self):
        expected = {
            "https://github.com/silascroe/repo-rat",
            "https://github.com/silascroe/repo-rat/issues",
            "https://github.com/silascroe/repo-rat/blob/main/LICENSE",
            "https://github.com/silascroe/repo-rat/blob/main/README.md",
            "https://github.com/silascroe/repo-rat/blob/main/docs/architecture.md",
        }
        self.assertTrue(expected <= set(self.page.hrefs))

    def test_setup_and_backfill_copy_is_explicit(self):
        copy = " ".join(self.page.text).lower()
        for phrase in (
            "backfill",
            "many messages",
            "first normal poll",
            "repo_rat_starred_username",
            "discord_webhook_url",
            "optional",
            "openai_api_key",
        ):
            self.assertIn(phrase, copy)
        self.assertIn("previews", copy)

    def test_page_has_no_script_tags(self):
        self.assertEqual(self.page.scripts, 0)

    def test_stylesheet_is_local_and_responsive(self):
        self.assertIn('href="site.css"', self.html)
        css = (DOCS / "site.css").read_text(encoding="utf-8")
        self.assertRegex(css, r":focus-visible")
        self.assertRegex(css, r"@media\s*\(max-width:")
        self.assertNotIn("@import", css)
        self.assertNotRegex(css, r"url\(\s*https?://")


if __name__ == "__main__":
    unittest.main()
