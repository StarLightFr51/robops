"""Installing and updating robops from the repository's GitHub Releases.

Each release published by .github/workflows/release.yml contains:
  - robops-windows.zip the tool as a folder: robops.exe + _internal\\ (its libraries).
                       Installed in %LOCALAPPDATA%\\Programs\\robops, it starts in about
                       0.3 s, against 1.2 s or more for a single .exe, which unpacks
                       its libraries into a temporary folder at every launch.
  - robops.exe         the same tool as a single file (for robops 1.7 and older,
                       whose update downloads it; they move to the folder on their
                       next `robops self-update`)
  - robops-setup.exe   the installer
  - SHA256SUMS.txt     the SHA-256 checksums of these files

Replacing the files of a running robops relies on the fact that Windows allows
*renaming* a running .exe or a loaded library (but not overwriting or deleting
it): each old file becomes <name>.old, the new one takes its place, and the old
ones are deleted on a later launch, once no robops uses them any more.

Automatic check: at most once a day, robops *tells* the user that a new version
exists; `robops self-update` installs it. Silent installation (the downloaded
files replace the current ones without asking) is opt-in with
ROBOPS_AUTO_UPDATE=1: the SHA-256 checksums come from the same release, so they
detect a corrupted download but not a compromised GitHub account. Releases also
carry a build provenance attestation: `gh attestation verify robops-windows.zip --repo <owner>/robops`.

Private repository: set ROBOPS_GITHUB_TOKEN (a token with read access to the repository).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import DEFAULT_REPOSITORY, __version__, _build_info

if TYPE_CHECKING:
    import requests
else:
    from ._lazy import requests  # imported on first use: faster startup

GITHUB_API = "https://api.github.com"   # overridable via ROBOPS_GITHUB_API (GitHub Enterprise, tests)
EXE_NAME = "robops.exe"
ZIP_NAME = "robops-windows.zip"     # robops.exe + _internal\ (the libraries)
CONTENTS_DIR = "_internal"          # PyInstaller --contents-directory
SETUP_NAME = "robops-setup.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"
CLEANUP_MARKER = ".robops-cleanup"  # old files are waiting to be deleted
_BACKUP_RE = re.compile(r"\.old(-\d+)?$")
CHECK_INTERVAL = 24 * 3600          # at most one automatic check per day


class UpdateError(Exception):
    pass


@dataclass
class Release:
    tag: str
    version: str
    assets: dict[str, dict]
    html_url: str = ""


# ------------------------------------------------------------- settings

def configured_repository() -> str:
    """Repository set explicitly (ROBOPS_REPO or burned in at build time), '' if none."""
    return (os.environ.get("ROBOPS_REPO") or _build_info.REPOSITORY or "").strip()


def repository() -> str:
    """Repository to install/update from: the configured one, else the project default."""
    return configured_repository() or DEFAULT_REPOSITORY


def github_token() -> str | None:
    return os.environ.get("ROBOPS_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def is_frozen() -> bool:
    """True when running from the PyInstaller executable (and not via pip)."""
    return bool(getattr(sys, "frozen", False))


def current_executable() -> Path:
    return Path(sys.executable).resolve()


def state_dir() -> Path:
    override = os.environ.get("ROBOPS_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "robops"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "robops"


def _load_state() -> dict:
    try:
        return json.loads((state_dir() / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "update.json").write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------- versions

def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' → (1, 2, 3). Suffixes (-beta…) are ignored."""
    cleaned = text.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    if not parts:
        raise UpdateError(f"Unreadable version number: {text!r}")
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# ------------------------------------------------------------------ GitHub

