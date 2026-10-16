"""Entry point of robops.exe (compiled by PyInstaller in release.yml)."""

import sys

from robops.cli import main

if __name__ == "__main__":
    sys.exit(main())
