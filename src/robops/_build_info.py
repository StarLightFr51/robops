"""Information injected when the executable is compiled.

The release workflow (.github/workflows/release.yml) rewrites this file before
calling PyInstaller. With a pip install, the values stay empty and
automatic updating is disabled (update with pip instead).
"""

# GitHub repository that publishes the releases, e.g. "my-account/robops".
REPOSITORY = ""
