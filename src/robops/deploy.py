"""Orchestration d'un déploiement complet pour un environnement.

Étapes (dans cet ordre) :
  1. build          — commande de build (ex. rojo build) si configurée
  2. préflight      — fichiers présents, extensions valides, JSON des données lisible
  3. publication    — chaque place est écrasée par son fichier (nouvelle version)
  4. métadonnées    — nom / description / taille de serveur, si renseignés
  5. DataStores     — snapshot puis écrasement des entrées (opt-in : --with-datastores)
  6. redémarrage    — redémarrage des serveurs pour basculer sur la nouvelle version

Aucun appel réseau n'est fait avant que le préflight soit passé, et rien n'est
appelé du tout en --dry-run.
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
    version_type: str | None = None        # surcharge de environments.<env>.version_type
    only_places: list[str] | None = None   # filtre par nom de place
    skip_build: bool = False
    with_datastores: bool = False
    restart: bool | None = None            # surcharge de restart_servers
    dry_run: bool = False
    strict_metadata: bool = False          # échec si la mise à jour des métadonnées échoue


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
            f"### 🚀 Déploiement Roblox — `{self.environment}`{mode}",
            "",
            f"Univers `{self.universe_id}` · type de version **{self.version_type}**",
            "",
            "| Place | Place ID | Fichier | Taille | Version | Métadonnées |",
            "|---|---|---|---|---|---|",
        ]
        for p in self.places:
            version = f"v{p.version}" if p.version is not None else "—"
            lines.append(
                f"| {p.name} | `{p.place_id}` | `{p.file.name}` | {_human(p.size)} | {version} | {p.metadata} |"
            )
        lines.append("")
        if self.datastore_writes:
            lines.append(f"DataStores : {self.datastore_writes} entrée(s) écrasée(s).")
        if self.dry_run:
            lines.append(f"Redémarrage des serveurs : {'prévu' if self.restart_planned else 'non prévu'}.")
        else:
            lines.append(f"Redémarrage des serveurs : {'oui' if self.restarted else 'non'}.")
        return "\n".join(lines)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} o"


# ------------------------------------------------------------------ helpers

def run_build(command: str, cwd: Path) -> None:
    out.info(f"$ {command}")
    try:
        completed = subprocess.run(shlex.split(command), cwd=cwd)
    except FileNotFoundError as exc:
        raise DeployError(
            f"Commande de build introuvable : {exc.filename}. "
            "Installe-la (ex. Rojo via Rokit) ou utilise --skip-build."
        ) from exc
    if completed.returncode != 0:
        raise DeployError(f"Le build a échoué (code {completed.returncode}).")


def load_seed(seed: DatastoreSeed) -> dict[str, Any]:
    if not seed.file.is_file():
        raise DeployError(f"Fichier de données introuvable pour le DataStore '{seed.name}' : {seed.file}")
    try:
        data = json.loads(seed.file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeployError(f"{seed.file} n'est pas un JSON valide : {exc}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"{seed.file} doit contenir un objet JSON {{\"clé\": valeur, ...}}.")
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
    """Écrase une entrée en conservant ses users/attributes existants.

    L'API Open Cloud efface users et attributes s'ils ne sont pas renvoyés avec
    la nouvelle valeur ; on les relit donc d'abord (une entrée absente est créée).
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
            raise DeployError(f"Place(s) inconnue(s) : {', '.join(sorted(unknown))} (connues : {known}).")
        places = [p for p in places if p.name in wanted]

    report = DeployReport(env.name, env.universe_id, version_type, opts.dry_run)
    report.restart_planned = restart and version_type == "Published"

    # 1. Build
    if env.build_command and not opts.skip_build:
        with out.group("Build"):
            run_build(env.build_command, project_root)

    # 2. Préflight
    with out.group("Vérifications"):
        for place in places:
            if not place.file.is_file():
                raise DeployError(f"[{place.name}] fichier introuvable : {place.file}")
            if place.file.suffix.lower() not in (".rbxl", ".rbxlx"):
                raise DeployError(f"[{place.name}] {place.file.name} doit être un .rbxl ou .rbxlx")
            size = place.file.stat().st_size
            if size == 0:
                raise DeployError(f"[{place.name}] {place.file} est vide.")
            report.places.append(PlaceOutcome(place.name, place.place_id, place.file, size))
            out.success(f"{place.name} → place {place.place_id} ({place.file.name}, {_human(size)})")

        seeds: list[tuple[DatastoreSeed, dict]] = []
        if opts.with_datastores:
            for seed in env.datastores:
                data = load_seed(seed)
                seeds.append((seed, data))
                out.success(f"DataStore {seed.name}: {len(data)} entrée(s) à écrire depuis {seed.file.name}")
        elif env.datastores:
            out.info("DataStores configurés mais ignorés (ajoute --with-datastores pour les écrire).")

    if opts.dry_run:
        out.notice(
            f"Dry-run : {len(places)} place(s) seraient publiées en '{version_type}' sur "
            f"l'univers {env.universe_id}"
            + (f", {sum(len(d) for _, d in seeds)} entrée(s) de DataStore écrasées" if seeds else "")
            + (", puis redémarrage des serveurs." if report.restart_planned else ".")
        )
        return report

    client = client_factory()

    # 3. Publication
    with out.group(f"Publication ({version_type})"):
        for outcome, place in zip(report.places, places):
            result = client.publish_place(env.universe_id, place.place_id, place.file, version_type=version_type)
            outcome.version = result.version_number
            out.success(f"{place.name} : version {result.version_number} ({version_type})")

    # 4. Métadonnées
    meta_places = [(o, p) for o, p in zip(report.places, places) if p.has_metadata]
    if meta_places:
        with out.group("Métadonnées des places"):
            for outcome, place in meta_places:
                try:
                    client.update_place(
                        env.universe_id,
                        place.place_id,
                        display_name=place.display_name,
                        description=place.description,
                        server_size=place.server_size,
                    )
                    outcome.metadata = "à jour"
                    out.success(f"{place.name} : métadonnées mises à jour")
                except RobloxApiError as exc:
                    outcome.metadata = f"échec ({exc.status})"
                    if opts.strict_metadata:
                        raise
                    out.warn(f"{place.name} : métadonnées non mises à jour — {exc.describe()}")

    # 5. DataStores
    if seeds:
        with out.group("DataStores"):
            snap = client.snapshot_datastores(env.universe_id)
            if snap.get("newSnapshotTaken"):
                out.success("Snapshot des DataStores créé (sauvegarde versionnée pendant 30 jours).")
            else:
                out.info(f"Snapshot déjà pris aujourd'hui (dernier : {snap.get('latestSnapshotTime')}).")
            for seed, data in seeds:
                for key, value in data.items():
                    overwrite_entry(client, env.universe_id, seed.name, key, value, scope=seed.scope)
                    report.datastore_writes += 1
                out.success(f"{seed.name} : {len(data)} entrée(s) écrasée(s)")

    # 6. Redémarrage
    if restart:
        with out.group("Redémarrage des serveurs"):
            if version_type != "Published":
                out.warn("Version 'Saved' : rien n'a été publié, redémarrage ignoré.")
            else:
                client.restart_servers(env.universe_id)
                report.restarted = True
                out.success("Redémarrage des serveurs demandé.")

    return report
