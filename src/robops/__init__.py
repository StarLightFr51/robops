"""robops — Roblox DevOps: publishing places and data via Open Cloud."""

# Single source of truth for the version (pyproject.toml reads it from here).
# To publish a release: bump this number, then push the tag v<version>.
__version__ = "1.2.2"

# GitHub repository whose releases the installer and the updater use when none
# is configured (ROBOPS_REPO) or burned in at build time (_build_info.py).
DEFAULT_REPOSITORY = "StarLightFr51/robops"
