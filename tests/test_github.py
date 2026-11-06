import io
import json
import unittest
from unittest.mock import patch

from app.github import GitHubReleaseError, latest_ipa_assets, parse_repository_url


class GitHubReleaseTests(unittest.TestCase):
    def test_parse_repository_url(self):
        parsed = parse_repository_url("https://github.com/example/demo.git/")

        self.assertEqual(
            parsed, ("https://github.com/example/demo", "example", "demo")
        )

    def test_rejects_non_repository_urls(self):
        for url in (
            "http://github.com/example/demo",
            "https://github.com/example/demo/releases",
            "https://github.com.evil.example/example/demo",
            "https://user:password@github.com/example/demo",
        ):
            with self.subTest(url=url), self.assertRaises(GitHubReleaseError):
                parse_repository_url(url)

    def test_returns_all_ipa_assets_from_latest_release(self):
        release = {
            "tag_name": "v2.1",
            "published_at": "2026-10-25T17:33:06Z",
            "assets": [
                {
                    "name": "readme.txt",
                    "browser_download_url": "https://github.com/example/demo/readme.txt",
                },
                {
                    "name": "Demo.ipa",
                    "browser_download_url": (
                        "https://github.com/example/demo/releases/download/v2.1/Demo.ipa"
                    ),
                },
                {
                    "name": "Demo-iOS16.IPA",
                    "browser_download_url": (
                        "https://github.com/example/demo/releases/download/v2.1/Demo-iOS16.IPA"
                    ),
                },
            ],
        }

        with patch(
            "app.github.urllib.request.urlopen",
            return_value=io.BytesIO(json.dumps(release).encode()),
        ):
            assets = latest_ipa_assets("https://github.com/example/demo")

        self.assertEqual([asset.name for asset in assets], ["Demo.ipa", "Demo-iOS16.IPA"])
        self.assertTrue(all(asset.release_tag == "v2.1" for asset in assets))
        self.assertTrue(all(asset.release_date == "2026-10-25" for asset in assets))

    def test_errors_when_latest_release_has_no_ipa_assets(self):
        release = {
            "tag_name": "v2.1",
            "assets": [
                {
                    "name": "source.zip",
                    "browser_download_url": "https://github.com/example/demo/source.zip",
                }
            ],
        }
        with (
            patch(
                "app.github.urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(release).encode()),
            ),
            self.assertRaisesRegex(GitHubReleaseError, "contains no IPA assets"),
        ):
            latest_ipa_assets("https://github.com/example/demo")


if __name__ == "__main__":
    unittest.main()