def make_session(token: str | None = None) -> requests.Session:
    session = requests.Session()
    # The dashboard calls GitHub from up to 16 threads: as many kept-alive connections.
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=16))
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"robops/{__version__}",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def latest_release(repo: str, *, session: requests.Session, timeout: float = 15) -> Release:
    if not repo or "/" not in repo:
        raise UpdateError("Unknown GitHub repository (expected owner/name, check ROBOPS_REPO).")
    try:
        api = os.environ.get("ROBOPS_GITHUB_API", GITHUB_API).rstrip("/")
        resp = session.get(f"{api}/repos/{repo}/releases/latest", timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateError(f"Unable to reach GitHub: {exc}") from exc
    if resp.status_code == 404:
        raise UpdateError(
            f"No release found on github.com/{repo}. Either no version has been published yet, "
            "or the repository is private: in that case set ROBOPS_GITHUB_TOKEN."
        )
    headers = getattr(resp, "headers", None) or {}
    if resp.status_code == 429 or (resp.status_code == 403 and headers.get("X-RateLimit-Remaining") == "0"):
        raise UpdateError("GitHub rate limit reached, try again in an hour.")
    if resp.status_code == 403:
        raise UpdateError("Access denied by GitHub (HTTP 403): firewall/proxy, or a token without access to the repository.")
    if resp.status_code != 200:
        raise UpdateError(f"Unexpected GitHub response (HTTP {resp.status_code}).")
    payload = resp.json()
    tag = payload.get("tag_name") or ""
    return Release(
        tag=tag,
        version=".".join(str(n) for n in parse_version(tag)),
        assets={a["name"]: a for a in payload.get("assets", []) if "name" in a},
        html_url=payload.get("html_url", ""),
    )


def _download(release: Release, name: str, dest: Path, *, session: requests.Session, timeout: float) -> Path:
    asset = release.assets.get(name)
    if not asset:
        raise UpdateError(f"Release {release.tag} does not contain {name}.")
    # The API URL + Accept octet-stream works for public AND private repositories.
    try:
        resp = session.get(asset["url"], headers={"Accept": "application/octet-stream"},
                           stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateError(f"Unable to download {name}: {exc}") from exc
    if resp.status_code != 200:
        raise UpdateError(f"Download of {name} refused (HTTP {resp.status_code}).")
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                fh.write(chunk)
    return dest


def parse_checksums(text: str) -> dict[str, str]:
    """Reads a "<sha256>  <name>" file (sha256sum format, with or without '*')."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*").strip()] = parts[0].lower()
    return sums


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_verified(release: Release, name: str, directory: Path, *,
                   session: requests.Session, timeout: float = 300) -> Path:
    """Downloads a file from the release and verifies its SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    sums_path = _download(release, CHECKSUMS_NAME, directory / CHECKSUMS_NAME, session=session, timeout=60)
    sums = parse_checksums(sums_path.read_text(encoding="utf-8", errors="replace"))
    if name not in sums:
        raise UpdateError(f"{CHECKSUMS_NAME} has no checksum for {name}.")
    path = _download(release, name, directory / name, session=session, timeout=timeout)
    actual = sha256_file(path)
    if actual != sums[name]:
        path.unlink(missing_ok=True)
        raise UpdateError(f"Incorrect SHA-256 checksum for {name}: corrupted file, update cancelled.")
    return path


# ------------------------------------------------- replacing the file

def replace_executable(new_file: Path, target: Path) -> None:
    """Replaces target with new_file, even if target is running."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # Outside Windows, a downloaded file is not executable: copy the permissions of the old one.
        try:
            shutil.copymode(target, new_file) if target.exists() else os.chmod(new_file, 0o755)
        except OSError:
            pass
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".old")
        if backup.exists():
            try:
                backup.unlink()
            except OSError:  # an old robops is still running: use another name
                backup = target.with_name(f"{target.name}.old-{int(time.time())}")
        target.replace(backup)
    try:
        shutil.move(str(new_file), str(target))
    except OSError:
        if backup is not None and not target.exists():
            backup.replace(target)   # put the old version back
        raise


def cleanup_old(target: Path) -> None:
    """Deletes the old versions left behind by an update (if no longer in use)."""
    for leftover in target.parent.glob(target.name + ".old*"):
        try:
            leftover.unlink()
        except OSError:
            pass


# ------------------------------------------------------ folder install

def folder_layout(install_dir: Path) -> bool:
    """True if robops is installed there as a folder (robops.exe + _internal)."""
    return (install_dir / CONTENTS_DIR).is_dir()


def can_switch_to_folder(release: Release) -> bool:
    """A single-file robops.exe that can move to the faster folder install."""
    return is_frozen() and ZIP_NAME in release.assets and not folder_layout(current_executable().parent)


def extract_release_zip(archive: Path, dest: Path) -> None:
    """Unpacks robops-windows.zip, refusing any path that would land outside dest."""
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise UpdateError(f"{ZIP_NAME} contains an unsafe path ({info.filename}): update cancelled.")
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"{ZIP_NAME} is not a valid zip ({exc}): update cancelled.") from exc
    if not (dest / EXE_NAME).is_file() or not (dest / CONTENTS_DIR).is_dir():
        raise UpdateError(f"{ZIP_NAME} does not contain {EXE_NAME} and {CONTENTS_DIR}: update cancelled.")
    if os.name != "nt":
        os.chmod(dest / EXE_NAME, 0o755)     # zip does not keep the "executable" permission


def _is_backup(path: Path) -> bool:
    return bool(_BACKUP_RE.search(path.name))


def _owned_files(install_dir: Path) -> list[Path]:
    """The files of the installed version: robops.exe and its libraries (not the user's files)."""
    files = [install_dir / EXE_NAME] if (install_dir / EXE_NAME).is_file() else []
    contents = install_dir / CONTENTS_DIR
    if contents.is_dir():
        files += [p for p in contents.rglob("*") if p.is_file() and not _is_backup(p)]
    return files


def _set_aside(target: Path) -> Path:
    """Renames a file to <name>.old (allowed even while it is in use)."""
    backup = target.with_name(target.name + ".old")
    if backup.exists():
        try:
            backup.unlink()
        except OSError:               # an older robops still uses it: another name
            backup = target.with_name(f"{target.name}.old-{time.time_ns()}")
    target.replace(backup)
    return backup


def swap_in(new_root: Path, install_dir: Path) -> None:
    """Puts the files of new_root in install_dir, even while robops runs from there.

    Every file replaced or no longer used is renamed <name>.old (Windows allows
    renaming a running .exe or a loaded library, not overwriting it), then the
    new file is moved in: a rename on the same disk, so the switch is quick.
    robops.exe comes last. On error, everything is put back as it was.
    """
    new_files = sorted((p.relative_to(new_root) for p in new_root.rglob("*") if p.is_file()),
                       key=lambda rel: (rel == Path(EXE_NAME), str(rel)))
    keep = set(new_files)
    gone = [p for p in _owned_files(install_dir) if p.relative_to(install_dir) not in keep]
    set_aside: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        for target in gone:
            set_aside.append((target, _set_aside(target)))
        for rel in new_files:
            target = install_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                set_aside.append((target, _set_aside(target)))
            os.replace(new_root / rel, target)
            placed.append(target)
    except OSError:
        for target in reversed(placed):
            try:
                target.unlink()
            except OSError:
                pass
        for target, backup in reversed(set_aside):
            try:
                backup.replace(target)
            except OSError:
                pass
        raise
    if set_aside:
        try:
            (install_dir / CLEANUP_MARKER).touch()
        except OSError:
            pass


def cleanup_folder(install_dir: Path) -> None:
    """Deletes the old files of a previous update, once no robops uses them (cheap when nothing to do)."""
    marker = install_dir / CLEANUP_MARKER
    if not marker.exists():
        return
    remaining = False
    for path in (install_dir / CONTENTS_DIR).rglob("*.old*"):
        if path.is_file() and _is_backup(path):
            try:
                path.unlink()
            except OSError:          # still loaded by a robops that is running
                remaining = True
    if not remaining:
        try:
            marker.unlink()
        except OSError:
            pass


def install_release(release: Release, install_dir: Path, *, session: requests.Session) -> str:
    """Installs the release's robops in install_dir, even while robops runs from there.

    As a folder (robops.exe + _internal) when the release has robops-windows.zip, which
    also moves a single-file install to the folder; otherwise the single robops.exe of
    older releases. Returns "folder" or "file".
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    # Download and unpack into the same folder: moving the files in is then instantaneous.
    with tempfile.TemporaryDirectory(prefix=".robops-update-", dir=install_dir) as tmp:
        if ZIP_NAME in release.assets:
            archive = fetch_verified(release, ZIP_NAME, Path(tmp) / "download", session=session)
            unpacked = Path(tmp) / "new"
            extract_release_zip(archive, unpacked)
            swap_in(unpacked, install_dir)
            return "folder"
        new_file = fetch_verified(release, EXE_NAME, Path(tmp), session=session)
        replace_executable(new_file, install_dir / EXE_NAME)
        return "file"


def update_executable(release: Release, target: Path, *, session: requests.Session) -> str:
    """Updates the robops whose executable is target (see install_release)."""
    return install_release(release, target.parent, session=session)


# --------------------------------------------------- automatic update

def auto_update_disabled_reason() -> str | None:
    if not is_frozen():
        return "installed via pip"
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return "continuous integration"
    if os.environ.get("ROBOPS_NO_UPDATE"):
        return "disabled by ROBOPS_NO_UPDATE"
    return None


def auto_install_enabled() -> bool:
    """Silent installation of new versions (opt-in): ROBOPS_AUTO_UPDATE=1."""
    return os.environ.get("ROBOPS_AUTO_UPDATE", "").strip().lower() in ("1", "true", "yes", "on")


def cleanup_after_update() -> None:
    if is_frozen():
        exe = current_executable()
        cleanup_old(exe)
        cleanup_folder(exe.parent)


def maybe_auto_update(
    *,
    now: float | None = None,
    session: requests.Session | None = None,
    notify: Callable[[str], None] | None = None,
) -> str | None:
    """Checks (at most once a day) whether a new version exists.

    By default only tells the user (`robops self-update` installs it); installs
    it silently with ROBOPS_AUTO_UPDATE=1. Never raises: a failed check must not
    break the command the user just ran. Returns the installed version, if any.
    """
    notify = notify or (lambda msg: print(msg, file=sys.stderr, flush=True))
    if auto_update_disabled_reason():
        return None
    now = time.time() if now is None else now
    state = _load_state()
    last = state.get("last_check")
    if isinstance(last, (int, float)) and 0 <= now - last < CHECK_INTERVAL:
        return None
    _save_state({**state, "last_check": now})
    install = auto_install_enabled()
    try:
        session = session or make_session(github_token())
        release = latest_release(repository(), session=session, timeout=5)
        newer = is_newer(release.version, __version__)
        if not newer and not can_switch_to_folder(release):
            return None
        if not install:
            notify(f"ℹ robops {release.version} is available (installed: {__version__}). "
                   "Install it with:  robops self-update" if newer else
                   "ℹ robops can start 4 times faster as a folder install. Switch with:  robops self-update")
            return None
        update_executable(release, current_executable(), session=session)
    except (UpdateError, OSError, requests.RequestException) as exc:
        notify(f"⚠ Automatic robops update failed: {exc}" if install
               else f"⚠ Could not check for robops updates: {exc}")
        return None
    _save_state({**state, "last_check": now, "installed": release.version})
    notify(f"✔ robops updated: {__version__} → {release.version} (active on next launch).")
    return release.version


def record_check(now: float | None = None) -> None:
    _save_state({**_load_state(), "last_check": time.time() if now is None else now})
