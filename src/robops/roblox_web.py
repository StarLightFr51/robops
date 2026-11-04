"""Public Roblox web APIs (no API key): live stats, servers, users.

These are the legacy endpoints of the Roblox website (games.roblox.com,
users.roblox.com, thumbnails.roblox.com). They need no key, only show public
data, and are used read-only by the dashboard. Note: for a multi-place
experience, "playing" only counts the players of the root place.
"""

from __future__ import annotations

from typing import Any

import requests

from . import __version__


class WebApiError(Exception):
    pass


class RobloxWeb:
    def __init__(self, session: requests.Session | None = None, timeout: float = 10):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", f"robops/{__version__}")
        self.timeout = timeout

    def _get(self, url: str, **params: Any) -> Any:
        try:
            resp = self.session.get(url, params=params or None, timeout=self.timeout)
        except requests.RequestException as exc:
            raise WebApiError(f"Unable to reach {url.split('/')[2]}: {exc}") from exc
        if resp.status_code != 200:
            raise WebApiError(f"{url.split('/')[2]} answered HTTP {resp.status_code}")
        return resp.json()

    def game(self, universe_id: int) -> dict:
        """Public stats: name, playing, visits, favorites, likes/dislikes, max players, dates."""
        games = self._get("https://games.roblox.com/v1/games", universeIds=universe_id).get("data") or []
        if not games:
            raise WebApiError(f"Universe {universe_id} not found on roblox.com (private or wrong ID?).")
        game = games[0]
        result = {
            "name": game.get("name"),
            "root_place_id": game.get("rootPlaceId"),
            "playing": game.get("playing"),
            "visits": game.get("visits"),
            "favorites": game.get("favoritedCount"),
            "max_players": game.get("maxPlayers"),
            "created": game.get("created"),
            "updated": game.get("updated"),
            "up_votes": None,
            "down_votes": None,
        }
        try:
            votes = self._get("https://games.roblox.com/v1/games/votes", universeIds=universe_id).get("data") or []
            if votes:
                result["up_votes"] = votes[0].get("upVotes")
                result["down_votes"] = votes[0].get("downVotes")
        except WebApiError:
            pass                         # votes are a bonus: never hide the rest for them
        return result

    def servers(self, place_id: int, *, limit: int = 100) -> list[dict]:
        """Public servers of a place (players, max, fps, ping)."""
        payload = self._get(f"https://games.roblox.com/v1/games/{place_id}/servers/Public",
                            limit=limit, sortOrder="Desc")
        return [
            {"id": s.get("id"), "playing": s.get("playing"), "max_players": s.get("maxPlayers"),
             "fps": s.get("fps"), "ping": s.get("ping")}
            for s in payload.get("data") or []
        ]

    def user(self, user_id: int) -> dict:
        data = self._get(f"https://users.roblox.com/v1/users/{user_id}")
        return {"id": data.get("id"), "name": data.get("name"), "display_name": data.get("displayName"),
                "created": data.get("created"), "banned": data.get("isBanned")}

    def user_id(self, username: str) -> int | None:
        try:
            resp = self.session.post("https://users.roblox.com/v1/usernames/users",
                                     json={"usernames": [username], "excludeBannedUsers": False},
                                     timeout=self.timeout)
        except requests.RequestException as exc:
            raise WebApiError(f"Unable to reach users.roblox.com: {exc}") from exc
        if resp.status_code != 200:
            raise WebApiError(f"users.roblox.com answered HTTP {resp.status_code}")
        data = resp.json().get("data") or []
        return int(data[0]["id"]) if data else None

    def headshot(self, user_id: int) -> str | None:
        try:
            data = self._get("https://thumbnails.roblox.com/v1/users/avatar-headshot",
                             userIds=user_id, size="150x150", format="Png", isCircular="false").get("data") or []
        except WebApiError:
            return None
        url = data[0].get("imageUrl") if data else None
        return url if isinstance(url, str) and url.startswith("https://") else None
