"""Compile robops.exe et robops-setup.exe avec PyInstaller (sous Windows).

Sur ton PC, depuis le dossier robops (Python 3.11+ requis) :

    python exe/build.py --install

  → compile robops.exe, le copie dans %LOCALAPPDATA%\\Programs\\robops et ajoute
    ce dossier à ton PATH. Ouvre ensuite un nouveau terminal : `robops --version`.

L'exécutable se met aussi à jour tout seul depuis les releases GitHub de
StarLightFr51/robops (dépôt gravé dans _build_info.py). --repo owner/nom permet
d'en viser un autre, c'est ce que fait release.yml avec $GITHUB_REPOSITORY.

PyInstaller ne sait pas produire un .exe Windows depuis Linux ou macOS.
Résultat dans dist/ : robops.exe, robops-setup.exe et SHA256SUMS.txt
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_INFO = ROOT / "src" / "robops" / "_build_info.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args],
                   check=True)


def pyinstaller(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                    "--console", *args], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="dépôt GitHub des releases (owner/nom) → active la mise à jour automatique")
    parser.add_argument("--install", action="store_true",
                        help="installe robops.exe sur ce PC et l'ajoute au PATH (Windows)")
    args = parser.parse_args()
    if args.repo and "/" not in args.repo:
        parser.error("--repo doit être de la forme owner/nom")
    if args.install and os.name != "nt":
        parser.error("--install ne fonctionne que sous Windows")
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 ou plus récent est nécessaire")

    # 0. Outils de compilation.
    print("Installation des dépendances (requests, PyInstaller)…", flush=True)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        pip("pyinstaller==6.*")
    pip(str(ROOT))

    # 1. Graver le dépôt (si fourni) puis réinstaller le paquet pour que PyInstaller voie ce code.
    original = BUILD_INFO.read_text(encoding="utf-8")
    if args.repo:
        BUILD_INFO.write_text(f'REPOSITORY = "{args.repo}"\n', encoding="utf-8")
    try:
        pip("--no-deps", "--force-reinstall", str(ROOT))
        # 2. Compilation des deux exécutables.
        print("Compilation de robops.exe et robops-setup.exe (1 à 2 minutes)…", flush=True)
        pyinstaller("--name", "robops", "--collect-data", "robops", "exe/entry_cli.py")
        pyinstaller("--name", "robops-setup", "exe/entry_setup.py")
    finally:
        BUILD_INFO.write_text(original, encoding="utf-8")

    # 3. Empreintes SHA-256 (vérifiées par l'installateur et la mise à jour automatique).
    dist = ROOT / "dist"
    binaries = sorted(p for p in dist.iterdir() if p.name.startswith("robops") and p.is_file()
                      and p.suffix in (".exe", ""))
    lines = [f"{sha256(p)}  {p.name}" for p in binaries]
    (dist / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)

    # 4. Installation locale (optionnelle).
    if args.install:
        import importlib
        importlib.invalidate_caches()   # le paquet vient d'être installé par pip
        from robops import __version__
        from robops.installer import install_local
        from robops.winsetup import default_install_dir

        install_dir = default_install_dir()
        path_changed = install_local(dist / "robops.exe", install_dir,
                                     setup_exe=dist / "robops-setup.exe", version=__version__)
        print(f"\n✔ robops {__version__} installé dans {install_dir}")
        if path_changed:
            print("✔ Dossier ajouté à ton PATH")
        print("Ouvre un NOUVEAU terminal puis tape :  robops --version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
