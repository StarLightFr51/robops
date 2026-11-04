import pytest

from robops.roblox_web import RobloxWeb, WebApiError

from conftest import FakeResponse


class Session:
    def __init__(self, routes):
        self.headers = {}
        self.routes = routes
        self.calls = []

    def _answer(self, url, params):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(404, {})

    def get(self, url, params=None, timeout=None):
        return self._answer(url, params)

    def post(self, url, json=None, timeout=None):
        return self._answer(url, json)


GAME = {"data": [{"id": 1, "rootPlaceId": 42, "name": "Brick Tycoon", "playing": 187, "visits": 1000,
                  "favoritedCount": 50, "maxPlayers": 24, "created": "2025-01-01T00:00:00Z",
                  "updated": "2026-11-04T00:00:00Z"}]}


def test_game_merges_stats_and_votes():
    web = RobloxWeb(Session({"/v1/games/votes": FakeResponse(200, {"data": [{"id": 1, "upVotes": 90, "downVotes": 10}]}),
                             "/v1/games": FakeResponse(200, GAME)}))
    game = web.game(1)
    assert game["playing"] == 187 and game["favorites"] == 50 and game["root_place_id"] == 42
    assert (game["up_votes"], game["down_votes"]) == (90, 10)


def test_game_without_votes_and_unknown_universe():
    web = RobloxWeb(Session({"/v1/games/votes": FakeResponse(500, "down"), "/v1/games": FakeResponse(200, GAME)}))
    assert web.game(1)["up_votes"] is None                 # votes are optional
    with pytest.raises(WebApiError, match="not found"):
        RobloxWeb(Session({"/v1/games": FakeResponse(200, {"data": []})})).game(9)
    with pytest.raises(WebApiError, match="HTTP 503"):
        RobloxWeb(Session({"/v1/games": FakeResponse(503, "x")})).game(9)


def test_servers_users_and_headshot():
    web = RobloxWeb(Session({
        "/servers/Public": FakeResponse(200, {"data": [{"id": "a", "playing": 3, "maxPlayers": 24, "fps": 60, "ping": 90}]}),
        "/v1/usernames/users": FakeResponse(200, {"data": [{"id": 156, "name": "brickfan"}]}),
        "/v1/users/156": FakeResponse(200, {"id": 156, "name": "brickfan", "displayName": "Brick Fan",
                                            "created": "2019-01-01T00:00:00Z", "isBanned": False}),
        "avatar-headshot": FakeResponse(200, {"data": [{"imageUrl": "https://tr.rbxcdn.com/abc/150/150/Png"}]}),
    }))
    assert web.servers(42) == [{"id": "a", "playing": 3, "max_players": 24, "fps": 60, "ping": 90}]
    assert web.user_id("brickfan") == 156
    assert web.user(156)["display_name"] == "Brick Fan"
    assert web.headshot(156).startswith("https://tr.rbxcdn.com/")


def test_headshot_ignores_non_https_urls():
    web = RobloxWeb(Session({"avatar-headshot": FakeResponse(200, {"data": [{"imageUrl": "javascript:alert(1)"}]})}))
    assert web.headshot(1) is None
