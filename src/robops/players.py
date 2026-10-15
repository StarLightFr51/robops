"""Données joueurs gérées par ProfileStore (DataService de leifstout, ProfileService…).

Chaque joueur est une entrée de DataStore dont la valeur a cette forme :

    {
      "Data": { ...les données du jeu... },
      "MetaData": {
        "ProfileCreateTime": 1700000000,
        "SessionLoadCount": 42,
        "ActiveSession": [placeId, "jobId", "guid"] | null,   <- verrou de session
        "ForceLoadSession": [...] | null,
        "LastUpdate": 1700000123,                             <- dernière sauvegarde (os.time)
        "MetaTags": {}
      },
      "UserIds": [...], "RobloxMetaData": {...}, "GlobalUpdates": [...]
    }

Règles appliquées ici :
  * on n'écrit jamais sur un profil verrouillé par un serveur vivant : il
    écraserait la modification à sa prochaine sauvegarde automatique ;
  * un verrou dont la dernière sauvegarde a plus de ASSUME_DEAD_SECONDS est
    celui d'un serveur planté (ProfileStore le considère mort aussi) ;
  * à l'écriture, on ne remplace que `Data` et on retire le verrou, tout le
    reste (compteurs, GlobalUpdates, UserIds…) est conservé ;
  * l'écriture est conditionnée à l'etag lu juste avant : si le joueur se
    connecte entre la lecture et l'écriture, Roblox refuse la mise à jour ;
  * l'état actuel du profil est copié dans un fichier local avant toute écriture.
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

ASSUME_DEAD_SECONDS = 630      # même seuil que ProfileStore (ASSUME_DEAD)
BACKUP_FORMAT = "robops-players/1"


class PlayerError(Exception):
    pass


@dataclass
class PlayersConfig:
    """Où DataService range les profils (options profileStoreIndex / profileStoreDataPrefix)."""

    datastore: str = "Default"
    key_prefix: str = "PLAYER_"
    scope: str | None = None

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


# ------------------------------------------------------------------ lecture

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
    age_txt = f"dernière sauvegarde il y a {_duration(age)}" if age is not None else "date de sauvegarde inconnue"

    if not session:
        return SessionState("offline", f"hors ligne ({age_txt})")
    where = _session_label(session)
    if age is None:
        # Sans date fiable, on reste prudent : on considère le verrou actif.
        return SessionState("online", f"verrouillé par {where}, {age_txt}")
    if age >= ASSUME_DEAD_SECONDS:
        return SessionState("stale", f"verrou périmé, {where} ne répond plus (arrêté ou planté, {age_txt})")
    return SessionState("online", f"en ligne sur {where}, {age_txt}")


def _session_label(session: Any) -> str:
    if isinstance(session, list) and len(session) >= 2:
        return f"le serveur {session[1]} (place {session[0]})"
    return "un serveur"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} j"


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
    """Copie du profil sans verrou de session (ActiveSession / ForceLoadSession)."""
    result = copy.deepcopy(profile)
    meta = result.setdefault("MetaData", {})
    meta["ActiveSession"] = None
    meta["ForceLoadSession"] = None
    return result


def with_data(current: Any, new_data: Any, fallback_profile: dict | None = None) -> dict:
    """Profil à écrire : le profil actuel avec `Data` remplacé et le verrou retiré.

    Si le joueur n'a plus de profil (ou un profil illisible), on repart du
    profil de secours (celui du backup).
    """
    if is_profile(current):
        base = current
    elif fallback_profile is not None and is_profile(fallback_profile):
        base = fallback_profile
    else:
        raise PlayerError("Aucun profil ProfileStore valide sur lequel écrire.")
    result = unlocked(base)
    result["Data"] = copy.deepcopy(new_data)
    return result


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def apply_edits(data: Any, sets: Iterable[str] = (), unsets: Iterable[str] = ()) -> Any:
    """Applique des modifications `chemin.vers.champ=valeur` sur une copie de Data."""
    result = copy.deepcopy(data) if isinstance(data, dict) else {}
    for item in sets:
        if "=" not in item:
            raise PlayerError(f"--set attend chemin=valeur (reçu : {item!r}).")
        path, raw = item.split("=", 1)
        parts = [p for p in path.strip().split(".") if p]
        if not parts:
            raise PlayerError(f"Chemin vide dans --set {item!r}.")
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
    """Liste lisible des champs de Data qui changent (sur deux niveaux)."""
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
    """Sauvegarde locale du profil avant écriture, pour pouvoir annuler."""
    if not current.exists:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{cfg.key(user_id)}-{stamp}.json"
    n = 2
    while path.exists():  # deux écritures dans la même seconde : ne jamais écraser une copie
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
    """Écrit le profil, conditionné à l'etag lu (refus si le profil a bougé entre-temps)."""
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
                f"Le profil de {user_id} a changé entre la lecture et l'écriture "
                "(le joueur vient peut-être de se connecter). Rien n'a été écrit : relance la commande."
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
    """Exporte tous les profils dans un JSON (ou .json.gz), écrit au fil de l'eau.

    Le fichier final n'apparaît qu'une fois complet (écriture dans un .tmp puis renommage).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    if out_path.suffix == ".gz":
        tmp_path = out_path.with_name(out_path.stem + ".tmp.gz")
    errors: dict[str, str] = {}
    count = 0

    def fetch(key: str) -> tuple[str, dict | None, str | None]:
        try:
            entry = client.get_entry(universe_id, cfg.datastore, key, scope=cfg.scope)
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
    keys = client.list_entry_ids(universe_id, cfg.datastore, scope=cfg.scope, prefix=cfg.key_prefix, limit=limit)
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
    return BackupResult(out_path, count, errors)


def load_backup(path: Path, cfg: PlayersConfig) -> dict[str, dict]:
    """Charge un backup → {user_id: {"value": profil, "users": [...]}}.

    Accepte le format de `robops player backup` et celui de `robops ds export`
    ({"PLAYER_123": profil, ...}).
    """
    path = Path(path)
    if not path.is_file():
        raise PlayerError(f"Backup introuvable : {path}")
    try:
        with _open_text(path, "r") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise PlayerError(f"{path} n'est pas un backup lisible : {exc}") from exc
    if isinstance(raw, dict) and raw.get("format") == BACKUP_FORMAT:
        if raw.get("datastore") != cfg.datastore or raw.get("key_prefix") != cfg.key_prefix:
            raise PlayerError(
                f"Ce backup vient de {raw.get('datastore')}/{raw.get('key_prefix')}* "
                f"mais la cible est {cfg.datastore}/{cfg.key_prefix}* : vérifie la config [players]."
            )
        return raw.get("profiles") or {}
    if isinstance(raw, dict):
        return {cfg.user_id(k): {"value": v, "users": []} for k, v in raw.items() if k.startswith(cfg.key_prefix)}
    raise PlayerError(f"{path} : format de backup non reconnu.")
