"""Mise à jour de robops.exe à partir des GitHub Releases du dépôt.

Chaque release publiée par .github/workflows/release.yml contient :
  - robops.exe         l'outil
  - robops-setup.exe   l'installateur
  - SHA256SUMS.txt     les empreintes SHA-256 des deux fichiers

Le remplacement de l'exécutable en cours d'utilisation repose sur le fait que
Windows autorise à *renommer* un .exe lancé (mais pas à l'écraser) : l'ancien
devient robops.exe.old, le nouveau prend sa place, et l'ancien est supprimé au
lancement suivant.

Dépôt privé : définis ROBOPS_GITHUB_TOKEN (token avec accès en lecture au dépôt).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import __version__, _build_info

GITHUB_API = "https://api.github.com"   # surchargeable via ROBOPS_GITHUB_API (GitHub Enterprise, tests)
EXE_NAME = "robops.exe"
SETUP_NAME = "robops-setup.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"
CHECK_INTERVAL = 24 * 3600          # une vérification automatique par jour au maximum


class UpdateError(Exception):
    pass


@dataclass
class Release:
    tag: str
    version: str
    assets: dict[str, dict]
    html_url: str = ""


# ------------------------------------------------------------- paramètres

def repository() -> str:
    return (os.environ.get("ROBOPS_REPO") or _build_info.REPOSITORY or "").strip()


def github_token() -> str | None:
    return os.environ.get("ROBOPS_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def is_frozen() -> bool:
    """Vrai quand on tourne depuis l'exécutable PyInstaller (et non via pip)."""
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
    """'v1.2.3' → (1, 2, 3). Les suffixes (-beta…) sont ignorés."""
    cleaned = text.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    if not parts:
        raise UpdateError(f"Numéro de version illisible : {text!r}")
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# ------------------------------------------------------------------ GitHub

def make_session(token: str | None = None) -> requests.Session:
    session = requests.Session()
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
        raise UpdateError("Dépôt GitHub inconnu (exécutable compilé sans dépôt, ou ROBOPS_REPO vide).")
    try:
        api = os.environ.get("ROBOPS_GITHUB_API", GITHUB_API).rstrip("/")
        resp = session.get(f"{api}/repos/{repo}/releases/latest", timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateError(f"Impossible de joindre GitHub : {exc}") from exc
    if resp.status_code == 404:
        raise UpdateError(
            f"Aucune release trouvée sur github.com/{repo}. Soit aucune version n'a encore été publiée, "
            "soit le dépôt est privé : définis alors ROBOPS_GITHUB_TOKEN."
        )
    headers = getattr(resp, "headers", None) or {}
    if resp.status_code == 429 or (resp.status_code == 403 and headers.get("X-RateLimit-Remaining") == "0"):
        raise UpdateError("Limite de requêtes GitHub atteinte, réessaie dans une heure.")
    if resp.status_code == 403:
        raise UpdateError("Accès refusé par GitHub (HTTP 403) : pare-feu/proxy, ou token sans accès au dépôt.")
    if resp.status_code != 200:
        raise UpdateError(f"Réponse GitHub inattendue (HTTP {resp.status_code}).")
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
        raise UpdateError(f"La release {release.tag} ne contient pas {name}.")
    # L'URL d'API + Accept octet-stream fonctionne pour les dépôts publics ET privés.
    try:
        resp = session.get(asset["url"], headers={"Accept": "application/octet-stream"},
                           stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise UpdateError(f"Téléchargement de {name} impossible : {exc}") from exc
    if resp.status_code != 200:
        raise UpdateError(f"Téléchargement de {name} refusé (HTTP {resp.status_code}).")
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                fh.write(chunk)
    return dest


def parse_checksums(text: str) -> dict[str, str]:
    """Lit un fichier « <sha256>  <nom> » (format sha256sum, avec ou sans '*')."""
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
    """Télécharge un fichier de la release et vérifie son SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    sums_path = _download(release, CHECKSUMS_NAME, directory / CHECKSUMS_NAME, session=session, timeout=60)
    sums = parse_checksums(sums_path.read_text(encoding="utf-8", errors="replace"))
    if name not in sums:
        raise UpdateError(f"{CHECKSUMS_NAME} ne contient pas d'empreinte pour {name}.")
    path = _download(release, name, directory / name, session=session, timeout=timeout)
    actual = sha256_file(path)
    if actual != sums[name]:
        path.unlink(missing_ok=True)
        raise UpdateError(f"Empreinte SHA-256 incorrecte pour {name} : fichier corrompu, mise à jour annulée.")
    return path


# ------------------------------------------------- remplacement du fichier

def replace_executable(new_file: Path, target: Path) -> None:
    """Remplace target par new_file, même si target est en cours d'exécution."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # Hors Windows, un fichier téléchargé n'est pas exécutable : on reprend les droits de l'ancien.
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
            except OSError:  # un ancien robops tourne encore : on prend un autre nom
                backup = target.with_name(f"{target.name}.old-{int(time.time())}")
        target.replace(backup)
    try:
        shutil.move(str(new_file), str(target))
    except OSError:
        if backup is not None and not target.exists():
            backup.replace(target)   # on remet l'ancienne version
        raise


def cleanup_old(target: Path) -> None:
    """Supprime les anciennes versions laissées par une mise à jour (si plus utilisées)."""
    for leftover in target.parent.glob(target.name + ".old*"):
        try:
            leftover.unlink()
        except OSError:
            pass


def update_executable(release: Release, target: Path, *, session: requests.Session) -> None:
    # Téléchargement dans le même dossier : le déplacement final est instantané.
    with tempfile.TemporaryDirectory(prefix=".robops-update-", dir=target.parent) as tmp:
        new_file = fetch_verified(release, EXE_NAME, Path(tmp), session=session)
        replace_executable(new_file, target)


# --------------------------------------------------- mise à jour automatique

def auto_update_disabled_reason() -> str | None:
    if not is_frozen():
        return "installation via pip"
    if not repository():
        return "dépôt inconnu"
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return "intégration continue"
    if os.environ.get("ROBOPS_NO_UPDATE"):
        return "désactivée par ROBOPS_NO_UPDATE"
    return None


def cleanup_after_update() -> None:
    if is_frozen():
        cleanup_old(current_executable())


def maybe_auto_update(
    *,
    now: float | None = None,
    session: requests.Session | None = None,
    notify: Callable[[str], None] | None = None,
) -> str | None:
    """Vérifie (au plus une fois par jour) et installe une nouvelle version.

    Ne lève jamais d'exception : une mise à jour ratée ne doit pas casser la
    commande que l'utilisateur vient de lancer. Retourne la version installée.
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
    try:
        session = session or make_session(github_token())
        release = latest_release(repository(), session=session, timeout=5)
        if not is_newer(release.version, __version__):
            return None
        update_executable(release, current_executable(), session=session)
    except (UpdateError, OSError, requests.RequestException) as exc:
        notify(f"⚠ Mise à jour automatique de robops impossible : {exc}")
        return None
    _save_state({**state, "last_check": now, "installed": release.version})
    notify(f"✔ robops mis à jour : {__version__} → {release.version} (active au prochain lancement).")
    return release.version


def record_check(now: float | None = None) -> None:
    _save_state({**_load_state(), "last_check": time.time() if now is None else now})
