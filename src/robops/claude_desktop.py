"""Claude Desktop on this computer: where it keeps its config and logs, and adding robops to it.

`robops mcp install` adds the robops MCP server (robops mcp serve) to Claude
Desktop's claude_desktop_config.json. Claude then has a tool to save its player
simulation reports straight into the project, where the dashboard reads them.

Windows: the official installer is an MSIX package, and Windows redirects the
app's %APPDATA% to %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming.
Claude Desktop reads its config there, while its "Edit Config" button may open
the non-redirected %APPDATA%\\Claude file. Both are handled.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import updater

CONFIG_NAME = "claude_desktop_config.json"
DEFAULT_NAME = "robops"
BACKUP_SUFFIX = ".robops-backup"
STUDIO_RE = re.compile(r"roblox|studio|rbx", re.IGNORECASE)
NOT_FOUND = ("Claude Desktop was not found on this computer (no claude_desktop_config.json folder). "
             "Install it from claude.ai/download and open it once, or give the file with --config-file.")
RESTART = ("Quit Claude Desktop completely (tray icon or menu bar → Quit, closing the window is not enough) "
           "and open it again: it loads its MCP servers at startup.")
STUDIO_HINT = ("In Roblox Studio: Assistant → … → Manage MCP Servers → turn on \"Enable Studio as MCP server\", "
               "then under Quick connect turn on Claude Desktop.")


class ClaudeDesktopError(Exception):
    pass


def _kind(platform: str | None) -> str:
    p = platform or sys.platform
    return "windows" if p.startswith("win") else "mac" if p == "darwin" else "linux"


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            ident = os.path.normcase(str(path.resolve()))
        except OSError:
            ident = os.path.normcase(str(path))
        if ident not in seen:
            seen.add(ident)
            out.append(path)
    return out


def app_dirs(*, environ: Mapping[str, str] | None = None, home: Path | None = None,
             platform: str | None = None) -> list[Path]:
    """Folders where Claude Desktop may keep its config (they may not exist)."""
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    kind = _kind(platform)
    dirs: list[Path] = []
    if kind == "windows":
        local = Path(environ["LOCALAPPDATA"]) if environ.get("LOCALAPPDATA") else home / "AppData" / "Local"
        packages = local / "Packages"
        if packages.is_dir():
            dirs += sorted(packages.glob("Claude_*/LocalCache/Roaming/Claude"))     # MSIX install
        roaming = Path(environ["APPDATA"]) if environ.get("APPDATA") else home / "AppData" / "Roaming"
        dirs.append(roaming / "Claude")
    elif kind == "mac":
        dirs.append(home / "Library" / "Application Support" / "Claude")
    else:
        dirs.append(Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude")
    return _unique(dirs)


def log_dirs(*, environ: Mapping[str, str] | None = None, home: Path | None = None,
             platform: str | None = None) -> list[Path]:
    """Where Claude Desktop writes mcp.log and mcp-server-<name>.log."""
    if _kind(platform) == "mac":
        return [(home or Path.home()) / "Library" / "Logs" / "Claude"]
    return [d / "logs" for d in app_dirs(environ=environ, home=home, platform=platform)]


def config_files(**where: Any) -> list[Path]:
    """The config files Claude Desktop may read: one per folder that exists."""
    return [d / CONFIG_NAME for d in app_dirs(**where) if d.is_dir()]


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")             # Notepad may add a BOM
    except OSError as exc:
        raise ClaudeDesktopError(f"Cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ClaudeDesktopError(f"{path} is not valid JSON ({exc}). Fix it or remove it first: "
                                 "robops did not change it.") from exc
    if not isinstance(data, dict):
        raise ClaudeDesktopError(f"{path} must contain a JSON object: robops did not change it.")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ClaudeDesktopError(f"\"mcpServers\" in {path} is not an object: robops did not change it.")
    return data


def write_config(path: Path, data: dict) -> Path | None:
    """Writes the config (atomically), after copying the old one next to it. Returns the copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".robops-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def server_entry(project: Path) -> dict:
    """How Claude Desktop starts the robops MCP server for this project."""
    folder = str(project.resolve())
    if updater.is_frozen():
        return {"command": str(updater.current_executable()), "args": ["mcp", "serve", "--project", folder]}
    # Not resolved: a virtual environment's python is a link, resolving it would leave the venv.
    return {"command": sys.executable, "args": ["-m", "robops", "mcp", "serve", "--project", folder]}


