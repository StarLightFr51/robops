"""Player data managed by ProfileStore (leifstout's DataService, ProfileService…).

Each player is a DataStore entry whose value has this shape:

    {
      "Data": { ...the game data... },
      "MetaData": {
        "ProfileCreateTime": 1700000000,
        "SessionLoadCount": 42,
        "ActiveSession": [placeId, "jobId", "guid"] | null,   <- session lock
        "ForceLoadSession": [...] | null,
        "LastUpdate": 1700000123,                             <- last save (os.time)
        "MetaTags": {}
      },
      "UserIds": [...], "RobloxMetaData": {...}, "GlobalUpdates": [...]
    }

Rules applied here:
  * we never write to a profile locked by a live server: it would overwrite
    the change on its next autosave;
  * a lock whose last save is older than ASSUME_DEAD_SECONDS belongs to a
    crashed server (ProfileStore considers it dead too);
  * when writing, only `Data` is replaced and the lock is removed; everything
    else (counters, GlobalUpdates, UserIds…) is kept;
  * the write is conditioned on the etag read just before: if the player
    joins between the read and the write, Roblox refuses the update;
  * the profile's current state is copied to a local file before any write.
"""

from __future__ import annotations

import copy
import gzip
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .client import OpenCloudClient, RobloxApiError

ASSUME_DEAD_SECONDS = 630      # same threshold as ProfileStore (ASSUME_DEAD)
BACKUP_FORMAT = "robops-players/1"


class PlayerError(Exception):
    pass


@dataclass
class PlayersConfig:
    """Where DataService keeps the profiles (profileStoreIndex / profileStoreDataPrefix options).

    ProfileStore opens its DataStores without a scope (GetDataStore(name, nil)): profiles
    are therefore in Roblox's default scope, "global". We target it explicitly,
    because an access without a scope through Open Cloud does not always find these entries.
    """

    datastore: str = "Default"
    key_prefix: str = "PLAYER_"
    scope: str | None = "global"

    def key(self, user_id: str | int) -> str:
        return f"{self.key_prefix}{user_id}"

    def user_id(self, key: str) -> str:
        return key[len(self.key_prefix):] if key.startswith(self.key_prefix) else key


@dataclass
class SessionState:
    status: str            # "offline" | "online" | "stale"
    detail: str

    @property
    def writable(self) -> bool:
        return self.status != "online"


# ------------------------------------------------------------------ reading

def is_profile(value: Any) -> bool:
    return isinstance(value, dict) and "Data" in value and isinstance(value.get("MetaData"), dict)


def session_state(profile: dict, now: float | None = None) -> SessionState:
    now = time.time() if now is None else now
    meta = profile.get("MetaData") or {}
    session = meta.get("ActiveSession")
    last = meta.get("LastUpdate")
    age = None
    if isinstance(last, (int, float)) and last > 0:
        age = max(0, int(now - last))
    age_txt = f"last save {_duration(age)} ago" if age is not None else "last save time unknown"

    if not session:
        return SessionState("offline", f"offline ({age_txt})")
    where = _session_label(session)
    if age is None:
        # Without a reliable date we stay cautious: we treat the lock as active.
        return SessionState("online", f"locked by {where}, {age_txt}")
    if age >= ASSUME_DEAD_SECONDS:
        return SessionState("stale", f"stale lock, {where} is no longer responding (stopped or crashed, {age_txt})")
    return SessionState("online", f"online on {where}, {age_txt}")


def _session_label(session: Any) -> str:
    if isinstance(session, list) and len(session) >= 2:
        return f"server {session[1]} (place {session[0]})"
    return "a server"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} d"


@dataclass
class CurrentProfile:
    exists: bool
    value: Any = None
    etag: str | None = None
    users: list[str] | None = None
    attributes: dict | None = None


def fetch_profile(client: OpenCloudClient, universe_id: int, cfg: PlayersConfig, user_id: str) -> CurrentProfile:
    try:
        entry = client.get_entry(universe_id, cfg.datastore, cfg.key(user_id), scope=cfg.scope)
    except RobloxApiError as exc:
        if exc.status == 404:
            return CurrentProfile(exists=False)
        raise
    return CurrentProfile(
        exists=True,
        value=entry.get("value"),
        etag=entry.get("etag"),
        users=entry.get("users") or None,
        attributes=entry.get("attributes") or None,
    )


# ------------------------------------------------------------- modification

