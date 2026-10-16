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
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import output as out
from .client import OpenCloudClient, RobloxApiError
from .config import DatastoreSeed, Environment


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
    restarted: bool = False
    restart_planned: bool = False

    def versions(self) -> dict[str, int | None]:
        return {p.name: p.version for p in self.places}

    def to_markdown(self) -> str:
        mode = " (dry-run)" if self.dry_run else ""
        lines = [
            f"### 🚀 Roblox deployment — `{self.environment}`{mode}",
            "",
            f"Universe `{self.universe_id}` · version type **{self.version_type}**",
            "",
            "| Place | Place ID | File | Size | Version | Metadata |",
            "|---|---|---|---|---|---|",
        ]
        for p in self.places:
            version = f"v{p.version}" if p.version is not None else "—"
            lines.append(
                f"| {p.name} | `{p.place_id}` | `{p.file.name}` | {_human(p.size)} | {version} | {p.metadata} |"
            )
        lines.append("")
        if self.datastore_writes:
            lines.append(f"DataStores: {self.datastore_writes} entries overwritten.")
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

def run_build(command: str, cwd: Path) -> None:
    out.info(f"$ {command}")
    try:
        completed = subprocess.run(shlex.split(command), cwd=cwd)
    except FileNotFoundError as exc:
        raise DeployError(
            f"Build command not found: {exc.filename}. "
            "Install it (e.g. Rojo via Rokit) or use --skip-build."
        ) from exc
    if completed.returncode != 0:
        raise DeployError(f"The build failed (exit code {completed.returncode}).")


def load_seed(seed: DatastoreSeed) -> dict[str, Any]:
    if not seed.file.is_file():
        raise DeployError(f"Data file not found for DataStore '{seed.name}': {seed.file}")
    try:
        data = json.loads(seed.file.read_text(encoding="utf-8"))
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
    """
    users = attributes = None
    if preserve_metadata:
        try:
            current = client.get_entry(universe_id, datastore, key, scope=scope)
            users = current.get("users") or None
            attributes = current.get("attributes") or None
        except RobloxApiError as exc:
            if exc.status != 404:
                raise
    return client.set_entry(
        universe_id, datastore, key, value, scope=scope, users=users, attributes=attributes
    )


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

    # 1. Build
    if env.build_command and not opts.skip_build:
        with out.group("Build"):
            run_build(env.build_command, project_root)

    # 2. Preflight
    with out.group("Checks"):
        for place in places:
            if not place.file.is_file():
                raise DeployError(f"[{place.name}] file not found: {place.file}")
            if place.file.suffix.lower() not in (".rbxl", ".rbxlx"):
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
        return report

    client = client_factory()

    # 3. Publishing
    with out.group(f"Publishing ({version_type})"):
        for outcome, place in zip(report.places, places):
            result = client.publish_place(env.universe_id, place.place_id, place.file, version_type=version_type)
            outcome.version = result.version_number
            out.success(f"{place.name}: version {result.version_number} ({version_type})")

    # 4. Metadata
    meta_places = [(o, p) for o, p in zip(report.places, places) if p.has_metadata]
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
                for key, value in data.items():
                    overwrite_entry(client, env.universe_id, seed.name, key, value, scope=seed.scope)
                    report.datastore_writes += 1
                out.success(f"{seed.name}: {len(data)} entries overwritten")

    # 6. Restart
    if restart:
        with out.group("Server restart"):
            if version_type != "Published":
                out.warn("Version 'Saved': nothing was published, restart skipped.")
            else:
                client.restart_servers(env.universe_id)
                report.restarted = True
                out.success("Server restart requested.")

    return report
