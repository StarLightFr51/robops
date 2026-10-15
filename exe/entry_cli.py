"""Point d'entrée de robops.exe (compilé par PyInstaller dans release.yml)."""

import sys

from robops.cli import main

if __name__ == "__main__":
    sys.exit(main())
