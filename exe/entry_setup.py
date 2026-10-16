"""Entry point of robops-setup.exe (compiled by PyInstaller in release.yml)."""

import sys

from robops.installer import main

if __name__ == "__main__":
    sys.exit(main())
