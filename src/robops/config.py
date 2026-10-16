"""Loading and validation of the robops.toml file.

Strings may contain ${VARIABLE} references resolved from the environment
(handy for keeping IDs in GitHub variables).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .players import PlayersConfig

DEFAULT_CONFIG = "robops.toml"
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
    default_key_env = raw.get("api_key_env", "ROBLOX_API_KEY")
    default_build = (raw.get("build") or {}).get("command")

    envs_raw = raw.get("environments")
    if not isinstance(envs_raw, dict) or not envs_raw:
        raise ConfigError(f"{config_path} must define at least one [environments.<name>] section.")

    return Config(
        path=config_path,
        root=root,
        raw_environments=envs_raw,
        default_key_env=default_key_env,
        default_build=default_build,
        default_players=raw.get("players") or {},
    )


def _parse_environment(cfg: Config, env_name: str, env_raw: dict) -> Environment:
    root = cfg.root
    where = f"environments.{env_name}"
    env_raw = _expand(env_raw, where)

    version_type = env_raw.get("version_type", "Published")
    if version_type not in ("Published", "Saved"):
        raise ConfigError(f"{where}.version_type must be 'Published' or 'Saved'.")

    places_raw = env_raw.get("places") or []
    if not places_raw:
        raise ConfigError(f"{where} must declare at least one [[{where}.places]].")

    places: list[PlaceConfig] = []
    seen: set[str] = set()
    for i, p in enumerate(places_raw):
        pw = f"{where}.places[{i}]"
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
                display_name=p.get("display_name"),
                description=p.get("description"),
                server_size=_as_int(server_size, f"{pw}.server_size") if server_size is not None else None,
            )
        )

    seeds = [
        DatastoreSeed(
            name=str(_require(d, "name", f"{where}.datastores[{i}]")),
            file=(root / str(_require(d, "file", f"{where}.datastores[{i}]"))).resolve(),
            scope=d.get("scope"),
        )
        for i, d in enumerate(env_raw.get("datastores") or [])
    ]

    return Environment(
        name=env_name,
        universe_id=_as_int(_require(env_raw, "universe_id", where), f"{where}.universe_id"),
        places=places,
        version_type=version_type,
        restart_servers=bool(env_raw.get("restart_servers", False)),
        build_command=env_raw.get("build_command", cfg.default_build),
        datastores=seeds,
        api_key_env=env_raw.get("api_key_env", cfg.default_key_env),
        players=_parse_players({**cfg.default_players, **(env_raw.get("players") or {})}, f"{where}.players"),
    )


def _parse_players(raw: dict, where: str) -> PlayersConfig:
    """[players] section: where DataService / ProfileStore keeps the profiles."""
    raw = _expand(raw, where)
    unknown = set(raw) - {"datastore", "key_prefix", "scope"}
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {', '.join(sorted(unknown))} "
                          "(expected: datastore, key_prefix, scope).")
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
