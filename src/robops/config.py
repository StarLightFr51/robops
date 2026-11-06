"""Loading and validation of the robops.toml file.

Strings may contain ${VARIABLE} references resolved from the environment
(handy for keeping IDs in GitHub variables).
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .players import PlayersConfig

DEFAULT_CONFIG = "robops.toml"

# Fields accepted in each table: anything else is a typo that would otherwise be
# silently ignored (e.g. "restart_server" deploying with the default value).
ROOT_KEYS = {"api_key_env", "build", "players", "playtests", "environments"}
BUILD_KEYS = {"command"}
ENV_KEYS = {"universe_id", "places", "version_type", "restart_servers", "build_command",
            "datastores", "api_key_env", "players", "playtests"}
PLACE_KEYS = {"name", "place_id", "file", "display_name", "description", "server_size"}
DATASTORE_KEYS = {"name", "file", "scope"}
PLAYERS_KEYS = {"datastore", "key_prefix", "scope"}
PLAYTESTS_KEYS = {"datastore"}
DEFAULT_PLAYTESTS_DATASTORE = "RobopsPlaytests"

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", ""}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


@dataclass
class PlaceConfig:
    name: str
    place_id: int
    file: Path
    display_name: str | None = None
    description: str | None = None
    server_size: int | None = None

    @property
    def has_metadata(self) -> bool:
        return any(v is not None for v in (self.display_name, self.description, self.server_size))


@dataclass
class DatastoreSeed:
    name: str
    file: Path
    scope: str | None = None


@dataclass
class Environment:
    name: str
    universe_id: int
    places: list[PlaceConfig]
    version_type: str = "Published"
    restart_servers: bool = False
    build_command: str | None = None
    datastores: list[DatastoreSeed] = field(default_factory=list)
    api_key_env: str = "ROBLOX_API_KEY"
    players: PlayersConfig = field(default_factory=PlayersConfig)
    # DataStore where Claude saves its simulated-player reports (dashboard "Simulated players").
    playtests_datastore: str = DEFAULT_PLAYTESTS_DATASTORE


@dataclass
class Config:
    """Loaded configuration. Each environment is only validated (and its ${VAR}
    resolved) when it is requested: deploying to staging does not require
    the production variables to exist."""

    path: Path
    root: Path
    raw_environments: dict[str, dict]
    default_key_env: str = "ROBLOX_API_KEY"
    default_build: str | None = None
    default_players: dict = field(default_factory=dict)
    default_playtests: dict = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.raw_environments)

    def env(self, name: str) -> Environment:
        if name not in self.raw_environments:
            known = ", ".join(self.names) or "none"
            raise ConfigError(f"Environment '{name}' not found in {self.path} (available: {known}).")
        return _parse_environment(self, name, self.raw_environments[name])


def _expand(value: Any, where: str) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            var, default = match.group(1), match.group(2)
            resolved = os.environ.get(var)
            if resolved is None or resolved == "":
                if default is not None:
                    return default
                raise ConfigError(f"{where}: the environment variable {var} is not set.")
            return resolved
        return _VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v, where) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, f"{where}.{k}") for k, v in value.items()}
    return value


def _as_int(value: Any, where: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be an integer (got: {value!r}).") from None
    if number <= 0:
        raise ConfigError(f"{where} must be greater than 0.")
    return number


def _as_bool(value: Any, where: str) -> bool:
    """TOML boolean, or text coming from ${VAR} ("false" must not become True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ConfigError(f"{where} must be true or false (got: {value!r}).")


def _opt_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be text (got: {value!r}).")
    return value


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if not unknown:
        return
    names = []
    for key in unknown:
        close = difflib.get_close_matches(key, allowed, n=1)
        names.append(f"'{key}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
    raise ConfigError(f"{where}: unknown field(s) {', '.join(names)}. "
                      f"Expected: {', '.join(sorted(allowed))}.")


def _tables(value: Any, where: str) -> list[dict]:
    """List of [[tables]] (places, datastores)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ConfigError(f"{where} must be a list of [[{where}]] sections.")
    return value


def _require(table: dict, key: str, where: str) -> Any:
    if key not in table or table[key] in (None, ""):
        raise ConfigError(f"Required field missing: {where}.{key}")
    return table[key]


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    root = config_path.resolve().parent
    _check_keys(raw, ROOT_KEYS, str(config_path))
    build = raw.get("build") or {}
    if not isinstance(build, dict):
        raise ConfigError("[build] must be a section.")
    _check_keys(build, BUILD_KEYS, "build")
    default_key_env = _opt_str(raw.get("api_key_env"), "api_key_env") or "ROBLOX_API_KEY"
    default_build = _opt_str(build.get("command"), "build.command")
    players = raw.get("players") or {}
    if not isinstance(players, dict):
        raise ConfigError("[players] must be a section.")
    playtests = raw.get("playtests") or {}
    if not isinstance(playtests, dict):
        raise ConfigError("[playtests] must be a section.")
    _check_keys(playtests, PLAYTESTS_KEYS, "playtests")

    envs_raw = raw.get("environments")
    if not isinstance(envs_raw, dict) or not envs_raw:
        raise ConfigError(f"{config_path} must define at least one [environments.<name>] section.")
    for name, table in envs_raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"environments.{name} must be a [environments.{name}] section.")

    return Config(
        path=config_path,
        root=root,
        raw_environments=envs_raw,
        default_key_env=default_key_env,
        default_build=default_build,
        default_players=players,
        default_playtests=playtests,
    )


def _parse_environment(cfg: Config, env_name: str, env_raw: dict) -> Environment:
    root = cfg.root
    where = f"environments.{env_name}"
    _check_keys(env_raw, ENV_KEYS, where)
    env_raw = _expand(env_raw, where)

    version_type = env_raw.get("version_type", "Published")
    if version_type not in ("Published", "Saved"):
        raise ConfigError(f"{where}.version_type must be 'Published' or 'Saved'.")

    places_raw = _tables(env_raw.get("places"), f"{where}.places")
    if not places_raw:
        raise ConfigError(f"{where} must declare at least one [[{where}.places]].")

    places: list[PlaceConfig] = []
    seen: set[str] = set()
    for i, p in enumerate(places_raw):
        pw = f"{where}.places[{i}]"
        _check_keys(p, PLACE_KEYS, pw)
        name = str(p.get("name") or f"place{i + 1}")
        if name in seen:
            raise ConfigError(f"{pw}: duplicate place name '{name}'.")
        seen.add(name)
        server_size = p.get("server_size")
        places.append(
            PlaceConfig(
                name=name,
                place_id=_as_int(_require(p, "place_id", pw), f"{pw}.place_id"),
                file=(root / str(_require(p, "file", pw))).resolve(),
                display_name=_opt_str(p.get("display_name"), f"{pw}.display_name"),
                description=_opt_str(p.get("description"), f"{pw}.description"),
                server_size=_as_int(server_size, f"{pw}.server_size") if server_size is not None else None,
            )
        )

    seeds = []
    for i, d in enumerate(_tables(env_raw.get("datastores"), f"{where}.datastores")):
        dw = f"{where}.datastores[{i}]"
        _check_keys(d, DATASTORE_KEYS, dw)
        seeds.append(DatastoreSeed(
            name=str(_require(d, "name", dw)),
            file=(root / str(_require(d, "file", dw))).resolve(),
            scope=_opt_str(d.get("scope"), f"{dw}.scope") or None,
        ))

    return Environment(
        name=env_name,
        universe_id=_as_int(_require(env_raw, "universe_id", where), f"{where}.universe_id"),
        places=places,
        version_type=version_type,
        restart_servers=_as_bool(env_raw.get("restart_servers", False), f"{where}.restart_servers"),
        build_command=_opt_str(env_raw.get("build_command", cfg.default_build), f"{where}.build_command"),
        datastores=seeds,
        api_key_env=_opt_str(env_raw.get("api_key_env"), f"{where}.api_key_env") or cfg.default_key_env,
        players=_parse_players(cfg.default_players, env_raw.get("players") or {}, f"{where}.players"),
        playtests_datastore=_parse_playtests(cfg.default_playtests, env_raw.get("playtests") or {}, f"{where}.playtests"),
    )


def _parse_playtests(defaults_raw: dict, env_raw: Any, where: str) -> str:
    if not isinstance(env_raw, dict):
        raise ConfigError(f"{where} must be a section.")
    _check_keys(env_raw, PLAYTESTS_KEYS, where)
    name = env_raw.get("datastore", _expand(defaults_raw, "playtests").get("datastore", DEFAULT_PLAYTESTS_DATASTORE))
    name = _opt_str(name, f"{where}.datastore")
    if not name:
        raise ConfigError(f"{where}.datastore must not be empty.")
    return name


def _parse_players(defaults_raw: dict, env_players: Any, where: str) -> PlayersConfig:
    """[players] section (global, overridden per environment): where DataService /
    ProfileStore keeps the profiles. The environment's part is already expanded."""
    if not isinstance(env_players, dict):
        raise ConfigError(f"{where} must be a section.")
    _check_keys(defaults_raw, PLAYERS_KEYS, "players")
    _check_keys(env_players, PLAYERS_KEYS, where)
    raw = {**_expand(defaults_raw, "players"), **env_players}
    defaults = PlayersConfig()
    datastore = str(raw.get("datastore", defaults.datastore)).strip()
    if not datastore:
        raise ConfigError(f"{where}.datastore cannot be empty.")
    key_prefix = str(raw.get("key_prefix", defaults.key_prefix))
    scope = raw.get("scope", defaults.scope) or None
    if "/" in key_prefix and "scope" not in raw:
        # "global/PLAYER_" (as shown by the Open Cloud list) = global scope + PLAYER_ prefix
        scope, key_prefix = key_prefix.split("/", 1)
    return PlayersConfig(datastore=datastore, key_prefix=key_prefix, scope=scope)
