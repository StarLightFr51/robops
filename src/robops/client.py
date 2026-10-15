"""Client minimal pour l'API Roblox Open Cloud.

Couvre ce dont un pipeline de déploiement a besoin :
  - publication d'une place (.rbxl / .rbxlx)            -> universe-places:write
  - lecture / mise à jour des métadonnées d'une place   -> universe.place:write
  - redémarrage des serveurs d'une expérience           -> universe:write
  - DataStores standard (lecture, écrasement, suppression, liste, snapshot)

Toutes les requêtes passent par `_request`, qui gère les retries (429 / 5xx)
avec backoff exponentiel et respecte l'en-tête Retry-After.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

from . import __version__

BASE_URL = "https://apis.roblox.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Indications en français affichées avec les erreurs HTTP les plus fréquentes.
STATUS_HINTS = {
    400: "Requête invalide : fichier de place corrompu, JSON mal formé ou paramètre incorrect.",
    401: (
        "Clé API refusée. Vérifie qu'elle n'a pas expiré et que ses restrictions d'IP "
        "autorisent la machine courante (les runners GitHub Actions ont des IP dynamiques : "
        "il faut autoriser 0.0.0.0/0)."
    ),
    403: (
        "Permission manquante. Vérifie que la clé a bien l'expérience ciblée et la "
        "permission requise par cette opération (scope indiqué ci-dessus)."
    ),
    404: "Ressource introuvable : universe_id, place_id, DataStore ou clé incorrect(e).",
    409: (
        "Conflit : la place n'appartient pas à cet univers, ou une session Team Create "
        "est ouverte sur la place (bloque la mise à jour des métadonnées)."
    ),
    429: "Limite de requêtes atteinte (la publication est limitée à ~30 requêtes/minute).",
}


class RobloxApiError(Exception):
    """Erreur renvoyée par l'API Open Cloud."""

    def __init__(self, status: int, message: str, *, scope: str | None = None, body: str = ""):
        self.status = status
        self.message = message
        self.scope = scope
        self.body = body
        super().__init__(self.describe())

    def describe(self) -> str:
        if self.status == 0:
            return f"Impossible de joindre apis.roblox.com : {self.message}"
        parts = [f"HTTP {self.status} : {self.message}"]
        if self.scope:
            parts.append(f"Permission requise : {self.scope}")
        hint = STATUS_HINTS.get(self.status)
        if hint:
            parts.append(hint)
        return "\n  ".join(parts)


@dataclass
class PublishResult:
    universe_id: int
    place_id: int
    version_number: int
    version_type: str


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".rbxl":
        return "application/octet-stream"
    if suffix == ".rbxlx":
        return "application/xml"
    raise ValueError(f"Extension non supportée pour une place : {path.name} (attendu .rbxl ou .rbxlx)")


def _entry_segment(key: str, *, for_read: bool = False) -> str:
    """Encode une clé de DataStore pour l'URL.

    Si la clé contient '@', l'API l'interprète comme un identifiant de révision ;
    en lecture on ajoute '@latest' pour lever l'ambiguïté.
    """
    segment = quote(key, safe="")
    if for_read and "@" in key:
        segment += "@latest"
    return segment


class OpenCloudClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        timeout: float = 120.0,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        if not api_key:
            raise ValueError("Clé API Open Cloud manquante.")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "User-Agent": f"robops/{__version__}"}
        )

    # ------------------------------------------------------------------ HTTP

    def _request(
        self,
        method: str,
        path: str,
        *,
        scope: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise RobloxApiError(0, str(exc), scope=scope) from exc
                self._sleep(self._backoff(attempt, None))
                continue

            if response.status_code in ok_statuses:
                return response

            if response.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue

            raise RobloxApiError(
                response.status_code,
                self._extract_message(response),
                scope=scope,
                body=response.text,
            )

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.5)

    @staticmethod
    def _extract_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text.strip()[:500] or response.reason or "erreur inconnue"
        if isinstance(payload, dict):
            for key in ("message", "error", "title"):
                if isinstance(payload.get(key), str) and payload[key]:
                    return payload[key]
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                return str(errors[0].get("message", errors[0]))
        return json.dumps(payload)[:500]

    # ------------------------------------------------------------ Publication

    def publish_place(
        self,
        universe_id: int,
        place_id: int,
        file_path: str | Path,
        *,
        version_type: str = "Published",
    ) -> PublishResult:
        """Écrase le contenu de la place avec le fichier fourni (nouvelle version)."""
        if version_type not in ("Published", "Saved"):
            raise ValueError("version_type doit valoir 'Published' ou 'Saved'.")
        path = Path(file_path)
        content_type = _content_type_for(path)
        payload = path.read_bytes()
        response = self._request(
            "POST",
            f"/universes/v1/{universe_id}/places/{place_id}/versions",
            scope="universe-places:write",
            params={"versionType": version_type},
            data=payload,
            headers={"Content-Type": content_type},
        )
        try:
            version = int(response.json()["versionNumber"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RobloxApiError(
                response.status_code, f"Réponse inattendue : {response.text[:200]}"
            ) from exc
        return PublishResult(universe_id, place_id, version, version_type)

    # ---------------------------------------------------------------- Places

    def get_place(self, universe_id: int, place_id: int) -> dict:
        return self._request(
            "GET",
            f"/cloud/v2/universes/{universe_id}/places/{place_id}",
            scope="universe.place:read",
        ).json()

    def update_place(
        self,
        universe_id: int,
        place_id: int,
        *,
        display_name: str | None = None,
        description: str | None = None,
        server_size: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if display_name is not None:
            body["displayName"] = display_name
        if description is not None:
            body["description"] = description
        if server_size is not None:
            body["serverSize"] = int(server_size)
        if not body:
            raise ValueError("Aucun champ à mettre à jour.")
        return self._request(
            "PATCH",
            f"/cloud/v2/universes/{universe_id}/places/{place_id}",
            scope="universe.place:write",
            params={"updateMask": ",".join(body)},
            json_body=body,
        ).json()

    # -------------------------------------------------------------- Univers

    def restart_servers(self, universe_id: int) -> None:
        """Redémarre les serveurs dont la version est antérieure à la dernière publiée."""
        self._request(
            "POST",
            f"/cloud/v2/universes/{universe_id}:restartServers",
            scope="universe:write",
            json_body={},
        )

    # ------------------------------------------------------------ DataStores

    @staticmethod
    def _entries_base(universe_id: int, datastore: str, scope: str | None) -> str:
        base = f"/cloud/v2/universes/{universe_id}/data-stores/{quote(datastore, safe='')}"
        if scope:
            base += f"/scopes/{quote(scope, safe='')}"
        return base + "/entries"

    def get_entry(self, universe_id: int, datastore: str, key: str, *, scope: str | None = None) -> dict:
        base = self._entries_base(universe_id, datastore, scope)
        return self._request(
            "GET",
            f"{base}/{_entry_segment(key, for_read=True)}",
            scope="universe-datastores.objects:read",
        ).json()

    def set_entry(
        self,
        universe_id: int,
        datastore: str,
        key: str,
        value: Any,
        *,
        scope: str | None = None,
        users: list[str] | None = None,
        attributes: dict | None = None,
        allow_missing: bool = True,
        etag: str | None = None,
    ) -> dict:
        """Écrase la valeur d'une entrée (la crée si allow_missing).

        Attention : l'API ne fait pas de mise à jour partielle. Si `users` ou
        `attributes` ne sont pas fournis, ils sont effacés côté Roblox.

        `etag` (lu lors d'un GET) protège contre les écritures concurrentes :
        si l'entrée a changé depuis la lecture, Roblox refuse la mise à jour.
        """
        body: dict[str, Any] = {"value": value}
        if users:
            body["users"] = users
        if attributes:
            body["attributes"] = attributes
        if etag:
            body["etag"] = etag
        base = self._entries_base(universe_id, datastore, scope)
        return self._request(
            "PATCH",
            f"{base}/{_entry_segment(key)}",
            scope="universe-datastores.objects:update (+ :create si l'entrée n'existe pas)",
            params={"allowMissing": "true" if allow_missing else "false"},
            json_body=body,
        ).json()

    def delete_entry(self, universe_id: int, datastore: str, key: str, *, scope: str | None = None) -> None:
        base = self._entries_base(universe_id, datastore, scope)
        self._request(
            "DELETE",
            f"{base}/{_entry_segment(key)}",
            scope="universe-datastores.objects:delete",
        )

    def list_entry_ids(
        self,
        universe_id: int,
        datastore: str,
        *,
        scope: str | None = None,
        prefix: str | None = None,
        limit: int | None = None,
    ) -> Iterator[str]:
        base = self._entries_base(universe_id, datastore, scope)
        params: dict[str, Any] = {"maxPageSize": 256}
        if prefix:
            params["filter"] = f'id.startsWith("{prefix}")'
        count = 0
        while True:
            payload = self._request(
                "GET", base, scope="universe-datastores.objects:list", params=params
            ).json()
            for entry in payload.get("dataStoreEntries", []):
                entry_id = entry.get("id") or entry.get("path", "").rsplit("/", 1)[-1]
                yield entry_id
                count += 1
                if limit is not None and count >= limit:
                    return
            token = payload.get("nextPageToken")
            if not token:
                return
            params = {**params, "pageToken": token}

    def list_datastores(self, universe_id: int, *, prefix: str | None = None) -> Iterator[str]:
        """Noms des DataStores de l'expérience."""
        params: dict[str, Any] = {"maxPageSize": 100}
        if prefix:
            params["filter"] = f'id.startsWith("{prefix}")'
        while True:
            payload = self._request(
                "GET", f"/cloud/v2/universes/{universe_id}/data-stores",
                scope="universe-datastores.control:list", params=params,
            ).json()
            for store in payload.get("dataStores", []):
                yield store.get("id") or store.get("path", "").rsplit("/", 1)[-1]
            token = payload.get("nextPageToken")
            if not token:
                return
            params = {**params, "pageToken": token}

    def snapshot_datastores(self, universe_id: int) -> dict:
        """Snapshot : garantit une sauvegarde versionnée de toutes les données (30 jours).

        Limité à un snapshot par jour UTC ; les appels suivants sont sans effet.
        """
        return self._request(
            "POST",
            f"/cloud/v2/universes/{universe_id}/data-stores:snapshot",
            scope="universe-datastores.control:snapshot",
            json_body={},
        ).json()
