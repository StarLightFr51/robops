import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API = "https://api.github.com"
USER_AGENT = "GitHub-Latest-Release-Downloader/1.0"


def parse_repository(value):
    """Accept owner/repo or a GitHub repository URL."""
    value = value.strip().rstrip("/")

    if value.startswith(("https://", "http://")):
        parsed = urllib.parse.urlparse(value)

        if parsed.hostname not in ("github.com", "www.github.com"):
            raise ValueError("Please provide a URL from github.com.")

        parts = parsed.path.strip("/").split("/")
    else:
        parts = value.split("/")

    if len(parts) < 2:
        raise ValueError("Repository must be in owner/repo format.")

    owner, repo = parts[0], parts[1]
    repo = re.sub(r"\.git$", "", repo)

    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("Invalid GitHub owner name.")

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository name.")

    return owner, repo


def request(url):
    """Make a GitHub API or download request."""
    headers = {"User-Agent": USER_AGENT}

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if url.startswith(API + "/"):
        headers["Accept"] = "application/vnd.github+json"

    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=60)


def get_latest_release(owner, repo):
    """Retrieve the latest published, non-prerelease release."""
    url = f"{API}/repos/{owner}/{repo}/releases/latest"

    with request(url) as response:
        release = json.load(response)

    return release


def download_file(url, destination):
    """Stream a download to disk without loading it all into memory."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(destination.name + ".part")

    try:
        with request(url) as response, open(temporary, "wb") as output:
            total = response.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            downloaded = 0

            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break

                output.write(chunk)
                downloaded += len(chunk)

                if total:
                    percent = downloaded * 100 / total
                    print(
                        f"\rDownloading: {percent:6.2f}% "
                        f"({downloaded:,}/{total:,} bytes)",
                        end="",
                        flush=True,
                    )
                else:
                    print(
                        f"\rDownloaded {downloaded:,} bytes",
                        end="",
                        flush=True,
                    )

        if total is not None and downloaded != total:
            raise IOError("Download size does not match Content-Length.")

        temporary.replace(destination)
        print(f"\nSaved: {destination.resolve()}")

    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Download assets from the latest GitHub release."
    )
    parser.add_argument(
        "repository",
        help="GitHub URL or owner/repo, e.g. owner/project",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download every asset attached to the release.",
    )
    parser.add_argument(
        "--source",
        choices=("zip", "tar.gz"),
        help="Download the release's source archive instead of assets.",
    )
    parser.add_argument(
        "--output",
        default="downloads",
        help="Output directory (default: downloads).",
    )

    args = parser.parse_args()

    try:
        owner, repo = parse_repository(args.repository)
        release = get_latest_release(owner, repo)

        tag = release.get("tag_name", "unknown")
        name = release.get("name") or tag

        print(f"\nRepository: {owner}/{repo}")
        print(f"Release:    {name}")
        print(f"Tag:        {tag}")
        print(f"URL:        {release.get('html_url', '')}\n")

        assets = release.get("assets", [])
        output_dir = Path(args.output)

        if args.source:
            archive_url = release.get(
                "tarball_url" if args.source == "tar.gz" else "zipball_url"
            )
            filename = f"{repo}-{tag}-source.{args.source}"

            if not archive_url:
                raise RuntimeError("GitHub did not provide a source archive.")

            download_file(archive_url, output_dir / filename)
            return

        if not assets:
            print("This release has no attached assets.")
            print("Downloading the source code ZIP archive instead.")
            download_file(
                release["zipball_url"],
                output_dir / f"{repo}-{tag}-source.zip",
            )
            return

        print("Available assets:")
        for index, asset in enumerate(assets, start=1):
            size_mb = asset.get("size", 0) / (1024 * 1024)
            print(f"  {index}. {asset['name']} ({size_mb:.2f} MB)")

        if args.all:
            selected = assets
        else:
            choice = input(
                "\nEnter asset number, 'a' for all, or 'q' to quit: "
            ).strip().lower()

            if choice == "q":
                print("Cancelled.")
                return

            if choice == "a":
                selected = assets
            else:
                try:
                    index = int(choice)
                    if not 1 <= index <= len(assets):
                        raise ValueError
                    selected = [assets[index - 1]]
                except ValueError:
                    raise ValueError("Invalid asset selection.")

        output_dir.mkdir(parents=True, exist_ok=True)

        for asset in selected:
            # Prevent an asset filename from escaping the output directory.
            filename = Path(asset["name"]).name
            if filename in ("", ".", ".."):
                raise ValueError("Invalid asset filename.")

            print(f"\nDownloading {filename}...")
            download_file(
                asset["browser_download_url"],
                output_dir / filename,
            )

        print("\nAll requested downloads completed.")

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(
                "Error: Repository or latest release not found. "
                "The repository may be private or have no published release.",
                file=sys.stderr,
            )
        elif exc.code == 403:
            print(
                "Error: GitHub API rate limit or access restriction. "
                "Try again later or set GITHUB_TOKEN.",
                file=sys.stderr,
            )
        else:
            print(f"GitHub HTTP error: {exc}", file=sys.stderr)
        sys.exit(1)

    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, RuntimeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()