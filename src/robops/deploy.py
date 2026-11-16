"""Orchestration of a full deployment for one environment.

Steps (in this order):
  1. build          — build command (e.g. rojo build) if configured
  2. preflight      — files present, valid extensions, data JSON readable
  3. publishing     — each place is overwritten by its file (new version)
  4. metadata       — name / description / server size, if set
  5. DataStores     — snapshot then overwrite entries (opt-in: --with-datastores)
  6. restart        — restart the servers to switch to the new version

No network call is made before the preflight has passed, and nothing is
called at all in --dry-run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import output as out
from .client import OpenCloudClient, RobloxApiError, without_nulls
from .config import DatastoreSeed, Environment, PlaceConfig

PLACE_SUFFIXES = (".rbxl", ".rbxlx")     # the only files Open Cloud accepts as a place


class DeployError(Exception):
    pass


@dataclass
class DeployOptions:
    version_type: str | None = None        # overrides environments.<env>.version_type
    only_places: list[str] | None = None   # filter by place name
    skip_build: bool = False
    with_datastores: bool = False
    restart: bool | None = None            # overrides restart_servers
    dry_run: bool = False
    strict_metadata: bool = False          # fail if the metadata update fails


@dataclass
class PlaceOutcome:
    name: str
    place_id: int
    file: Path
    size: int
    version: int | None = None
    metadata: str = "—"


@dataclass
class DeployReport:
    environment: str
    universe_id: int
    version_type: str
    dry_run: bool
    places: list[PlaceOutcome] = field(default_factory=list)
    datastore_writes: int = 0
    datastore_unchanged: int = 0          # entries that already held the value: not rewritten
    restarted: bool = False
    restart_planned: bool = False
    error: str | None = None              # set when the deployment stopped halfway

    def versions(self) -> dict[str, int | None]:
        return {p.name: p.version for p in self.places}

    def published(self) -> list[PlaceOutcome]:
        return [p for p in self.places if p.version is not None]

    def to_markdown(self) -> str:
        mode = " (dry-run)" if self.dry_run else (" — ❌ failed" if self.error else "")
        lines = [
            f"### 🚀 Roblox deployment — `{self.environment}`{mode}",
            "",
            f"Universe `{self.universe_id}` · version type **{self.version_type}**",
            "",
        ]
        if self.error:
            first_line = self.error.splitlines()[0] if self.error else ""
            live = ", ".join(f"{p.name} v{p.version}" for p in self.published()) or "nothing"
            lines += [f"> **Error:** {first_line}", ">", f"> Already live before the failure: **{live}**.", ""]
        lines += [
            "| Place | Place ID | File | Size | Version | Metadata |",
            "|---|---|---|---|---|---|",
        ]
        for p in self.places:
            if p.version is not None:
                version = f"v{p.version}"
            else:
                version = "not published" if self.error else "—"
            lines.append(
                f"| {p.name} | `{p.place_id}` | `{p.file.name}` | {_human(p.size)} | {version} | {p.metadata} |"
            )
        lines.append("")
        if self.datastore_writes or self.datastore_unchanged:
            lines.append(f"DataStores: {self.datastore_writes} entries overwritten"
                         + (f", {self.datastore_unchanged} already up to date" if self.datastore_unchanged else "")
                         + ".")
        if self.dry_run:
            lines.append(f"Server restart: {'planned' if self.restart_planned else 'not planned'}.")
        else:
            lines.append(f"Server restart: {'yes' if self.restarted else 'no'}.")
        return "\n".join(lines)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ------------------------------------------------------------------ helpers

def build_argv(command: str, *, windows: bool | None = None) -> str | list[str]:
    """Command for subprocess.run, without a shell.

    On Windows the string is passed as is (CreateProcess parses it itself): POSIX
    shlex.split would eat the backslashes of paths (build\\game.rbxl → buildgame.rbxl).
    """
    if (os.name == "nt") if windows is None else windows:
        return command
    return shlex.split(command)


def run_build(command: str, cwd: Path) -> None:
    out.info(f"$ {command}")
    try:
        completed = subprocess.run(build_argv(command), cwd=cwd)
    except FileNotFoundError as exc:
        program = exc.filename or command.split()[0]
        raise DeployError(
            f"Build command not found: {program}. "
            "Install it (e.g. Rojo via Rokit) or use --skip-build."
        ) from exc
    if completed.returncode != 0:
        raise DeployError(f"The build failed (exit code {completed.returncode}).")


def load_seed(seed: DatastoreSeed) -> dict[str, Any]:
    if not seed.file.is_file():
        raise DeployError(f"Data file not found for DataStore '{seed.name}': {seed.file}")
    try:
        data = json.loads(seed.file.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise DeployError(f"{seed.file} is not UTF-8 text: save it as UTF-8 ({exc.reason}).") from exc
    except json.JSONDecodeError as exc:
        raise DeployError(f"{seed.file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"{seed.file} must contain a JSON object {{\"key\": value, ...}}.")
    return data


def overwrite_entry(
    client: OpenCloudClient,
    universe_id: int,
    datastore: str,
    key: str,
    value: Any,
    *,
    scope: str | None = None,
    preserve_metadata: bool = True,
) -> dict:
    """Overwrites an entry while keeping its existing users/attributes.

    The Open Cloud API erases users and attributes if they are not sent back
    along with the new value; so we read them first (a missing entry is created).
    The write is conditioned on the etag that was read: if a game server changes
    the entry in between, Roblox refuses the update instead of losing one of them.
    """
    current = read_entry(client, universe_id, datastore, key, scope=scope) if preserve_metadata else None
    return _write_over(client, universe_id, datastore, key, value, scope, current)


def sync_entry(
    client: OpenCloudClient,
    universe_id: int,
    datastore: str,
    key: str,
    value: Any,
    *,
    scope: str | None = None,
) -> bool:
    """Like overwrite_entry, but leaves alone an entry that already holds this value.

    The entry is read anyway (to keep its users/attributes), so the comparison is free,
    and an unchanged key then costs no write budget and creates no new revision.
    Returns True if the entry was written.
    """
    current = read_entry(client, universe_id, datastore, key, scope=scope)
    if current is not None and "value" in current and same_value(current["value"], value):
        return False
    _write_over(client, universe_id, datastore, key, value, scope, current)
    return True


def read_entry(client: OpenCloudClient, universe_id: int, datastore: str, key: str, *,
               scope: str | None = None) -> dict | None:
    """The entry (value, users, attributes, etag…), or None if it does not exist."""
    try:
        return client.get_entry(universe_id, datastore, key, scope=scope)
    except RobloxApiError as exc:
        if exc.status == 404:
            return None
        raise


def _write_over(client: OpenCloudClient, universe_id: int, datastore: str, key: str, value: Any,
                scope: str | None, current: dict | None) -> dict:
    """Writes `value`, sending back the users/attributes of the entry that was read, conditioned on its etag."""
    current = current or {}
    return client.set_entry(
        universe_id, datastore, key, value, scope=scope,
        users=current.get("users") or None,
        attributes=current.get("attributes") or None,
        etag=current.get("etag") or None,
    )


def _as_luau(value: Any) -> Any:
    """Luau has a single number type: 1 and 1.0 are the same value."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _as_luau(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_luau(v) for v in value]
    return value


