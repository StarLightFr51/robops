"""Compiles robops and robops-setup.exe with PyInstaller (on Windows).

On your PC, from the robops folder (Python 3.11+ required):

    python exe/build.py --install

  → compiles robops, copies it to %LOCALAPPDATA%\\Programs\\robops (robops.exe and
    the _internal folder of its libraries) and adds that folder to your PATH.
    Then open a new terminal: `robops --version`.

The executables install and update themselves from the GitHub releases of
DEFAULT_REPOSITORY (see src/robops/__init__.py). Use --repo owner/name to burn
in another repository.

PyInstaller cannot produce a Windows .exe from Linux or macOS.
Result in dist/:
  folder/robops/       robops.exe + _internal (what is installed: starts in ~0.3 s)
  robops-windows.zip   that folder, published in the releases
  robops.exe           the same tool as a single file, for robops ≤ 1.7 updating themselves
  robops-setup.exe     the installer
  SHA256SUMS.txt
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_INFO = ROOT / "src" / "robops" / "_build_info.py"

sys.path.insert(0, str(ROOT / "src"))
from robops import DEFAULT_REPOSITORY  # noqa: E402  (robops/__init__.py has no dependencies)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args],
                   check=True)


def pyinstaller(mode: str, name: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", f"--{mode}", "--console",
                    "--name", name, "--workpath", f"build/{name}-{mode}", "--specpath", f"build/{name}-{mode}",
                    *args], cwd=ROOT, check=True)


def zip_folder(folder: Path, archive: Path) -> None:
    """robops-windows.zip: robops.exe and _internal at the root, in a stable order."""
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(folder).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY,
                        help=f"GitHub repository of the releases (owner/name) burned into the executables "
                             f"(default: {DEFAULT_REPOSITORY})")
    parser.add_argument("--install", action="store_true",
                        help="install robops.exe on this PC and add it to the PATH (Windows)")
    args = parser.parse_args()
    if "/" not in args.repo:
        parser.error("--repo must be of the form owner/name")
    if args.install and os.name != "nt":
        parser.error("--install only works on Windows")
    if sys.version_info < (3, 11):  # noqa: UP036 — this script may be run by an older Python
        parser.error("Python 3.11 or newer is required")

    # 0. Build tools.
    print("Installing dependencies (requests, PyInstaller)…", flush=True)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        pip("pyinstaller==6.*")
    pip(str(ROOT))

    # 1. Burn in the repository, then reinstall the package so PyInstaller sees this code.
    original = BUILD_INFO.read_text(encoding="utf-8")
    BUILD_INFO.write_text(f'REPOSITORY = "{args.repo}"\n', encoding="utf-8")
    try:
        pip("--no-deps", "--force-reinstall", str(ROOT))
        # 2. Compile: robops as a folder (installed), as a single file (older updaters), and the setup.
        print("Compiling robops and robops-setup.exe (2 to 4 minutes)…", flush=True)
        pyinstaller("onedir", "robops", "--contents-directory", "_internal", "--collect-data", "robops",
                    "--distpath", "dist/folder", "exe/entry_cli.py")
        pyinstaller("onefile", "robops", "--collect-data", "robops", "--distpath", "dist", "exe/entry_cli.py")
        pyinstaller("onefile", "robops-setup", "--distpath", "dist", "exe/entry_setup.py")
    finally:
        BUILD_INFO.write_text(original, encoding="utf-8")

    dist = ROOT / "dist"
    zip_folder(dist / "folder" / "robops", dist / "robops-windows.zip")

    # 3. SHA-256 checksums (verified by the installer and the automatic update).
    binaries = sorted(p for p in dist.iterdir() if p.name.startswith("robops") and p.is_file()
                      and p.suffix in (".exe", ".zip", ""))
    lines = [f"{sha256(p)}  {p.name}" for p in binaries]
    (dist / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)

    # 4. Local installation (optional).
    if args.install:
        import importlib
        importlib.invalidate_caches()   # the package was just installed by pip
        from robops import __version__
        from robops.installer import install_local
        from robops.winsetup import default_install_dir

        install_dir = default_install_dir()
        path_changed = install_local(dist / "folder" / "robops", install_dir,
                                     setup_exe=dist / "robops-setup.exe", version=__version__)
        print(f"\n✔ robops {__version__} installed in {install_dir} (robops.exe + _internal)")
        if path_changed:
            print("✔ Folder added to your PATH")
        print("Open a NEW terminal then type:  robops --version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