def _quote(arg: str) -> str:
    return f'"{arg}"' if not arg or re.search(r'[\s"]', arg) else arg


def claude_code_command(entry: dict, name: str = DEFAULT_NAME) -> str:
    """The same server for Claude Code (which does not read Claude Desktop's config)."""
    return " ".join(["claude", "mcp", "add", name, "--", _quote(entry["command"]), *map(_quote, entry["args"])])


def manual_snippet(entry: dict, name: str = DEFAULT_NAME) -> str:
    return json.dumps({"mcpServers": {name: entry}}, indent=2, ensure_ascii=False)


def _project_of(entry: dict) -> str | None:
    args = entry.get("args")
    if not isinstance(args, list):
        return None
    args = [str(a) for a in args]
    if "--project" in args[:-1]:
        return args[args.index("--project") + 1]
    return None


def _same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def studio_servers(servers: Mapping[str, Any], *, exclude: str = DEFAULT_NAME) -> list[str]:
    """Names of the MCP servers that are Roblox Studio's (by name, or by the program they start)."""
    names = []
    for name, entry in servers.items():
        if name == exclude:
            continue
        command = str(entry.get("command") or "") if isinstance(entry, dict) else ""
        program = re.split(r"[\\/]", command)[-1]
        if STUDIO_RE.search(name) or STUDIO_RE.search(program):
            names.append(name)
    return names


@dataclass
class Change:
    path: Path
    status: str                       # added | updated | unchanged | removed | absent
    previous_project: str | None = None
    backup: Path | None = None


def install(project: Path, *, name: str = DEFAULT_NAME, files: list[Path] | None = None,
            entry: dict | None = None) -> list[Change]:
    """Adds (or updates) the robops server in every Claude Desktop config found."""
    files = config_files() if files is None else files
    if not files:
        raise ClaudeDesktopError(NOT_FOUND)
    entry = entry or server_entry(project)
    loaded = [(path, read_config(path)) for path in files]   # every file is checked before one is written
    changes = []
    for path, data in loaded:
        servers = data.setdefault("mcpServers", {})
        previous = servers.get(name)
        new = {**previous, **entry} if isinstance(previous, dict) else dict(entry)   # keeps e.g. an "env" added by hand
        if new == previous:
            changes.append(Change(path, "unchanged"))
            continue
        servers[name] = new
        backup = write_config(path, data)
        other = _project_of(previous) if isinstance(previous, dict) else None
        changes.append(Change(path, "updated" if previous is not None else "added",
                              other if other and not _same_folder(other, project) else None, backup))
    return changes


def uninstall(*, name: str = DEFAULT_NAME, files: list[Path] | None = None) -> list[Change]:
    files = config_files() if files is None else files
    changes = []
    for path in files:
        data = read_config(path)
        servers = data.get("mcpServers") or {}
        if name not in servers:
            changes.append(Change(path, "absent"))
            continue
        del servers[name]
        changes.append(Change(path, "removed", backup=write_config(path, data)))
    return changes


def status(project: Path | None = None, *, name: str = DEFAULT_NAME, files: list[Path] | None = None) -> dict:
    """What Claude Desktop will load: the robops entry and the Roblox Studio server, per config file."""
    files = config_files() if files is None else files
    expected = server_entry(project) if project is not None else None
    configs = []
    for path in files:
        item: dict[str, Any] = {"path": str(path), "exists": path.is_file(), "robops": None, "studio": [], "error": None}
        try:
            data = read_config(path)
        except ClaudeDesktopError as exc:
            item["error"] = str(exc)
            configs.append(item)
            continue
        servers = data.get("mcpServers") or {}
        entry = servers.get(name)
        if isinstance(entry, dict):
            command = str(entry.get("command") or "")
            folder = _project_of(entry)
            item["robops"] = {
                "command": command, "project": folder,
                "command_found": bool(command) and (Path(command).is_file() or shutil.which(command) is not None),
                "this_project": project is not None and folder is not None and _same_folder(folder, project),
                "up_to_date": expected is not None and command == expected["command"]
                              and entry.get("args") == expected["args"],
            }
        item["studio"] = studio_servers(servers, exclude=name)
        configs.append(item)
    usable = [c for c in configs if not c["error"]]
    return {
        "found": bool(files), "name": name, "configs": configs,
        "installed": bool(usable) and all(c["robops"] and c["robops"]["this_project"] and c["robops"]["command_found"]
                                          for c in usable),
        "studio": sorted({s for c in configs for s in c["studio"]}),
        "entry": expected,
    }