def same_value(stored: Any, wanted: Any) -> bool:
    """True if the DataStore already holds `wanted`, as robops would write it (without null fields).

    Compared as JSON text: in Python True == 1, but for a game `true` and `1` are different.
    Key order does not matter, and 1 / 1.0 are equal (one number type in Luau).
    """
    def text(value: Any) -> str:
        return json.dumps(_as_luau(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    return text(stored) == text(without_nulls(wanted))


# --------------------------------------------------------------- deployment

def deploy(
    env: Environment,
    opts: DeployOptions,
    *,
    project_root: Path,
    client_factory: Callable[[], OpenCloudClient],
) -> DeployReport:
    version_type = opts.version_type or env.version_type
    restart = env.restart_servers if opts.restart is None else opts.restart

    places = env.places
    if opts.only_places:
        wanted = set(opts.only_places)
        unknown = wanted - {p.name for p in places}
        if unknown:
            known = ", ".join(p.name for p in places)
            raise DeployError(f"Unknown place(s): {', '.join(sorted(unknown))} (known: {known}).")
        places = [p for p in places if p.name in wanted]

    report = DeployReport(env.name, env.universe_id, version_type, opts.dry_run)
    report.restart_planned = restart and version_type == "Published"
    try:
        _run_steps(env, opts, places, report, version_type, restart, project_root, client_factory)
    except Exception as exc:
        # The caller can still publish what happened (places already live…).
        report.error = exc.describe() if isinstance(exc, RobloxApiError) else str(exc)
        exc.deploy_report = report  # type: ignore[attr-defined]
        raise
    return report


def _run_steps(
    env: Environment,
    opts: DeployOptions,
    places: list[PlaceConfig],
    report: DeployReport,
    version_type: str,
    restart: bool,
    project_root: Path,
    client_factory: Callable[[], OpenCloudClient],
) -> None:
    # 1. Build
    if env.build_command and not opts.skip_build:
        with out.group("Build"):
            run_build(env.build_command, project_root)

    # 2. Preflight
    with out.group("Checks"):
        for place in places:
            if not place.file.is_file():
                raise DeployError(f"[{place.name}] file not found: {place.file}")
            if place.file.suffix.lower() not in PLACE_SUFFIXES:
                raise DeployError(f"[{place.name}] {place.file.name} must be a .rbxl or .rbxlx")
            size = place.file.stat().st_size
            if size == 0:
                raise DeployError(f"[{place.name}] {place.file} is empty.")
            report.places.append(PlaceOutcome(place.name, place.place_id, place.file, size))
            out.success(f"{place.name} → place {place.place_id} ({place.file.name}, {_human(size)})")

        seeds: list[tuple[DatastoreSeed, dict]] = []
        if opts.with_datastores:
            for seed in env.datastores:
                data = load_seed(seed)
                seeds.append((seed, data))
                out.success(f"DataStore {seed.name}: {len(data)} entries to write from {seed.file.name}")
        elif env.datastores:
            out.info("DataStores configured but skipped (add --with-datastores to write them).")

    if opts.dry_run:
        out.notice(
            f"Dry-run: {len(places)} place(s) would be published as '{version_type}' on "
            f"universe {env.universe_id}"
            + (f", {sum(len(d) for _, d in seeds)} DataStore entries overwritten" if seeds else "")
            + (", then the servers restarted." if report.restart_planned else ".")
        )
        return

    client = client_factory()

    # 3. Publishing
    with out.group(f"Publishing ({version_type})"):
        for outcome, place in zip(report.places, places, strict=True):
            result = client.publish_place(env.universe_id, place.place_id, place.file, version_type=version_type)
            outcome.version = result.version_number
            out.success(f"{place.name}: version {result.version_number} ({version_type})")

    # 4. Metadata
    meta_places = [(o, p) for o, p in zip(report.places, places, strict=True) if p.has_metadata]
    if meta_places:
        with out.group("Place metadata"):
            for outcome, place in meta_places:
                try:
                    client.update_place(
                        env.universe_id,
                        place.place_id,
                        display_name=place.display_name,
                        description=place.description,
                        server_size=place.server_size,
                    )
                    outcome.metadata = "updated"
                    out.success(f"{place.name}: metadata updated")
                except RobloxApiError as exc:
                    outcome.metadata = f"failed ({exc.status})"
                    if opts.strict_metadata:
                        raise
                    out.warn(f"{place.name}: metadata not updated — {exc.describe()}")

    # 5. DataStores
    if seeds:
        with out.group("DataStores"):
            snap = client.snapshot_datastores(env.universe_id)
            if snap.get("newSnapshotTaken"):
                out.success("DataStores snapshot created (versioned backup kept for 30 days).")
            else:
                out.info(f"Snapshot already taken today (latest: {snap.get('latestSnapshotTime')}).")
            for seed, data in seeds:
                before = report.datastore_writes          # counted entry by entry: right even after a failure
                for key, value in data.items():
                    if sync_entry(client, env.universe_id, seed.name, key, value, scope=seed.scope):
                        report.datastore_writes += 1
                    else:
                        report.datastore_unchanged += 1
                written = report.datastore_writes - before
                out.success(f"{seed.name}: {written} entries overwritten"
                            + (f", {len(data) - written} already up to date" if written < len(data) else ""))

    # 6. Restart
    if restart:
        with out.group("Server restart"):
            if version_type != "Published":
                out.warn("Version 'Saved': nothing was published, restart skipped.")
            else:
                client.restart_servers(env.universe_id)
                report.restarted = True
                out.success("Server restart requested.")