def unlocked(profile: dict) -> dict:
    """Copy of the profile without the session lock (ActiveSession / ForceLoadSession)."""
    result = copy.deepcopy(profile)
    meta = result.setdefault("MetaData", {})
    meta["ActiveSession"] = None
    meta["ForceLoadSession"] = None
    return result


def with_data(current: Any, new_data: Any, fallback_profile: dict | None = None) -> dict:
    """Profile to write: the current profile with `Data` replaced and the lock removed.

    If the player no longer has a profile (or has an unreadable one), we start
    from the fallback profile (the one from the backup).
    """
    if is_profile(current):
        base = current
    elif fallback_profile is not None and is_profile(fallback_profile):
        base = fallback_profile
    else:
        raise PlayerError("No valid ProfileStore profile to write to.")
    result = unlocked(base)
    result["Data"] = copy.deepcopy(new_data)
    return result


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def apply_edits(data: Any, sets: Iterable[str] = (), unsets: Iterable[str] = ()) -> Any:
    """Applies `path.to.field=value` edits to a copy of Data."""
    result = copy.deepcopy(data) if isinstance(data, dict) else {}
    for item in sets:
        if "=" not in item:
            raise PlayerError(f"--set expects path=value (got: {item!r}).")
        path, raw = item.split("=", 1)
        parts = [p for p in path.strip().split(".") if p]
        if not parts:
            raise PlayerError(f"Empty path in --set {item!r}.")
        node = result
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = parse_value(raw)
    for path in unsets:
        parts = [p for p in path.strip().split(".") if p]
        node = result
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return result


