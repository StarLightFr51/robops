"""Entry point of the `robops` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__, updater
from . import output as out
from . import players as pl
from .checks import check_environment
from .claude_desktop import ClaudeDesktopError
from .client import OpenCloudClient, RobloxApiError
from .config import DEFAULT_CONFIG, ConfigError, load_config
from .deploy import DeployError, DeployOptions, deploy, overwrite_entry

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


# ----------------------------------------------------------------- helpers

def _api_key(env_var: str) -> str:
    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    if out.in_github_actions():
        repo = os.environ.get("GITHUB_REPOSITORY", "this repository")
        raise UsageError(
            f"The secret {env_var} is empty for this run. It must be stored in the repository {repo} "
            "(the one that launches the workflow, not the robops repository): Settings → Secrets and variables → "
            f"Actions → 'Secrets' tab → 'Repository secrets', named exactly {env_var}. "
            "A secret placed in another repository, in the 'Variables' tab, in Dependabot/Codespaces "
            "or in a GitHub Environment is not visible here."
        )
    raise UsageError(
        f"{env_var} is not set on this machine. GitHub secrets only exist during runs on GitHub: "
        "to publish, use the 'Run workflow' button in the Actions tab. "
        f"To run robops locally, in PowerShell:  $env:{env_var} = \"your-key\""
    )


def _client(env_var: str) -> OpenCloudClient:
    return OpenCloudClient(_api_key(env_var))


def _resolve_target(args, *, need_place: bool) -> tuple[int, int | None, str]:
    """Returns (universe_id, place_id, key_variable_name) from --env or the explicit IDs."""
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
                    raise UsageError(f"Place '{args.place}' not found in environment {env.name}.")
                place_id = match[0].place_id
            elif len(env.places) == 1:
                place_id = env.places[0].place_id
            else:
                names = ", ".join(p.name for p in env.places)
                raise UsageError(f"Several places in {env.name}: specify --place ({names}).")
        return args.universe_id or env.universe_id, place_id, key_env
    if not args.universe_id:
        raise UsageError("Specify --env <environment> or --universe-id.")
    if need_place and not getattr(args, "place_id", None):
        raise UsageError("Specify --place-id (or --env with --place).")
    return args.universe_id, getattr(args, "place_id", None), key_env or "ROBLOX_API_KEY"


def _place_target(args) -> tuple[int, int, str]:
    """(universe_id, place_id, key variable) for the commands that act on one place."""
    universe_id, place_id, key_env = _resolve_target(args, need_place=True)
    if place_id is None:  # _resolve_target already refuses this case
        raise UsageError("Specify --place-id (or --env with --place).")
    return universe_id, place_id, key_env


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _parse_value(args) -> Any:
    if args.value is not None and args.file is not None:
        raise UsageError("Use --value OR --file, not both.")
    if args.file is not None:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.value is not None:
        text = args.value
    else:
        raise UsageError("Specify --value '<json>' or --file <path.json>.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if args.file is None:
            # Value passed on the command line that is not JSON: treat it as a string.
            return text
        raise UsageError(f"{args.file} is not valid JSON.") from None


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
        _api_key(env.api_key_env)  # fails early, before the build, if the key is missing

    try:
        report = deploy(env, opts, project_root=cfg.root, client_factory=lambda: _client(env.api_key_env))
    except Exception as exc:
        partial = getattr(exc, "deploy_report", None)
        if partial is not None and not partial.dry_run:
            _publish_report(env.name, partial)
            if partial.published():
                out.warn("Already live before the failure: "
                         + ", ".join(f"{p.name} v{p.version}" for p in partial.published()))
        raise
    _publish_report(env.name, report)

    if args.json:
        _print_json({"environment": env.name, "universe_id": env.universe_id, "versions": report.versions(),
                     "restarted": report.restarted, "dry_run": report.dry_run})
    elif not report.dry_run:
        published = ", ".join(f"{p.name} v{p.version}" for p in report.places)
        out.notice(f"Deployment '{env.name}' finished: {published}")
    return EXIT_OK


def _publish_report(environment: str, report) -> None:
    """GitHub outputs + run summary (also written when the deployment failed halfway)."""
    out.set_output("environment", environment)
    out.set_output("universe_id", str(report.universe_id))
    out.set_output("versions", json.dumps(report.versions()))
    out.set_output("failed", "true" if report.error else "false")
    for place in report.places:
        if place.version is not None:
            out.set_output(f"version_{place.name}", str(place.version))
    out.write_summary(report.to_markdown())


def cmd_publish(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise UsageError(f"File not found: {path}")
    if path.suffix.lower() not in (".rbxl", ".rbxlx"):
        raise UsageError(f"{path.name}: a place must be a .rbxl or .rbxlx file.")
    universe_id, place_id, key_env = _place_target(args)
    result = _client(key_env).publish_place(universe_id, place_id, path, version_type=args.version_type)
    out.success(f"Place {place_id} overwritten: version {result.version_number} ({result.version_type})")
    out.set_output("version", str(result.version_number))
    return EXIT_OK


def cmd_place_get(args) -> int:
    universe_id, place_id, key_env = _place_target(args)
    _print_json(_client(key_env).get_place(universe_id, place_id))
    return EXIT_OK


def cmd_place_update(args) -> int:
    if args.name is None and args.description is None and args.server_size is None:
        raise UsageError("Nothing to update: specify --name, --description and/or --server-size.")
    universe_id, place_id, key_env = _place_target(args)
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
    out.success(f"Server restart requested for universe {universe_id}.")
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
    out.success(f"{args.datastore}/{args.key} overwritten (revision {entry.get('revisionId', '?')}).")
    return EXIT_OK


def cmd_ds_delete(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    _client(key_env).delete_entry(universe_id, args.datastore, args.key, scope=args.scope)
    out.success(f"{args.datastore}/{args.key} marked as deleted (permanently purged within 30 days).")
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
    out.success(f"{len(data)} entries exported to {args.out}")
    return EXIT_OK


def cmd_ds_import(args) -> int:
    path = Path(args.file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"File not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise UsageError(f"{path} must contain a JSON object {{\"key\": value}}.")
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    if args.dry_run:
        out.notice(f"Dry-run: {len(data)} entries would be overwritten in {args.datastore}.")
        for key in data:
            print(f"  {key}")
        return EXIT_OK
    client = _client(key_env)
    if not args.no_snapshot:
        snap = client.snapshot_datastores(universe_id)
        out.info(f"Snapshot: {'created' if snap.get('newSnapshotTaken') else 'already taken today'}.")
    for key, value in data.items():
        overwrite_entry(client, universe_id, args.datastore, key, value,
                        scope=args.scope, preserve_metadata=not args.clear_metadata)
    out.success(f"{len(data)} entries overwritten in {args.datastore}.")
    return EXIT_OK


def cmd_ds_stores(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    names = list(_client(key_env).list_datastores(universe_id, prefix=args.prefix))
    for name in names:
        print(name)
    _status(f"{len(names)} DataStore(s) in universe {universe_id}.")
    return EXIT_OK


def cmd_ds_snapshot(args) -> int:
    universe_id, _, key_env = _resolve_target(args, need_place=False)
    _print_json(_client(key_env).snapshot_datastores(universe_id))
    return EXIT_OK


# ------------------------------------------------------- players (ProfileStore)

def _players_target(args) -> tuple[int, pl.PlayersConfig, str]:
    """Universe + profile location ([players] config, overridden by the options)."""
    key_env = args.api_key_env
    if args.env:
        env = load_config(args.config).env(args.env)
        cfg = env.players
        universe_id = args.universe_id or env.universe_id
        key_env = key_env or env.api_key_env
    else:
        if not args.universe_id:
            raise UsageError("Specify --env <environment> or --universe-id.")
        cfg, universe_id = pl.PlayersConfig(), args.universe_id
    if args.datastore:
        cfg.datastore = args.datastore
    if args.prefix is not None:
        cfg.key_prefix = args.prefix
    if args.scope:
        cfg.scope = args.scope
    return universe_id, cfg, key_env or "ROBLOX_API_KEY"


def _status(msg: str) -> None:
    # Goes to stderr so that stdout stays usable JSON (jq, redirects…).
    print(msg, file=sys.stderr, flush=True)


def _ensure_writable(user_id: str, current: pl.CurrentProfile, force: bool) -> None:
    if not current.exists or not pl.is_profile(current.value):
        return
    state = pl.session_state(current.value)
    if state.status == "stale":
        out.warn(f"Player {user_id}: {state.detail}. The lock will be removed.")
    if not state.writable:
        if not force:
            raise pl.PlayerError(
                f"Player {user_id}: {state.detail}. Write refused: their server would overwrite the "
                "change on its next save. Wait for them to disconnect, or edit them "
                "from inside the game (admin command)."
            )
        out.warn(f"Player {user_id}: {state.detail}. --force: writing despite the lock.")


def _print_changes(user_id: str, changes: list[str]) -> None:
    out.info(f"Player {user_id}: {len(changes)} field(s) changed")
    for line in changes[:50]:
        out.info(f"    {line}")
    if len(changes) > 50:
        out.info(f"    … and {len(changes) - 50} more")


def cmd_player_get(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    current = pl.fetch_profile(_client(key_env), universe_id, cfg, args.user_id)
    if not current.exists:
        raise pl.PlayerError(f"No profile for {cfg.key(args.user_id)} in {cfg.datastore}.")
    if not pl.is_profile(current.value):
        _status("⚠ This entry does not have the ProfileStore structure (Data / MetaData).")
        _print_json(current.value)
        return EXIT_OK
    _status(f"Player {args.user_id}: {pl.session_state(current.value).detail}")
    _print_json(current.value if args.raw else current.value.get("Data"))
    return EXIT_OK


def cmd_player_backup(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    client = _client(key_env)
    out.info(f"Backup of {cfg.datastore}/{cfg.key_prefix}* (universe {universe_id}) → {args.out}")
    last = [0]

    def progress(count: int) -> None:
        if count - last[0] >= 500:
            last[0] = count
            out.info(f"  … {count} profile(s)")

    result = pl.backup_players(client, universe_id, cfg, Path(args.out), limit=args.limit,
                               workers=args.workers, progress=progress)
    out.set_output("backup_file", str(result.path))
    out.set_output("backup_count", str(result.count))
    out.success(f"{result.count} profile(s) saved to {result.path}")
    if result.count == 0 and not result.errors and result.other_keys:
        guess = pl.guess_prefix(result.other_examples or [])
        out.warn(
            f"The DataStore '{cfg.datastore}' contains {result.other_keys} key(s), but none of them starts with "
            f"'{cfg.key_prefix}'. Examples: {', '.join(result.other_examples or [])}"
        )
        if guess is not None:
            out.info(f"  → In robops.toml, in the [players] section, set:  key_prefix = \"{guess}\"   then run the backup again.")
    elif result.count == 0 and not result.errors:
        out.warn(
            f"No profile found: the DataStore '{cfg.datastore}' is empty or does not exist (universe {universe_id}). "
            "Possible causes:\n"
            "  1. Wrong universe: check universe_id in robops.toml (the one of the game your players play in).\n"
            "  2. Different DataStore name: look at profileStoreIndex in your DataService:init, then the "
            "[players] section of robops.toml (Creator Dashboard → your game → Configurations → Data stores manager).\n"
            "  3. No data saved yet: tests only in Studio without 'Enable Studio Access "
            "to API Services', or useMock enabled in DataService."
        )
    if result.errors:
        for user_id, error in list(result.errors.items())[:20]:
            out.warn(f"{user_id}: {error}")
        out.error(f"{len(result.errors)} profile(s) could not be read (listed in the file).")
        return EXIT_FAILURE
    return EXIT_OK


def _restore_one(client, universe_id: int, cfg: pl.PlayersConfig, user_id: str, record: dict, args) -> str:
    backup_profile = record.get("value")
    if not pl.is_profile(backup_profile):
        raise pl.PlayerError(f"Player {user_id}: the backup entry is not a ProfileStore profile.")
    current = pl.fetch_profile(client, universe_id, cfg, user_id)
    _ensure_writable(user_id, current, args.force)

    if not current.exists:
        if args.all and not args.recreate_missing:
            # Never resurrect in bulk a profile deleted since the backup: it may have been
            # erased following a data erasure request (right to be forgotten).
            out.info(f"Player {user_id}: no current profile, skipped (add --recreate-missing to recreate it).")
            return "skipped (no current profile)"
        out.warn(f"Player {user_id}: no current profile, it will be recreated from the backup. "
                 "Do not do this for a player deleted following a data erasure request.")

    current_ok = current.exists and pl.is_profile(current.value)
    if current.exists and not current_ok:
        out.warn(f"Player {user_id}: current profile unreadable, it will be replaced by the full profile from the backup.")
    before = current.value.get("Data") if current_ok else None
    changes = pl.changed_fields(before, backup_profile["Data"])
    stale = current_ok and pl.session_state(current.value).status == "stale"
    if current_ok and not changes and not stale:
        out.info(f"Player {user_id}: already identical to the backup, nothing to do.")
        return "unchanged"

    new_profile = pl.with_data(current.value if current_ok else None, backup_profile["Data"],
                               fallback_profile=backup_profile)
    if current.exists and not changes:
        out.info(f"Player {user_id}: data identical to the backup, only the stale lock will be removed.")
    elif current.exists:
        _print_changes(user_id, changes)
    if args.dry_run:
        return "simulated"
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, user_id, current)
    pl.write_profile(client, universe_id, cfg, user_id, new_profile, current, users=record.get("users") or None)
    out.success(f"Player {user_id} restored" + (f" (previous profile copied to {safety})" if safety else ""))
    return "restored"


def cmd_player_restore(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    backup = pl.load_backup(Path(args.backup), cfg, universe_id=universe_id,
                            allow_other_universe=args.allow_other_universe)
    if args.all:
        if args.user_ids:
            raise UsageError("Use --all OR a list of players, not both.")
        if not args.yes and not args.dry_run:
            raise UsageError(
                f"--all will reset {len(backup)} player(s) to the backup state, overwriting any progress "
                "made since. Run again with --dry-run to see the impact, then with --yes to confirm."
            )
        user_ids = list(backup)
    else:
        if not args.user_ids:
            raise UsageError("Specify one or more UserIds, or --all.")
        user_ids = [str(u) for u in args.user_ids]
        missing = [u for u in user_ids if u not in backup]
        if missing:
            raise UsageError(f"Missing from the backup: {', '.join(missing)}")

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

    summary = ", ".join(f"{n} {k}" for k, n in tally.items()) or "no players"
    label = "Restore (dry-run)" if args.dry_run else "Restore"
    if failures:
        message = (f"{label}: {summary}; {len(failures)} player(s) could not be restored "
                   f"(online or errored): " + ", ".join(list(failures)[:20]))
        if args.dry_run:
            out.warn(message)
            return EXIT_OK
        out.error(message + ". Run the command again for them later.")
        return EXIT_FAILURE
    out.notice(f"{label}: {summary}.")
    return EXIT_OK


def cmd_player_edit(args) -> int:
    if not (args.set or args.unset or args.data_file):
        raise UsageError("Nothing to change: use --set path=value, --unset path or --data-file.")
    universe_id, cfg, key_env = _players_target(args)
    client = _client(key_env)
    current = pl.fetch_profile(client, universe_id, cfg, args.user_id)
    if not current.exists:
        raise pl.PlayerError(f"No profile for {cfg.key(args.user_id)} (has the player never joined?).")
    if not pl.is_profile(current.value):
        raise pl.PlayerError(f"{cfg.key(args.user_id)} does not have the ProfileStore structure (Data / MetaData).")
    _ensure_writable(args.user_id, current, args.force)

    data = current.value.get("Data")
    if args.data_file:
        try:
            data = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"{args.data_file} unreadable: {exc}") from None
    new_data = pl.apply_edits(data, args.set or [], args.unset or [])
    changes = pl.changed_fields(current.value.get("Data"), new_data)
    if not changes:
        out.info("No difference from the current profile, nothing to write.")
        return EXIT_OK
    _print_changes(args.user_id, changes)
    if args.dry_run:
        out.notice("Dry-run: nothing was written.")
        return EXIT_OK
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, args.user_id, current)
    pl.write_profile(client, universe_id, cfg, args.user_id, pl.with_data(current.value, new_data), current)
    out.success(f"Player {args.user_id} edited (previous profile copied to {safety}).")
    return EXIT_OK


def cmd_player_delete(args) -> int:
    universe_id, cfg, key_env = _players_target(args)
    if not args.yes:
        raise UsageError(f"Delete the profile of {args.user_id}? Run again with --yes to confirm.")
    client = _client(key_env)
    current = pl.fetch_profile(client, universe_id, cfg, args.user_id)
    if not current.exists:
        out.info(f"No profile for {cfg.key(args.user_id)}, nothing to delete.")
        return EXIT_OK
    _ensure_writable(args.user_id, current, args.force)
    safety = pl.save_safety_copy(Path(args.safety_dir), cfg, args.user_id, current)
    client.delete_entry(universe_id, cfg.datastore, cfg.key(args.user_id), scope=cfg.scope)
    out.success(f"Profile of {args.user_id} deleted (permanently purged by Roblox within 30 days; "
                f"local copy: {safety}).")
    return EXIT_OK


def cmd_self_update(args) -> int:
    if not updater.is_frozen():
        out.info("robops is installed with pip: update it with  pip install -U <robops folder or repository>")
        return EXIT_OK
    repo = updater.repository()
    session = updater.make_session(updater.github_token())
    try:
        release = updater.latest_release(repo, session=session)
        updater.record_check()
        if not updater.is_newer(release.version, __version__) and not args.force:
            out.success(f"robops {__version__} is up to date (latest version: {release.version}).")
            return EXIT_OK
        if args.check:
            out.notice(f"New version available: {release.version} (installed: {__version__}). "
                       "Run  robops self-update  to install it.")
            return EXIT_OK
        out.info(f"Downloading robops {release.version} from github.com/{repo} …")
        updater.update_executable(release, updater.current_executable(), session=session)
    except updater.UpdateError as exc:
        out.error(str(exc))
        return EXIT_FAILURE
    out.success(f"robops updated: {__version__} → {release.version}. Run the command again to use it.")
    return EXIT_OK


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    names = [args.environment] if args.environment else cfg.names
    printers = {"ok": out.success, "info": out.info, "warn": out.warn, "error": out.error}
    problems = 0
    for name in names:
        with out.group(f"Environment {name}"):
            env, findings = check_environment(cfg, name)
            for finding in findings:
                printers[finding.level](finding.message)
            problems += sum(1 for f in findings if f.level == "error")
            if env is None or not args.online:
                continue
            if not os.environ.get(env.api_key_env):
                problems += 1
                continue
            client = _client(env.api_key_env)
            for place in env.places:
                try:
                    info = client.get_place(env.universe_id, place.place_id)
                    out.success(f"  {place.name} reachable: '{info.get('displayName', '?')}'")
                except RobloxApiError as exc:
                    out.error(f"  {place.name}: {exc.describe()}")
                    problems += 1
    if problems:
        out.error(f"{problems} problem(s) detected.")
        return EXIT_FAILURE
    out.success("Configuration is valid.")
    return EXIT_OK


def cmd_dashboard(args) -> int:
    from . import dashboard as dash
    from . import github

    config_path = Path(args.config)
    cfg = load_config(config_path)                      # fails early on a broken robops.toml
    repo = args.github_repo or github.repo_from_remote(cfg.root)
    token = github.token()
    if not repo:
        out.warn("GitHub repository not found (no 'origin' remote): deployments and GitHub backups are hidden. "
                 "Pass --github-repo owner/name.")
    elif not token:
        out.info("No ROBOPS_GITHUB_TOKEN: deployments and backups are only visible for a public repository, "
                 "and the Deploy / Backup buttons need a token with Actions: Read and write.")
    app = dash.Dashboard(config_path, github_repo=repo, github_token=token,
                         deploy_workflow=args.deploy_workflow, backup_workflow=args.backup_workflow,
                         sample_every=max(0.0, args.sample_every) * 60,
                         claude_logs=Path(args.claude_logs) if args.claude_logs else None)
    dash.serve(app, port=args.port, open_browser=not args.no_browser)
    return EXIT_OK


def cmd_stats_record(args) -> int:
    from .roblox_web import RobloxWeb, WebApiError
    from .stats import History, take_sample

    cfg = load_config(args.config)
    history = History(cfg.root)
    web = RobloxWeb()
    failures = 0
    for name in [args.env] if args.env else cfg.names:
        env = cfg.env(name)
        try:
            sample = take_sample(web, env.universe_id)
        except WebApiError as exc:
            out.error(f"{name}: {exc}")
            failures += 1
            continue
        history.append(name, sample)
        out.success(f"{name}: {sample['playing']} playing, {sample['visits']} visits recorded "
                    f"in {history.path(name).relative_to(cfg.root)}")
    return EXIT_FAILURE if failures else EXIT_OK


def _project_dir(args) -> Path:
    """The game folder: --project, otherwise the folder of robops.toml (-c)."""
    project = Path(args.project).expanduser() if getattr(args, "project", None) else Path(args.config).resolve().parent
    if not project.is_dir():
        raise UsageError(f"Project folder not found: {project}")
    return project.resolve()


def _config_files(args) -> list[Path] | None:
    return [Path(f).expanduser() for f in args.config_file] if getattr(args, "config_file", None) else None


def cmd_mcp_serve(args) -> int:
    from . import mcp_server

    return mcp_server.serve(_project_dir(args))


def cmd_mcp_install(args) -> int:
    from . import claude_desktop as cd

    project = _project_dir(args)
    if not (project / "robops.toml").is_file():
        out.warn(f"No robops.toml in {project}: run this from your game folder, or pass --project <folder>.")
    changes = cd.install(project, name=args.name, files=_config_files(args))
    for change in changes:
        if change.status == "unchanged":
            out.success(f"Already set up in {change.path}")
            continue
        out.success(f"robops {'added to' if change.status == 'added' else 'updated in'} {change.path}"
                    + (f" (previous version kept in {change.backup.name})" if change.backup else ""))
        if change.previous_project:
            out.warn(f"The '{args.name}' entry pointed to another project ({change.previous_project}): it now points "
                     f"to {project}. To keep both, install this one with --name robops-<game>.")
    entry = cd.server_entry(project)
    if any(c.status != "unchanged" for c in changes):
        out.notice(cd.RESTART)
    state = cd.status(project, name=args.name, files=_config_files(args))
    if state["studio"]:
        out.success(f"Roblox Studio MCP found in Claude Desktop: {', '.join(state['studio'])}")
    else:
        out.warn("Roblox Studio's MCP server is not in Claude Desktop's config yet. " + cd.STUDIO_HINT)
    out.info("\nThen, in Claude Desktop: \"+\" → robops → simulate_players, or ask Claude: "
             "\"Simulate 5 players on my game in Studio and save the report with robops\".\n"
             "Reports appear in robops dashboard → Simulated players.\n"
             f"For Claude Code instead:  {cd.claude_code_command(entry, args.name)}")
    return EXIT_OK


def cmd_mcp_uninstall(args) -> int:
    from . import claude_desktop as cd

    changes = cd.uninstall(name=args.name, files=_config_files(args))
    if not changes:
        raise UsageError(cd.NOT_FOUND)
    for change in changes:
        if change.status == "removed":
            out.success(f"robops removed from {change.path}")
        else:
            out.info(f"No '{args.name}' entry in {change.path}")
    return EXIT_OK


def cmd_mcp_status(args) -> int:
    from . import claude_desktop as cd
    from . import playtests as pt

    project = _project_dir(args)
    state = cd.status(project, name=args.name, files=_config_files(args))
    if not state["found"]:
        out.warn(cd.NOT_FOUND)
    for config in state["configs"]:
        out.info(f"\n{config['path']}{'' if config['exists'] else ' (does not exist yet)'}")
        if config["error"]:
            out.error(config["error"])
            continue
        robops = config["robops"]
        if not robops:
            out.warn("  robops: not installed (robops mcp install)")
        else:
            where = "this project" if robops["this_project"] else f"another project: {robops['project']}"
            (out.success if robops["this_project"] and robops["command_found"] else out.warn)(
                f"  robops: installed for {where}")
            if not robops["command_found"]:
                out.warn(f"  the program it starts does not exist any more: {robops['command']} "
                         "(run robops mcp install again)")
        if config["studio"]:
            out.success(f"  Roblox Studio MCP: {', '.join(config['studio'])}")
        else:
            out.warn("  Roblox Studio MCP: not found. " + cd.STUDIO_HINT)
    reports = pt.sort_reports([{"key": k} for k in pt.local_reports(project)])
    out.info(f"\nReports in {project}: {len(reports)}" + (f" (latest: {reports[0]['key']})" if reports else ""))
    return EXIT_OK


def _point_to_action_repo(workflow: str, action_repo: str) -> str:
    """Replaces "uses: ./tools/robops[/sub-action]" with robops' GitHub repository.

    e.g. ./tools/robops/backup → alex/robops/backup@v1.3.0

    Without an explicit @ref, the workflow is pinned to the tag of this robops
    version rather than @main: the deployment receives the Roblox API key, so it
    must not run whatever lands on robops' main branch later.
    """
    import re

    repo, _, ref = action_repo.partition("@")
    ref = ref or f"v{__version__}"
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
    # Where the workflow loads the robops action from: robops' GitHub repository if known,
    # otherwise a local copy in tools/robops.
    action_repo = (args.action_repo or updater.configured_repository()).strip()
    for template, dest in targets.items():
        if dest.exists() and not args.force:
            out.warn(f"{dest} already exists (use --force to overwrite it).")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = (templates / template).read_text(encoding="utf-8")
        if template.endswith(".yml") and action_repo:
            content = _point_to_action_repo(content, action_repo)
        dest.write_text(content, encoding="utf-8")
        out.success(f"{dest} created")

    # Safety copies and backups contain players' personal data:
    # make sure they never end up in Git.
    gitignore = Path(".gitignore")
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    wanted = [entry for entry in (".robops/", "backups/") if entry not in lines]
    if wanted:
        prefix = "\n" if lines and lines[-1].strip() else ""
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "# robops: player data (never commit)\n" + "\n".join(wanted) + "\n")
        out.success(f".gitignore: added {', '.join(wanted)}")

    out.info("Fill in your IDs in robops.toml, then add the ROBLOX_API_KEY secret on GitHub.")
    return EXIT_OK


# ------------------------------------------------------------------ parser

def _add_target(p: argparse.ArgumentParser, *, place: bool) -> None:
    g = p.add_argument_group("target")
    g.add_argument("--env", help="robops.toml environment (provides universe/place/key)")
    g.add_argument("--universe-id", type=int, help="universe (experience) ID")
    if place:
        g.add_argument("--place", help="name of the place in the environment")
        g.add_argument("--place-id", type=int, help="place ID")


def _add_ds_common(p: argparse.ArgumentParser) -> None:
    _add_target(p, place=False)
    p.add_argument("--scope", help="DataStore scope (default: global)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robops",
        description="Roblox DevOps: overwrites the places and data of an experience via Open Cloud.",
    )
    parser.add_argument("--version", action="version", version=f"robops {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: robops.toml)")
    parser.add_argument("--api-key-env", help="variable holding the API key (default: ROBLOX_API_KEY)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # The same options are accepted after the subcommand (robops deploy staging -c other.toml).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--api-key-env", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # deploy
    p = sub.add_parser("deploy", parents=[common], help="build + full publish of an environment")
    p.add_argument("environment", help="environment name (e.g. staging, production)")
    p.add_argument("--version-type", choices=["Published", "Saved"], help="override the version type")
    p.add_argument("--place", action="append", metavar="NAME", help="deploy only this place (repeatable)")
    p.add_argument("--skip-build", action="store_true", help="do not run the build command")
    p.add_argument("--with-datastores", action="store_true", help="also overwrite the configured DataStores")
    restart = p.add_mutually_exclusive_group()
    restart.add_argument("--restart", dest="restart", action="store_true", default=None,
                         help="restart the servers after publishing")
    restart.add_argument("--no-restart", dest="restart", action="store_false", help="do not restart the servers")
    p.add_argument("--dry-run", action="store_true", help="build + checks, without any call to Roblox")
    p.add_argument("--strict-metadata", action="store_true", help="fail if the metadata update fails")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(func=cmd_deploy)

    # publish
    p = sub.add_parser("publish", parents=[common], help="overwrite a place with a .rbxl/.rbxlx file")
    p.add_argument("file", help=".rbxl or .rbxlx file")
    _add_target(p, place=True)
    p.add_argument("--version-type", choices=["Published", "Saved"], default="Published")
    p.set_defaults(func=cmd_publish)

    # place
    place = sub.add_parser("place", parents=[common], help="read / change a place's metadata").add_subparsers(
        dest="place_cmd", required=True, metavar="<action>")
    p = place.add_parser("get", parents=[common], help="show a place's info")
    _add_target(p, place=True)
    p.set_defaults(func=cmd_place_get)
    p = place.add_parser("update", parents=[common], help="change name, description or server size")
    _add_target(p, place=True)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--server-size", type=int)
    p.set_defaults(func=cmd_place_update)

    # servers
    servers = sub.add_parser("servers", parents=[common], help="server management").add_subparsers(
        dest="servers_cmd", required=True, metavar="<action>")
    p = servers.add_parser("restart", parents=[common], help="restart the servers on the latest published version")
    _add_target(p, place=False)
    p.set_defaults(func=cmd_restart)

    # datastore
    ds = sub.add_parser("datastore", parents=[common], aliases=["ds"], help="read / overwrite DataStores").add_subparsers(
        dest="ds_cmd", required=True, metavar="<action>")

    p = ds.add_parser("get", parents=[common], help="show a key's value")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.add_argument("--raw", action="store_true", help="show the full entry (revision, users, attributes)")
    p.set_defaults(func=cmd_ds_get)

    p = ds.add_parser("set", parents=[common], help="overwrite (or create) a key's value")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.add_argument("--value", help="JSON value (e.g. '{\"coins\": 10}')")
    p.add_argument("--file", help="JSON file containing the value")
    p.add_argument("--clear-metadata", action="store_true", help="do not try to keep users/attributes")
    p.set_defaults(func=cmd_ds_set)

    p = ds.add_parser("delete", parents=[common], help="delete a key")
    p.add_argument("datastore"); p.add_argument("key")
    _add_ds_common(p)
    p.set_defaults(func=cmd_ds_delete)

    p = ds.add_parser("list", parents=[common], help="list keys")
    p.add_argument("datastore")
    _add_ds_common(p)
    p.add_argument("--prefix")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_ds_list)

    p = ds.add_parser("export", parents=[common], help="export a DataStore as JSON {key: value}")
    p.add_argument("datastore")
    _add_ds_common(p)
    p.add_argument("--prefix")
    p.add_argument("--out", required=True, help="output file")
    p.set_defaults(func=cmd_ds_export)

    p = ds.add_parser("import", parents=[common], help="overwrite a DataStore from a JSON {key: value}")
    p.add_argument("datastore"); p.add_argument("file")
    _add_ds_common(p)
    p.add_argument("--no-snapshot", action="store_true", help="do not take a snapshot before writing")
    p.add_argument("--clear-metadata", action="store_true", help="do not try to keep users/attributes")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ds_import)

    p = ds.add_parser("stores", parents=[common], help="list the experience's DataStores")
    _add_target(p, place=False)
    p.add_argument("--prefix", help="only names starting with this prefix")
    p.set_defaults(func=cmd_ds_stores)

    p = ds.add_parser("snapshot", parents=[common], help="take a DataStores snapshot (1 per UTC day)")
    _add_target(p, place=False)
    p.set_defaults(func=cmd_ds_snapshot)

    # player (ProfileStore / DataService)
    player = sub.add_parser(
        "player", parents=[common], aliases=["players"],
        help="ProfileStore / DataService player data (backup, restore, edit)",
    ).add_subparsers(dest="player_cmd", required=True, metavar="<action>")

    def player_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = player.add_parser(name, parents=[common], help=help_text)
        _add_target(sp, place=False)
        g = sp.add_argument_group("profile location (default: [players] section or Default / PLAYER_)")
        g.add_argument("--datastore", help="ProfileStore name (DataService profileStoreIndex option)")
        g.add_argument("--prefix", help="key prefix (profileStoreDataPrefix option)")
        g.add_argument("--scope", help="DataStore scope")
        return sp

    def safety(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="show the changes without writing anything")
        sp.add_argument("--force", action="store_true",
                        help="write even if the player appears to be online (not recommended)")
        sp.add_argument("--safety-dir", default=".robops/safety",
                        help="folder for safety copies (default: .robops/safety)")

    p = player_parser("get", "show a player's data and session state")
    p.add_argument("user_id", help="the player's Roblox UserId")
    p.add_argument("--raw", action="store_true", help="full profile (MetaData, GlobalUpdates…)")
    p.set_defaults(func=cmd_player_get)

    p = player_parser("backup", "save all profiles to a JSON file (.json or .json.gz)")
    p.add_argument("--out", required=True, help="output file (.json.gz to compress)")
    p.add_argument("--limit", type=int, help="maximum number of profiles (for testing)")
    p.add_argument("--workers", type=int, default=4, help="parallel reads (1 to 16, default 4)")
    p.set_defaults(func=cmd_player_backup)

    p = player_parser("restore", "reset one or more players to the state of a backup")
    p.add_argument("user_ids", nargs="*", metavar="USER_ID", help="UserIds to restore")
    p.add_argument("--from", dest="backup", required=True, help="backup file")
    p.add_argument("--all", action="store_true", help="restore every player in the backup")
    p.add_argument("--yes", action="store_true", help="confirm the restore with --all")
    p.add_argument("--recreate-missing", action="store_true",
                   help="with --all, also recreate the profiles deleted since the backup (never do this "
                        "if some were deleted following a data erasure request)")
    p.add_argument("--allow-other-universe", action="store_true",
                   help="accept a backup taken in another universe (e.g. staging → production)")
    safety(p)
    p.set_defaults(func=cmd_player_restore)

    p = player_parser("edit", "change fields of an offline player's Data")
    p.add_argument("user_id", help="the player's Roblox UserId")
    p.add_argument("--set", action="append", metavar="PATH=VALUE",
                   help="e.g. --set coins=500 --set stats.level=10 --set 'inventory=[\"sword\"]'")
    p.add_argument("--unset", action="append", metavar="PATH", help="remove a field from Data")
    p.add_argument("--data-file", help="replace all of Data with the content of this JSON")
    safety(p)
    p.set_defaults(func=cmd_player_edit)

    p = player_parser("delete", "delete a player's profile (e.g. Roblox erasure request)")
    p.add_argument("user_id", help="the player's Roblox UserId")
    p.add_argument("--yes", action="store_true", help="confirm the deletion")
    p.add_argument("--force", action="store_true", help="delete even if the player appears to be online")
    p.add_argument("--safety-dir", default=".robops/safety", help="folder for the safety copy")
    p.set_defaults(func=cmd_player_delete)

    # check / init
    p = sub.add_parser("check", parents=[common], help="validate the configuration (and API access with --online)")
    p.add_argument("environment", nargs="?", help="environment to check (default: all)")
    p.add_argument("--online", action="store_true", help="also check the key and IDs against Roblox")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("dashboard", parents=[common],
                       help="local web dashboard: live game, deployments, players, backups, DataStores")
    p.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument("--github-repo", metavar="OWNER/NAME",
                   help="game repository for deployments and backups (default: the 'origin' git remote)")
    p.add_argument("--deploy-workflow", metavar="FILE.yml",
                   help="deploy workflow of the game repository (default: detected, e.g. roblox-deploy.yml)")
    p.add_argument("--backup-workflow", metavar="FILE.yml",
                   help="backup workflow of the game repository (default: detected, e.g. roblox-backup.yml)")
    p.add_argument("--claude-logs", metavar="FOLDER",
                   help="Claude Desktop's log folder, for Claude's actions in Studio (default: the usual place)")
    p.add_argument("--sample-every", type=float, default=5, metavar="MINUTES",
                   help="record the live stats for the Charts view every N minutes (default 5, 0 = off)")
    p.set_defaults(func=cmd_dashboard)

    mcp = sub.add_parser("mcp", parents=[common],
                         help="robops MCP server: Claude saves its player simulation reports for the dashboard")\
        .add_subparsers(dest="mcp_cmd", required=True, metavar="<action>")

    def _mcp_common(p: argparse.ArgumentParser, *, config_file: bool = True) -> None:
        p.add_argument("--project", metavar="FOLDER", help="game folder (default: the folder of robops.toml)")
        if config_file:
            p.add_argument("--name", default="robops", help="server name in Claude Desktop (default: robops)")
            p.add_argument("--config-file", action="append", metavar="PATH",
                           help="claude_desktop_config.json to use (default: the ones Claude Desktop reads)")

    p = mcp.add_parser("install", parents=[common], help="add robops to Claude Desktop (then restart Claude Desktop)")
    _mcp_common(p)
    p.set_defaults(func=cmd_mcp_install)
    p = mcp.add_parser("status", parents=[common], help="is robops (and Roblox Studio's MCP) set up in Claude Desktop?")
    _mcp_common(p)
    p.set_defaults(func=cmd_mcp_status)
    p = mcp.add_parser("uninstall", parents=[common], help="remove robops from Claude Desktop")
    p.add_argument("--name", default="robops", help="server name in Claude Desktop (default: robops)")
    p.add_argument("--config-file", action="append", metavar="PATH", help="claude_desktop_config.json to use")
    p.set_defaults(func=cmd_mcp_uninstall)
    p = mcp.add_parser("serve", parents=[common], help="run the MCP server on stdin/stdout (started by Claude)")
    _mcp_common(p, config_file=False)
    p.set_defaults(func=cmd_mcp_serve)

    stats = sub.add_parser("stats", parents=[common], help="live stats history (Charts view of the dashboard)")\
        .add_subparsers(dest="stats_cmd", required=True, metavar="<action>")
    p = stats.add_parser("record", parents=[common],
                         help="record one sample of the live stats (schedule it to keep a history)")
    p.add_argument("--env", help="only this environment (default: all)")
    p.set_defaults(func=cmd_stats_record)

    p = sub.add_parser("self-update", parents=[common], help="update robops.exe from the GitHub releases")
    p.add_argument("--check", action="store_true", help="only report whether an update exists")
    p.add_argument("--force", action="store_true", help="reinstall the latest version even if up to date")
    p.set_defaults(func=cmd_self_update)

    p = sub.add_parser("init", parents=[common], help="create robops.toml and the GitHub Actions workflow")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument("--action-repo", metavar="ACCOUNT/robops[@REF]",
                   help="robops GitHub repository used by the workflow, pinned to the tag of this version "
                        "unless @REF is given (default: local copy in tools/robops)")
    p.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    out.ensure_utf8_output()
    updater.cleanup_after_update()
    parser = build_parser()
    args = parser.parse_args(argv)
    code = _run(args)
    if args.command != "self-update" and getattr(args, "mcp_cmd", None) != "serve":
        # Daily check: only notifies unless ROBOPS_AUTO_UPDATE=1 (disabled in CI and with pip).
        updater.maybe_auto_update()
    return code


def _run(args) -> int:
    try:
        return args.func(args)
    except (UsageError, ConfigError) as exc:
        out.error(str(exc))
        return EXIT_USAGE
    except (DeployError, pl.PlayerError, ClaudeDesktopError) as exc:
        out.error(str(exc))
        return EXIT_FAILURE
    except RobloxApiError as exc:
        out.error(exc.describe())
        return EXIT_FAILURE
    except OSError as exc:
        out.error(f"File error: {exc}")
        return EXIT_FAILURE
    except KeyboardInterrupt:
        out.error("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 — last resort: a message rather than a traceback
        if os.environ.get("ROBOPS_DEBUG"):
            raise
        out.error(f"Unexpected error ({type(exc).__name__}): {exc}\n"
                  "  This is probably a robops bug. Run again with ROBOPS_DEBUG=1 to see the details.")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
