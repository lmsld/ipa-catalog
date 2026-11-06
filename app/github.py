"""Resolve IPA assets from public GitHub repositories."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_RELEASE_RESPONSE_SIZE = 5 * 1024 * 1024


class GitHubReleaseError(ValueError):
    """Raised when a GitHub repository has no usable latest-release IPA."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    release_tag: str
    release_date: str


def parse_repository_url(url: str) -> tuple[str, str, str]:
    """Return canonical repository URL, owner, and repository name."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise GitHubReleaseError("The GitHub repository URL is invalid.") from exc

    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise GitHubReleaseError(
            "Enter a GitHub repository URL like https://github.com/owner/repo."
        )

    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or owner in (".", "..") or repository in (".", ".."):
        raise GitHubReleaseError("The GitHub repository URL is invalid.")

    return f"https://github.com/{owner}/{repository}", owner, repository


def latest_ipa_assets(repository_url: str) -> list[ReleaseAsset]:
    """Fetch the latest GitHub release and return its IPA assets in release order."""
    canonical_url, owner, repository = parse_repository_url(repository_url)
    api_url = (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
        "/releases/latest"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "IPA-Catalog/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(MAX_RELEASE_RESPONSE_SIZE + 1)
        if len(data) > MAX_RELEASE_RESPONSE_SIZE:
            raise GitHubReleaseError("The GitHub release response is too large.")
        release = json.loads(data)
    except GitHubReleaseError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GitHubReleaseError(
                "This GitHub repository has no published latest release."
            ) from exc
        if exc.code == 403:
            raise GitHubReleaseError(
                "GitHub API rate limit reached. Try again later."
            ) from exc
        raise GitHubReleaseError(
            f"GitHub could not load the latest release (HTTP {exc.code})."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GitHubReleaseError(f"Could not load the GitHub release: {exc}") from exc

    if not isinstance(release, dict) or release.get("draft"):
        raise GitHubReleaseError("GitHub did not return a published latest release.")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise GitHubReleaseError("The latest GitHub release has no tag name.")
    published_at = release.get("published_at")
    date = published_at[:10] if isinstance(published_at, str) and len(published_at) >= 10 else ""
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise GitHubReleaseError("The latest GitHub release has no assets.")

    ipa_assets = []
    expected_prefix = f"{canonical_url}/releases/download/"
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        download_url = asset.get("browser_download_url")
        if (
            isinstance(name, str)
            and name.lower().endswith(".ipa")
            and isinstance(download_url, str)
            and download_url.startswith(expected_prefix)
        ):
            ipa_assets.append(
                ReleaseAsset(
                    name=name,
                    download_url=download_url,
                    release_tag=tag,
                    release_date=date,
                )
            )
    if not ipa_assets:
        raise GitHubReleaseError("The latest GitHub release contains no IPA assets.")
    return ipa_assets
