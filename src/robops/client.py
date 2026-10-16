"""Minimal client for the Roblox Open Cloud API.

Covers what a deployment pipeline needs:
  - publishing a place (.rbxl / .rbxlx)                 -> universe-places:write
  - reading / updating a place's metadata               -> universe.place:write
  - restarting an experience's servers                  -> universe:write
  - standard DataStores (read, overwrite, delete, list, snapshot)

All requests go through `_request`, which handles retries (429 / 5xx)
with exponential backoff and honours the Retry-After header.
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

# Hints displayed along with the most common HTTP errors.
STATUS_HINTS = {
    400: "Invalid request: corrupted place file, malformed JSON or incorrect parameter.",
    401: (
        "API key rejected. Check that it has not expired and that its IP restrictions "
        "allow the current machine (GitHub Actions runners have dynamic IPs: "
        "you must allow 0.0.0.0/0)."
    ),
    403: (
        "Missing permission. Check that the key includes the targeted experience and the "
        "permission required by this operation (scope shown above)."
    ),
    404: "Resource not found: incorrect universe_id, place_id, DataStore or key.",
    409: (
        "Conflict: the place does not belong to this universe, or a Team Create session "
        "is open on the place (it blocks the metadata update)."
    ),
    429: "Rate limit reached (publishing is limited to ~30 requests/minute).",
}


class RobloxApiError(Exception):
    """Error returned by the Open Cloud API."""

    def __init__(self, status: int, message: str, *, scope: str | None = None, body: str = ""):
        self.status = status
        self.message = message
        self.scope = scope
        self.body = body
        super().__init__(self.describe())

    def describe(self) -> str:
        if self.status == 0:
            return f"Unable to reach apis.roblox.com: {self.message}"
        parts = [f"HTTP {self.status}: {self.message}"]
        if self.scope:
            parts.append(f"Required permission: {self.scope}")
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
    raise ValueError(f"Unsupported extension for a place: {path.name} (expected .rbxl or .rbxlx)")


def _entry_segment(key: str, *, for_read: bool = False) -> str:
    """Encodes a DataStore key for the URL.

    If the key contains '@', the API interprets it as a revision identifier;
    when reading we append '@latest' to remove the ambiguity.
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
            raise ValueError("Open Cloud API key missing.")
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
            return response.text.strip()[:500] or response.reason or "unknown error"
        if isinstance(payload, dict):
            for key in ("message", "error", "title"):
                if isinstance(payload.get(key), str) and payload[key]:
                    return payload[key]
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                return str(errors[0].get("message", errors[0]))
        return json.dumps(payload)[:500]

    # ------------------------------------------------------------ Publishing

    def publish_place(
        self,
        universe_id: int,
        place_id: int,
        file_path: str | Path,
        *,
        version_type: str = "Published",
    ) -> PublishResult:
        """Overwrites the place's content with the provided file (new version)."""
        if version_type not in ("Published", "Saved"):
            raise ValueError("version_type must be 'Published' or 'Saved'.")
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
                response.status_code, f"Unexpected response: {response.text[:200]}"
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
            raise ValueError("No field to update.")
        return self._request(
            "PATCH",
            f"/cloud/v2/universes/{universe_id}/places/{place_id}",
            scope="universe.place:write",
            params={"updateMask": ",".join(body)},
            json_body=body,
        ).json()

    # -------------------------------------------------------------- Universe

    def restart_servers(self, universe_id: int) -> None:
        """Restarts the servers running a version older than the latest published one."""
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
        """Overwrites an entry's value (creates it if allow_missing).

        Warning: the API does not do partial updates. If `users` or
        `attributes` are not provided, they are erased on Roblox's side.

        `etag` (read during a GET) protects against concurrent writes:
        if the entry changed since it was read, Roblox refuses the update.
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
            scope="universe-datastores.objects:update (+ :create if the entry does not exist)",
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
        """Names of the experience's DataStores."""
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
        """Snapshot: guarantees a versioned backup of all the data (30 days).

        Limited to one snapshot per UTC day; subsequent calls have no effect.
        """
        return self._request(
            "POST",
            f"/cloud/v2/universes/{universe_id}/data-stores:snapshot",
            scope="universe-datastores.control:snapshot",
            json_body={},
        ).json()