def changed_fields(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Readable list of the Data fields that change (two levels deep)."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [prefix or "Data"] if before != after else []
    changes: list[str] = []
    for key in sorted(set(before) | set(after), key=str):
        name = f"{prefix}{key}"
        if key not in before:
            changes.append(f"+ {name}")
        elif key not in after:
            changes.append(f"- {name}")
        elif before[key] != after[key]:
            if isinstance(before[key], dict) and isinstance(after[key], dict) and not prefix:
                changes.extend(changed_fields(before[key], after[key], f"{name}."))
            else:
                changes.append(f"~ {name}")
    return changes


def save_safety_copy(directory: Path, cfg: PlayersConfig, user_id: str, current: CurrentProfile) -> Path | None:
    """Local copy of the profile before writing, so the change can be undone."""
    if not current.exists:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{cfg.key(user_id)}-{stamp}.json"
    n = 2
    while path.exists():  # two writes in the same second: never overwrite a copy
        path = directory / f"{cfg.key(user_id)}-{stamp}-{n}.json"
        n += 1
    path.write_text(
        json.dumps({"key": cfg.key(user_id), "value": current.value, "users": current.users},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_profile(
    client: OpenCloudClient,
    universe_id: int,
    cfg: PlayersConfig,
    user_id: str,
    profile: dict,
    current: CurrentProfile,
    *,
    users: list[str] | None = None,
) -> dict:
    """Writes the profile, conditioned on the etag that was read (refused if the profile moved in the meantime)."""
    try:
        return client.set_entry(
            universe_id,
            cfg.datastore,
            cfg.key(user_id),
            profile,
            scope=cfg.scope,
            users=current.users or users,
            attributes=current.attributes,
            etag=current.etag,
        )
    except RobloxApiError as exc:
        if exc.status in (409, 412) or (exc.status == 400 and "etag" in exc.message.lower()):
            raise PlayerError(
                f"The profile of {user_id} changed between the read and the write "
                "(the player may have just joined). Nothing was written: run the command again."
            ) from exc
        raise


# ------------------------------------------------------------------- backup

def _open_text(path: Path, mode: str):
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def _chunks(items: Iterator[str], size: int) -> Iterator[list[str]]:
    chunk: list[str] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


@dataclass
class BackupResult:
    path: Path
    count: int
    errors: dict[str, str]
    other_keys: int = 0                 # DataStore keys that don't start with the prefix
    other_examples: list[str] | None = None


def split_scoped(raw_id: str, scope: str | None) -> tuple[str | None, str]:
    """Splits an identifier returned by the Open Cloud list into (scope, key).

    Depending on the request, the API returns "PLAYER_123" or "global/PLAYER_123"
    (scope-prefixed). A key containing "/" is only split if the part before
    the "/" matches the requested scope, or if no scope was requested.
    """
    if "/" in raw_id:
        head, tail = raw_id.split("/", 1)
        if scope is None or head == scope:
            return head, tail
    return scope, raw_id


def guess_prefix(keys: Iterable[str]) -> str | None:
    """Guesses the key prefix of the form <prefix><UserId> (e.g. 'Player_123' → 'Player_')."""
    counts: dict[str, int] = {}
    for raw in keys:
        key = split_scoped(raw, None)[1]
        stripped = key.rstrip("0123456789")
        if stripped != key:                      # the key does end with a number
            counts[stripped] = counts.get(stripped, 0) + 1
    return max(counts, key=counts.get) if counts else None


def backup_players(
    client: OpenCloudClient,
    universe_id: int,
    cfg: PlayersConfig,
    out_path: Path,
    *,
    limit: int | None = None,
    workers: int = 4,
    progress: Callable[[int], None] | None = None,
) -> BackupResult:
    """Exports all the profiles to a JSON (or .json.gz), written as it goes.

    The final file only appears once complete (written to a .tmp then renamed).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    if out_path.suffix == ".gz":
        tmp_path = out_path.with_name(out_path.stem + ".tmp.gz")
    errors: dict[str, str] = {}
    count = 0

    def fetch(item: tuple[str | None, str]) -> tuple[str, dict | None, str | None]:
        scope, key = item
        try:
            entry = client.get_entry(universe_id, cfg.datastore, key, scope=scope)
            return key, {"value": entry.get("value"), "users": entry.get("users") or []}, None
        except RobloxApiError as exc:
            return key, None, exc.describe().splitlines()[0]

    header = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_id": universe_id,
        "datastore": cfg.datastore,
        "key_prefix": cfg.key_prefix,
        "scope": cfg.scope,
    }
    # The prefix is filtered by robops (not by the API): this way we can report
    # the keys that don't match, to help fix the configuration.
    others: list[str] = []
    other_count = 0

    def matching_keys() -> Iterator[tuple[str | None, str]]:
        nonlocal other_count
        taken = 0
        for raw in client.list_entry_ids(universe_id, cfg.datastore, scope=cfg.scope):
            scope, key = split_scoped(raw, cfg.scope)
            if not key.startswith(cfg.key_prefix):
                other_count += 1
                if len(others) < 5:
                    others.append(raw)
                continue
            yield scope, key
            taken += 1
            if limit is not None and taken >= limit:
                return

    keys = matching_keys()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool, _open_text(tmp_path, "w") as fh:
        fh.write(json.dumps(header, ensure_ascii=False)[:-1] + ', "profiles": {')
        first = True
        for chunk in _chunks(keys, 64):
            for key, record, error in pool.map(fetch, chunk):
                if error:
                    errors[cfg.user_id(key)] = error
                    continue
                fh.write(("" if first else ",") + "\n  " + json.dumps(cfg.user_id(key)) + ": "
                         + json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                first = False
                count += 1
            if progress:
                progress(count)
        fh.write("\n}, " + f'"count": {count}, "errors": ' + json.dumps(errors, ensure_ascii=False) + "}\n")
    os.replace(tmp_path, out_path)
    return BackupResult(out_path, count, errors, other_count, others)


def load_backup(path: Path, cfg: PlayersConfig) -> dict[str, dict]:
    """Loads a backup → {user_id: {"value": profile, "users": [...]}}.

    Accepts the format of `robops player backup` and that of `robops ds export`
    ({"PLAYER_123": profile, ...}).
    """
    path = Path(path)
    if not path.is_file():
        raise PlayerError(f"Backup not found: {path}")
    try:
        with _open_text(path, "r") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise PlayerError(f"{path} is not a readable backup: {exc}") from exc
    if isinstance(raw, dict) and raw.get("format") == BACKUP_FORMAT:
        if raw.get("datastore") != cfg.datastore or raw.get("key_prefix") != cfg.key_prefix:
            raise PlayerError(
                f"This backup comes from {raw.get('datastore')}/{raw.get('key_prefix')}* "
                f"but the target is {cfg.datastore}/{cfg.key_prefix}*: check the [players] config."
            )
        return raw.get("profiles") or {}
    if isinstance(raw, dict):
        return {cfg.user_id(k): {"value": v, "users": []} for k, v in raw.items() if k.startswith(cfg.key_prefix)}
    raise PlayerError(f"{path}: unrecognized backup format.")
