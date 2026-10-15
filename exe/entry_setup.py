"""Point d'entrée de robops-setup.exe (compilé par PyInstaller dans release.yml)."""

import sys

from robops.installer import main

if __name__ == "__main__":
    sys.exit(main())
