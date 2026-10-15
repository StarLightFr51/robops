"""Point d'entrée de la ligne de commande `robops`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from . import output as out
from .client import OpenCloudClient, RobloxApiError
from .config import DEFAULT_CONFIG, ConfigError, load_config
from .deploy import DeployError, DeployOptions, deploy, overwrite_entry
from . import players as pl
from . import updater

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


# ----------------------------------------------------------------- helpers

def _api_key(env_var: str) -> str:
    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    if out.in_github_actions():
        repo = os.environ.get("GITHUB_REPOSITORY", "ce dépôt")
        raise UsageError(
            f"Le secret {env_var} est vide pour ce run. Il doit se trouver dans le dépôt {repo} "
            "(celui qui lance le workflow, pas le dépôt robops) : Settings → Secrets and variables → "
            f"Actions → onglet « Secrets » → « Repository secrets », nommé exactement {env_var}. "
            "Un secret placé dans un autre dépôt, dans l'onglet « Variables », dans Dependabot/Codespaces "
            "ou dans un Environment GitHub n'est pas visible ici."
        )
    raise UsageError(
        f"{env_var} n'est pas définie sur cette machine. Les secrets GitHub n'existent que pendant les "
        "exécutions sur GitHub : pour publier, utilise le bouton « Run workflow » de l'onglet Actions. "
        f"Pour lancer robops en local, dans PowerShell :  $env:{env_var} = \"ta-clé\""
    )


def _client(env_var: str) -> OpenCloudClient:
    return OpenCloudClient(_api_key(env_var))


def _resolve_target(args, *, need_place: bool) -> tuple[int, int | None, str]:
    """Retourne (universe_id, place_id, nom_variable_clé) depuis --env ou les IDs explicites."""
    key_env = args.api_key_env
    if getattr(args, "env", None):
        env = load_config(args.config).env(args.env)
        key_env = key_env or env.api_key_env
        place_id = None
        if need_place:
            if getattr(args, "place_id", None):
                place_id = args.place_id
            elif getattr(args, "place", None):
                match = [p for p in env.places if p.name == args.place]
                if not match:
                    raise UsageError(f"Place '{args.place}' absente de l'environnement {env.name}.")
                place_id = match[0].place_id
            elif len(env.places) == 1:
                place_id = env.places[0].place_id
            else:
                names = ", ".join(p.name for p in env.places)
                raise UsageError(f"Plusieurs places dans {env.name} : précise --place ({names}).")
        return args.universe_id or env.universe_id, place_id, key_env
    if not args.universe_id:
        raise UsageError("Précise --env <environnement> ou --universe-id.")
    if need_place and not getattr(args, "place_id", None):
        raise UsageError("Précise --place-id (ou --env avec --place).")
    return args.universe_id, getattr(args, "place_id", None), key_env or "ROBLOX_API_KEY"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _parse_value(args) -> Any:
    if args.value is not None and args.file is not None:
        raise UsageError("Utilise --value OU --file, pas les deux.")
    if args.file is not None:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.value is not None:
        text = args.value
    else:
        raise UsageError("Précise --value '<json>' ou --file <chemin.json>.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if args.file is None:
            # Valeur passée en ligne de commande non JSON : on la traite comme une chaîne.
            return text
        raise UsageError(f"{args.file} n'est pas un JSON valide.")


# ---------------------------------------------------------------- commands

def cmd_deploy(args) -> int:
    cfg = load_config(args.config)
    env = cfg.env(args.environment)
    if args.api_key_env:
        env.api_key_env = args.api_key_env
    opts = DeployOptions(
        version_type=args.version_type,
        only_places=args.place or None,
        skip_build=args.skip_build,
        with_datastores=args.with_datastores,
        restart=args.restart,
        dry_run=args.dry_run,
        strict_metadata=args.strict_metadata,
    )
    if not opts.dry_run:
        _api_key(env.api_key_env)  # échoue tôt, avant le build, si la clé manque

    report = deploy(env, opts, project_root=cfg.root, client_factory=lambda: _client(env.api_key_env))

    out.set_output("environment", env.name)
    out.set_output("universe_id", str(env.universe_id))
    out.set_output("versions", json.dumps(report.versions()))
    for place in report.places:
        if place.version is not None:
            out.set_output(f"version_{place.name}", str(place.version))
    out.write_summary(report.to_markdown())

    if args.json:
        _print_json({"environment": env.name, "universe_id": env.universe_id, "versions": report.versions(),
                     "restarted": report.restarted, "dry_run": report.dry_run})
    elif not report.dry_run:
        published = ", ".join(f"{p.name} v{p.version}" for p in report.places)
        out.notice(f"Déploiement '{env.name}' terminé : {published}")
    return EXIT_OK


def cmd_publish(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise UsageError(f"Fichier introuvable : {path}")
    universe_id, place_id, key_env = _resolve_target(args, need_place=True)
    result = _client(key_env).publish_place(universe_id, place_id, path, version_type=args.version_type)
    out.success(f"Place {place_id} écrasée : version {result.version_number} ({result.version_type})")
    out.set_output("version", str(result.version_number))
    return EXIT_OK


def cmd_place_get(args) -> int:
    universe_id, place_id, key_env = _resolve_target(args, need_place=True)
    _print_json(_client(key_env).get_place(universe_id, place_id))
    return EXIT_OK


def cmd_place_update(args) -> int:
    if args.name is None and args.description is None and args.server_size is None:
        raise UsageError("Rien à mettre à jour : précise --name, --description et/ou --server-size.")
    universe_id, place_id, key_env = _resolve_target(args, need_place=True)
    _print_json(
        _client(key_env).update_place(
            universe_id, place_id, display_name=args.name, description=args.description,
            server_size=args.server_size,
        )
    )
    return EXIT_OK


def cmd_restart(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    _client(key_env).restart_servers(universe_id)
    out.success(f"Redémarrage des serveurs demandé pour l'univers {universe_id}.")
    return EXIT_OK


def cmd_ds_get(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    entry = _client(key_env).get_entry(universe_id, args.datastore, args.key, scope=args.scope)
    _print_json(entry if args.raw else entry.get("value"))
    return EXIT_OK


def cmd_ds_set(args) -> int:
    value = _parse_value(args)
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    entry = overwrite_entry(
        _client(key_env), universe_id, args.datastore, args.key, value,
        scope=args.scope, preserve_metadata=not args.clear_metadata,
    )
    out.success(f"{args.datastore}/{args.key} écrasée (révision {entry.get('revisionId', '?')}).")
    return EXIT_OK


def cmd_ds_delete(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    _client(key_env).delete_entry(universe_id, args.datastore, args.key, scope=args.scope)
    out.success(f"{args.datastore}/{args.key} marquée comme supprimée (purge définitive sous 30 jours).")
    return EXIT_OK


def cmd_ds_list(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    for entry_id in _client(key_env).list_entry_ids(
        universe_id, args.datastore, scope=args.scope, prefix=args.prefix, limit=args.limit
    ):
        print(entry_id)
    return EXIT_OK


def cmd_ds_export(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    client = _client(key_env)
    data: dict[str, Any] = {}
    for entry_id in client.list_entry_ids(universe_id, args.datastore, scope=args.scope, prefix=args.prefix):
        scope, key = pl.split_scoped(entry_id, args.scope)
        data[key] = client.get_entry(universe_id, args.datastore, key, scope=scope).get("value")
    Path(args.out).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out.success(f"{len(data)} entrée(s) exportée(s) vers {args.out}")
    return EXIT_OK


def cmd_ds_import(args) -> int:
    path = Path(args.file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"Fichier introuvable : {path}") from None
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} n'est pas un JSON valide : {exc}") from None
    if not isinstance(data, dict):
        raise UsageError(f"{path} doit contenir un objet JSON {{\"clé\": valeur}}.")
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    if args.dry_run:
        out.notice(f"Dry-run : {len(data)} entrée(s) seraient écrasées dans {args.datastore}.")
        for key in data:
            print(f"  {key}")
        return EXIT_OK
    client = _client(key_env)
    if not args.no_snapshot:
        snap = client.snapshot_datastores(universe_id)
        out.info(f"Snapshot : {'créé' if snap.get('newSnapshotTaken') else 'déjà pris aujourd’hui'}.")
    for key, value in data.items():
        overwrite_entry(client, universe_id, args.datastore, key, value,
                        scope=args.scope, preserve_metadata=not args.clear_metadata)
    out.success(f"{len(data)} entrée(s) écrasée(s) dans {args.datastore}.")
    return EXIT_OK


def cmd_ds_stores(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    names = list(_client(key_env).list_datastores(universe_id, prefix=args.prefix))
    for name in names:
        print(name)
    _status(f"{len(names)} DataStore(s) dans l'univers {universe_id}.")
    return EXIT_OK


def cmd_ds_snapshot(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    _print_json(_client(key_env).snapshot_datastores(universe_id))
    return EXIT_OK


# ------------------------------------------------------- joueurs (ProfileStore)

def _players_target(args) -> tuple[int, pl.PlayersConfig, str]:
    """Univers + emplacement des profils (config [players], surchargée par les options)."""
    key_env = args.api_key_env
    if args.env:
        env = load_config(args.config).env(args.env)
        cfg = env.players
        universe_id = args.universe_id or env.universe_id
        key_env = key_env or env.api_key_env
    else:
        if not args.universe_id:
            raise UsageError("Précise --env <environnement> ou --universe-id.")
        cfg, universe_id = pl.PlayersConfig(), args.universe_id
    if args.datastore:
        cfg.datastore = args.datastore
    if args.prefix is not None:
        cfg.key_prefix = args.prefix
    if args.scope:
        cfg.scope = args.scope
    return universe_id, cfg, key_env or "ROBLOX_API_KEY"


def _status(msg: str) -> None:
    # Sur stderr pour que stdout reste du JSON exploitable (jq, redirections…).
    print(msg, file=sys.stderr, flush=True)


def _ensure_writable(user_id: str, current: pl.CurrentProfile, force: bool) -> None:
    if not current.exists or not pl.is_profile(current.value):
        return
    state = pl.session_state(current.value)
    if state.status == "stale":
        out.warn(f"Joueur {user_id} : {state.detail}. Le verrou sera retiré.")
    if not state.writable:
        if not force:
            raise pl.PlayerError(
                f"Joueur {user_id} : {state.detail}. Écriture refusée : son serveur écraserait la "
                "modification à la prochaine sauvegarde. Attends qu'il se déconnecte, ou modifie-le "
                "depuis le jeu (commande admin)."
            )
        out.warn(f"Joueur {user_id} : {state.detail}. --force : écriture malgré le verrou.")


def _print_changes(user_id: str, changes: list[str]) -> None:
    out.info(f"Joueur {user_id} : {len(changes)} champ(s) modifié(s)")
    for line in changes[:50]:
        out.info(f"    {line}")
    if len(changes) > 50:
        out.info(f"    … et {len(changes) - 50} autre(s)")


def cmd_player_get(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    current = pl.fetch_profile(_client(key_env), universe_id, cfg, args.user_id)
    if not current.exists:
        raise pl.PlayerError(f"Aucun profil pour {cfg.key(args.user_id)} dans {cfg.datastore}.")
    if not pl.is_profile(current.value):
        _status("⚠ Cette entrée n'a pas la structure ProfileStore (Data / MetaData).")
        _print_json(current.value)
        return EXIT_OK
    _status(f"Joueur {args.user_id} : {pl.session_state(current.value).detail}")
    _print_json(current.value if args.raw else current.value.get("Data"))
    return EXIT_OK


def cmd_player_backup(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    client = _client(key_env)
    out.info(f"Backup de {cfg.datastore}/{cfg.key_prefix}* (univers {universe_id}) → {args.out}")
    last = [0]

    def progress(count: int) -> None:
        if count - last[0] >= 500:
            last[0] = count
            out.info(f"  … {count} profil(s)")

    result = pl.backup_players(client, universe_id, cfg, Path(args.out), limit=args.limit,
                               workers=args.workers, progress=progress)
    out.set_output("backup_file", str(result.path))
    out.set_output("backup_count", str(result.count))
    out.success(f"{result.count} profil(s) sauvegardé(s) dans {result.path}")
    if result.count == 0 and not result.errors and result.other_keys:
        guess = pl.guess_prefix(result.other_examples or [])
        out.warn(
            f"Le DataStore « {cfg.datastore} » contient {result.other_keys} clé(s), mais aucune ne commence par "
            f"« {cfg.key_prefix} ». Exemples : {', '.join(result.other_examples or [])}"
        )
        if guess is not None:
            out.info(f"  → Dans robops.toml, section [players], mets :  key_prefix = \"{guess}\"   puis relance le backup.")
    elif result.count == 0 and not result.errors:
        out.warn(
            f"Aucun profil trouvé : le DataStore « {cfg.datastore} » est vide ou n'existe pas (univers {universe_id}). "
            "Causes possibles :\n"
            "  1. Mauvais univers : vérifie universe_id dans robops.toml (celui du jeu où jouent les joueurs).\n"
            "  2. Autre nom de DataStore : regarde profileStoreIndex dans ton DataService:init, puis la section "
            "[players] de robops.toml (Creator Dashboard → ton jeu → Configurations → Data stores manager).\n"
            "  3. Aucune donnée encore sauvegardée : tests uniquement dans Studio sans « Enable Studio Access "
            "to API Services », ou useMock activé dans DataService."
        )
    if result.errors:
        for user_id, error in list(result.errors.items())[:20]:
            out.warn(f"{user_id} : {error}")
        out.error(f"{len(result.errors)} profil(s) n'ont pas pu être lus (listés dans le fichier).")
        return EXIT_FAILURE
    return EXIT_OK


def _restore_one(client, universe_id: int, cfg: pl.PlayersConfig, user_id: str, record: dict, args) -> str:
    backup_profile = record.get("value")
    if not pl.is_profile(backup_profile):
        raise pl.PlayerError(f"Joueur {user_id} : l'entrée du backup n'est pas un profil ProfileStore.")
    current = pl.fetch_profile(client, universe_id, cfg, user_id)
    _ensure_writable(user_id, current, args.force)

    current_ok = current.exists and pl.is_profile(current.value)
    if current.exists and not current_ok:
        out.warn(f"Joueur {user_id} : profil actuel illisible, il sera remplacé par le profil complet du backup.")
    before = current.value.get("Data") if current_ok else None
    changes = pl.changed_fields(before, backup_profile["Data"])
    stale = current_ok and pl.session_state(current.value).status == "stale"
    if current_ok and not changes and not stale:
        out.info(f"Joueur {user_id} : déjà identique au backup, rien à faire.")
        return "identique"

    new_profile = pl.with_data(current.value if current_ok else None, backup_profile["Data"],
                               fallback_profile=backup_profile)
    if not current.exists:
        out.info(f"Joueur {user_id} : aucun profil actuel, le profil du backup sera recréé.")
    elif not changes:
        out.info(f"Joueur {user_id} : données identiques au backup, seul le verrou périmé sera retiré.")
    else:
        _print_changes(user_id, changes)
    if args.dry_run:
        return "simulé"
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, user_id, current)
    pl.write_profile(client, universe_id, cfg, user_id, new_profile, current, users=record.get("users") or None)
    out.success(f"Joueur {user_id} restauré" + (f" (ancien profil copié dans {safety})" if safety else ""))
    return "restauré"


def cmd_player_restore(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    backup = pl.load_backup(Path(args.backup), cfg)
    if args.all:
        if args.user_ids:
            raise UsageError("Utilise --all OU une liste de joueurs, pas les deux.")
        if not args.yes and not args.dry_run:
            raise UsageError(
                f"--all va remettre {len(backup)} joueur(s) à l'état du backup, en écrasant leur progression "
                "depuis. Relance avec --dry-run pour voir l'impact, puis --yes pour confirmer."
            )
        user_ids = list(backup)
    else:
        if not args.user_ids:
            raise UsageError("Précise un ou plusieurs UserId, ou --all.")
        user_ids = [str(u) for u in args.user_ids]
        missing = [u for u in user_ids if u not in backup]
        if missing:
            raise UsageError(f"Absent(s) du backup : {', '.join(missing)}")

    client = _client(key_env)
    tally: dict[str, int] = {}
    failures: dict[str, str] = {}
    for user_id in user_ids:
        try:
            status = _restore_one(client, universe_id, cfg, user_id, backup[user_id], args)
            tally[status] = tally.get(status, 0) + 1
        except (pl.PlayerError, RobloxApiError) as exc:
            message = exc.describe() if isinstance(exc, RobloxApiError) else str(exc)
            if not args.all:
                raise
            failures[user_id] = message.splitlines()[0]
            out.warn(message)

    summary = ", ".join(f"{n} {k}(s)" for k, n in tally.items()) or "aucun joueur"
    label = "Restauration (dry-run)" if args.dry_run else "Restauration"
    if failures:
        message = (f"{label} : {summary} ; {len(failures)} joueur(s) non restaurable(s) "
                   f"(en ligne ou en erreur) : " + ", ".join(list(failures)[:20]))
        if args.dry_run:
            out.warn(message)
            return EXIT_OK
        out.error(message + ". Relance la commande pour eux plus tard.")
        return EXIT_FAILURE
    out.notice(f"{label} : {summary}.")
    return EXIT_OK


def cmd_player_edit(args) -> int:
    if not (args.set or args.unset or args.data_file):
        raise UsageError("Rien à modifier : utilise --set chemin=valeur, --unset chemin ou --data-file.")
    universe_id, cfg, key_env = _players_target(args)
    client = _client(key_env)
    current = pl.fetch_profile(client, universe_id, cfg, args.user_id)
    if not current.exists:
        raise pl.PlayerError(f"Aucun profil pour {cfg.key(args.user_id)} (le joueur ne s'est jamais connecté ?).")
    if not pl.is_profile(current.value):
        raise pl.PlayerError(f"{cfg.key(args.user_id)} n'a pas la structure ProfileStore (Data / MetaData).")
    _ensure_writable(args.user_id, current, args.force)

    data = current.value.get("Data")
    if args.data_file:
        try:
            data = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"{args.data_file} illisible : {exc}") from None
    new_data = pl.apply_edits(data, args.set or [], args.unset or [])
    changes = pl.changed_fields(current.value.get("Data"), new_data)
    if not changes:
        out.info("Aucune différence avec le profil actuel, rien à écrire.")
        return EXIT_OK
    _print_changes(args.user_id, changes)
    if args.dry_run:
        out.notice("Dry-run : rien n'a été écrit.")
        return EXIT_OK
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, args.user_id, current)
    pl.write_profile(client, universe_id, cfg, args.user_id, pl.with_data(current.value, new_data), current)
    out.success(f"Joueur {args.user_id} modifié (ancien profil copié dans {safety}).")
    return EXIT_OK


def cmd_player_delete(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    if not args.yes:
        raise UsageError(f"Supprimer le profil de {args.user_id} ? Relance avec --yes pour confirmer.")
    client = _client(key_env)
    current = pl.fetch_profile(client, universe_id, cfg, args.user_id)
    if not current.exists:
        out.info(f"Aucun profil pour {cfg.key(args.user_id)}, rien à supprimer.")
        return EXIT_OK
    _ensure_writable(args.user_id, current, args.force)
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, args.user_id, current)
    client.delete_entry(universe_id, cfg.datastore, cfg.key(args.user_id), scope=cfg.scope)
    out.success(f"Profil de {args.user_id} supprimé (purge définitive par Roblox sous 30 jours ; "
                f"copie locale : {safety}).")
    return EXIT_OK


def cmd_self_update(args) -> int:
    if not updater.is_frozen():
        out.info("robops est installé avec pip : mets-le à jour avec  pip install -U <dossier ou dépôt robops>")
        return EXIT_OK
    repo = updater.repository()
    session = updater.make_session(updater.github_token())
    try:
        release = updater.latest_release(repo, session=session)
        updater.record_check()
        if not updater.is_newer(release.version, __version__) and not args.force:
            out.success(f"robops {__version__} est à jour (dernière version : {release.version}).")
            return EXIT_OK
        if args.check:
            out.notice(f"Nouvelle version disponible : {release.version} (installée : {__version__}). "
                       "Lance  robops self-update  pour l'installer.")
            return EXIT_OK
        out.info(f"Téléchargement de robops {release.version} depuis github.com/{repo} …")
        updater.update_executable(release, updater.current_executable(), session=session)
    except updater.UpdateError as exc:
        out.error(str(exc))
        return EXIT_FAILURE
    out.success(f"robops mis à jour : {__version__} → {release.version}. Relance la commande pour l'utiliser.")
    return EXIT_OK


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    names = [args.environment] if args.environment else cfg.names
    problems = 0
    for name in names:
        with out.group(f"Environnement {name}"):
            try:
                env = cfg.env(name)
            except ConfigError as exc:
                out.error(str(exc))
                problems += 1
                continue
            out.success(f"univers {env.universe_id}, {len(env.places)} place(s), version {env.version_type}")
            if env.build_command:
                out.info(f"build : {env.build_command}")
            out.info(f"profils joueurs : DataStore {env.players.datastore}, clés {env.players.key_prefix}<UserId>"
                     + (f", scope {env.players.scope}" if env.players.scope else ""))
            for place in env.places:
                state = "présent" if place.file.is_file() else "absent (sera produit par le build ?)"
                out.info(f"  • {place.name} → {place.place_id} : {place.file.name} {state}")
            for seed in env.datastores:
                if seed.file.is_file():
                    out.info(f"  • DataStore {seed.name} ← {seed.file.name} présent")
                else:
                    problems += 1
                    out.error(f"  • DataStore {seed.name} : fichier introuvable ({seed.file}). Crée-le, "
                              f"ou commente la section [[environments.{name}.datastores]] si tu ne t'en sers pas.")
            if not os.environ.get(env.api_key_env):
                out.warn(
                    f"{env.api_key_env} n'est pas définie sur cette machine. C'est normal si tu publies via "
                    "GitHub Actions : un secret GitHub n'existe que pendant les exécutions sur GitHub. "
                    f"Pour tester en local, dans PowerShell :  $env:{env.api_key_env} = \"ta-clé\""
                )
                if args.online:
                    problems += 1
                    continue
            if args.online:
                client = _client(env.api_key_env)
                for place in env.places:
                    try:
                        info = client.get_place(env.universe_id, place.place_id)
                        out.success(f"  {place.name} accessible : « {info.get('displayName', '?')} »")
                    except RobloxApiError as exc:
                        out.error(f"  {place.name} : {exc.describe()}")
                        problems += 1
    if problems:
        out.error(f"{problems} problème(s) détecté(s).")
        return EXIT_FAILURE
    out.success("Configuration valide.")
    return EXIT_OK


def _point_to_action_repo(workflow: str, action_repo: str) -> str:
    """Remplace « uses: ./tools/robops[/sous-action] » par le dépôt GitHub de robops.

    ex. ./tools/robops/backup → alex/robops/backup@main
    """
    import re

    repo, _, ref = action_repo.partition("@")
    ref = ref or "main"
    pattern = re.compile(r"uses: \./tools/robops(/[\w.-]+)?[^\S\n]*(#[^\n]*)?")
    return pattern.sub(lambda m: f"uses: {repo}{m.group(1) or ''}@{ref}", workflow)


def cmd_init(args) -> int:
    targets = {
        "robops.toml": Path(args.config),
        "github-workflow.yml": Path(".github/workflows/roblox-deploy.yml"),
        "backup-workflow.yml": Path(".github/workflows/roblox-backup.yml"),
        "rokit.toml": Path("rokit.toml"),
    }
    templates = resources.files("robops") / "templates"
    # D'où le workflow charge l'action robops : le dépôt GitHub de robops s'il est connu,
    # sinon une copie locale dans tools/robops.
    action_repo = (args.action_repo or updater.repository() or "").strip()
    for template, dest in targets.items():
        if dest.exists() and not args.force:
            out.warn(f"{dest} existe déjà (utilise --force pour l'écraser).")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = (templates / template).read_text(encoding="utf-8")
        if template.endswith(".yml") and action_repo:
            content = _point_to_action_repo(content, action_repo)
        dest.write_text(content, encoding="utf-8")
        out.success(f"{dest} créé")

    # Les copies de sécurité et les backups contiennent des données personnelles de joueurs :
    # on s'assure qu'ils ne finissent jamais dans Git.
    gitignore = Path(".gitignore")
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    wanted = [entry for entry in (".robops/", "backups/") if entry not in lines]
    if wanted:
        prefix = "\n" if lines and lines[-1].strip() else ""
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "# robops : données joueurs (ne jamais committer)\n" + "\n".join(wanted) + "\n")
        out.success(f".gitignore : ajout de {', '.join(wanted)}")

    out.info("Renseigne tes IDs dans robops.toml, puis ajoute le secret ROBLOX_API_KEY sur GitHub.")
    return EXIT_OK


# ------------------------------------------------------------------ parser

def _add_target(p: argparse.ArgumentParser, *, place: bool) -> None:
    g = p.add_argument_group("cible")
    g.add_argument("--env", help="environnement de robops.toml (fournit univers/place/clé)")
    g.add_argument("--universe-id", type=int, help="ID de l'univers (expérience)")
    if place:
        g.add_argument("--place", help="nom de la place dans l'environnement")
        g.add_argument("--place-id", type=int, help="ID de la place")


def _add_ds_common(p: argparse.ArgumentParser) -> None:
    _add_target(p, place=False)
    p.add_argument("--scope", help="scope du DataStore (défaut : global)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robops",
        description="DevOps Roblox : écrase les places et les données d'une expérience via Open Cloud.",
    )
    parser.add_argument("--version", action="version", version=f"robops {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="fichier de config (défaut : robops.toml)")
    parser.add_argument("--api-key-env", help="variable contenant la clé API (défaut : ROBLOX_API_KEY)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<commande>")

    # Mêmes options acceptées après la sous-commande (robops deploy staging -c autre.toml).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--api-key-env", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # deploy
    p = sub.add_parser("deploy", parents=[common], help="build + publication complète d'un environnement")
    p.add_argument("environment", help="nom de l'environnement (ex. staging, production)")
    p.add_argument("--version-type", choices=["Published", "Saved"], help="surcharge le type de version")
    p.add_argument("--place", action="append", metavar="NOM", help="ne déployer que cette place (répétable)")
    p.add_argument("--skip-build", action="store_true", help="ne pas lancer la commande de build")
    p.add_argument("--with-datastores", action="store_true", help="écrase aussi les DataStores configurés")
    restart = p.add_mutually_exclusive_group()
    restart.add_argument("--restart", dest="restart", action="store_true", default=None,
                         help="redémarre les serveurs après publication")
    restart.add_argument("--no-restart", dest="restart", action="store_false", help="ne redémarre pas les serveurs")
    p.add_argument("--dry-run", action="store_true", help="build + vérifications, sans aucun appel à Roblox")
    p.add_argument("--strict-metadata", action="store_true", help="échoue si la mise à jour des métadonnées échoue")
    p.add_argument("--json", action="store_true", help="affiche le résultat en JSON")
    p.set_defaults(func=cmd_deploy)

    # publish
    p = sub.add_parser("publish", parents=[common], help="écrase une place avec un fichier .rbxl/.rbxlx")
    p.add_argument("file", help="fichier .rbxl ou .rbxlx")
    _add_target(p, place=True)
    p.add_argument("--version-type", choices=["Published", "Saved"], default="Published")
    p.set_defaults(func=cmd_publish)

    # place
    place = sub.add_parser("place", parents=[common], help="lire / modifier les métadonnées d'une place").add_subparsers(
        dest="place_cmd", required=True, metavar="<action>")
    p = place.add_parser("get", parents=[common], help="affiche les infos d'une place")
    _add_target(p, place=True)
    p.set_defaults(func=cmd_place_get)
    p = place.add_parser("update", parents=[common], help="modifie nom, description ou taille de serveur")
    _add_target(p, place=True)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--server-size", type=int)
    p.set_defaults(func=cmd_place_update)

    # servers
    servers = sub.add_parser("servers", parents=[common], help="gestion des serveurs").add_subparsers(
        dest="servers_cmd", required=True, metavar="<action>")
    p = servers.add_parser("restart", parents=[common], help="redémarre les serveurs sur la dernière version publiée")
    _add_target(p, place=False)
    p.set_defaults(func=cmd_restart)

    # datastore
    ds = sub.add_parser("datastore", parents=[common], aliases=["ds"], help="lire / écraser les DataStores").add_subparsers(
        dest="ds_cmd", required=True, metavar="<action>")

    p = ds.add_parser("get", parents=[common], help="affiche la valeur d'une clé")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.add_argument("--raw", action="store_true", help="affiche l'entrée complète (révision, users, attributs)")
    p.set_defaults(func=cmd_ds_get)

    p = ds.add_parser("set", parents=[common], help="écrase (ou crée) la valeur d'une clé")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.add_argument("--value", help="valeur JSON (ex. '{\"coins\": 10}')")
    p.add_argument("--file", help="fichier JSON contenant la valeur")
    p.add_argument("--clear-metadata", action="store_true", help="n'essaie pas de conserver users/attributes")
    p.set_defaults(func=cmd_ds_set)

    p = ds.add_parser("delete", parents=[common], help="supprime une clé")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.set_defaults(func=cmd_ds_delete)

    p = ds.add_parser("list", parents=[common], help="liste les clés")
    p.add_argument("datastore")
    _add_ds_common(p)
    p.add_argument("--prefix")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_ds_list)

    p = ds.add_parser("export", parents=[common], help="exporte un DataStore en JSON {clé: valeur}")
    p.add_argument("datastore")
    _add_ds_common(p)
    p.add_argument("--prefix")
    p.add_argument("--out", required=True, help="fichier de sortie")
    p.set_defaults(func=cmd_ds_export)

    p = ds.add_parser("import", parents=[common], help="écrase un DataStore depuis un JSON {clé: valeur}")
    p.add_argument("datastore"); p.add_argument("file")
    _add_ds_common(p)
    p.add_argument("--no-snapshot", action="store_true", help="ne pas prendre de snapshot avant l'écriture")
    p.add_argument("--clear-metadata", action="store_true", help="n'essaie pas de conserver users/attributes")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ds_import)

    p = ds.add_parser("stores", parents=[common], help="liste les DataStores de l'expérience")
    _add_target(p, place=False)
    p.add_argument("--prefix", help="seulement les noms commençant par ce préfixe")
    p.set_defaults(func=cmd_ds_stores)

    p = ds.add_parser("snapshot", parents=[common], help="prend un snapshot des DataStores (1 par jour UTC)")
    _add_target(p, place=False)
    p.set_defaults(func=cmd_ds_snapshot)

    # player (ProfileStore / DataService)
    player = sub.add_parser(
        "player", parents=[common], aliases=["players"],
        help="données joueurs ProfileStore / DataService (backup, restauration, édition)",
    ).add_subparsers(dest="player_cmd", required=True, metavar="<action>")

    def player_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = player.add_parser(name, parents=[common], help=help_text)
        _add_target(sp, place=False)
        g = sp.add_argument_group("emplacement des profils (défaut : section [players] ou Default / PLAYER_)")
        g.add_argument("--datastore", help="nom du ProfileStore (option profileStoreIndex de DataService)")
        g.add_argument("--prefix", help="préfixe des clés (option profileStoreDataPrefix)")
        g.add_argument("--scope", help="scope du DataStore")
        return sp

    def safety(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="montre les changements sans rien écrire")
        sp.add_argument("--force", action="store_true",
                        help="écrit même si le joueur semble en ligne (déconseillé)")
        sp.add_argument("--safety-dir", default=".robops/safety",
                        help="dossier des copies de sécurité (défaut : .robops/safety)")

    p = player_parser("get", "affiche les données d'un joueur et son état de session")
    p.add_argument("user_id", help="UserId Roblox du joueur")
    p.add_argument("--raw", action="store_true", help="profil complet (MetaData, GlobalUpdates…)")
    p.set_defaults(func=cmd_player_get)

    p = player_parser("backup", "sauvegarde tous les profils dans un fichier JSON (.json ou .json.gz)")
    p.add_argument("--out", required=True, help="fichier de sortie (.json.gz pour compresser)")
    p.add_argument("--limit", type=int, help="nombre maximum de profils (pour tester)")
    p.add_argument("--workers", type=int, default=4, help="lectures en parallèle (1 à 16, défaut 4)")
    p.set_defaults(func=cmd_player_backup)

    p = player_parser("restore", "remet un ou plusieurs joueurs à l'état d'un backup")
    p.add_argument("user_ids", nargs="*", metavar="USER_ID", help="UserId à restaurer")
    p.add_argument("--from", dest="backup", required=True, help="fichier de backup")
    p.add_argument("--all", action="store_true", help="restaure tous les joueurs du backup")
    p.add_argument("--yes", action="store_true", help="confirme la restauration avec --all")
    safety(p)
    p.set_defaults(func=cmd_player_restore)

    p = player_parser("edit", "modifie des champs de Data d'un joueur hors ligne")
    p.add_argument("user_id", help="UserId Roblox du joueur")
    p.add_argument("--set", action="append", metavar="CHEMIN=VALEUR",
                   help="ex. --set coins=500 --set stats.level=10 --set 'inventory=[\"sword\"]'")
    p.add_argument("--unset", action="append", metavar="CHEMIN", help="supprime un champ de Data")
    p.add_argument("--data-file", help="remplace tout Data par le contenu de ce JSON")
    safety(p)
    p.set_defaults(func=cmd_player_edit)

    p = player_parser("delete", "supprime le profil d'un joueur (ex. demande d'effacement Roblox)")
    p.add_argument("user_id", help="UserId Roblox du joueur")
    p.add_argument("--yes", action="store_true", help="confirme la suppression")
    p.add_argument("--force", action="store_true", help="supprime même si le joueur semble en ligne")
    p.add_argument("--safety-dir", default=".robops/safety", help="dossier de la copie de sécurité")
    p.set_defaults(func=cmd_player_delete)

    # check / init
    p = sub.add_parser("check", parents=[common], help="valide la configuration (et l'accès API avec --online)")
    p.add_argument("environment", nargs="?", help="environnement à vérifier (défaut : tous)")
    p.add_argument("--online", action="store_true", help="vérifie aussi la clé et les IDs auprès de Roblox")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("self-update", parents=[common], help="met à jour robops.exe depuis les releases GitHub")
    p.add_argument("--check", action="store_true", help="indique seulement si une mise à jour existe")
    p.add_argument("--force", action="store_true", help="réinstalle la dernière version même si à jour")
    p.set_defaults(func=cmd_self_update)

    p = sub.add_parser("init", parents=[common], help="crée robops.toml et le workflow GitHub Actions")
    p.add_argument("--force", action="store_true", help="écrase les fichiers existants")
    p.add_argument("--action-repo", metavar="COMPTE/robops",
                   help="dépôt GitHub de robops utilisé par le workflow (défaut : copie locale tools/robops)")
    p.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    out.ensure_utf8_output()
    updater.cleanup_after_update()
    parser = build_parser()
    args = parser.parse_args(argv)
    code = _run(args)
    if args.command != "self-update":
        # Vérification quotidienne, silencieuse, jamais bloquante (désactivée en CI et via pip).
        updater.maybe_auto_update()
    return code


def _run(args) -> int:
    try:
        return args.func(args)
    except (UsageError, ConfigError) as exc:
        out.error(str(exc))
        return EXIT_USAGE
    except (DeployError, pl.PlayerError) as exc:
        out.error(str(exc))
        return EXIT_FAILURE
    except RobloxApiError as exc:
        out.error(exc.describe())
        return EXIT_FAILURE
    except OSError as exc:
        out.error(f"Erreur de fichier : {exc}")
        return EXIT_FAILURE
    except KeyboardInterrupt:
        out.error("Interrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
