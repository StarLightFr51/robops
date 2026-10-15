"""Compile robops.exe et robops-setup.exe avec PyInstaller.

Utilisé par .github/workflows/release.yml, mais peut aussi se lancer à la main
sur un PC Windows (PyInstaller ne sait pas compiler un .exe depuis Linux/macOS) :

    pip install . pyinstaller
    python exe/build.py --repo mon-compte/robops

Résultat : dist/robops.exe, dist/robops-setup.exe et dist/SHA256SUMS.txt
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_INFO = ROOT / "src" / "robops" / "_build_info.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pyinstaller(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                    "--console", *args], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="dépôt GitHub qui publie les releases (owner/nom)")
    args = parser.parse_args()
    if "/" not in args.repo:
        parser.error("--repo doit être de la forme owner/nom")

    # 1. Graver le dépôt dans le code puis réinstaller le paquet pour que PyInstaller le voie.
    original = BUILD_INFO.read_text(encoding="utf-8")
    BUILD_INFO.write_text(f'REPOSITORY = "{args.repo}"\n', encoding="utf-8")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall",
                        str(ROOT)], check=True)
        # 2. Compilation des deux exécutables.
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
