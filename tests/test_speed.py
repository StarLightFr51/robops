"""What makes the dashboard fast: kept connections, conditional GitHub requests, nothing asked twice."""

import threading

import pytest

from robops import cli
from robops.github import GitHub

from dashboard_fakes import FakeCloud, FakeGitHub, make_dashboard


class Response:
    def __init__(self, status, payload=None, etag=None):
        self.status_code, self._payload = status, payload
        self.headers = {"ETag": etag} if etag else {}

    def json(self):
        return self._payload


class EtagServer:
    """Answers like GitHub: 304 when If-None-Match is the current ETag."""

    def __init__(self):
        self.version, self.sent = 1, []

    def request(self, method, url, timeout=None, headers=None, **kwargs):
        self.sent.append((method, url, dict(headers or {})))
        etag = f'W/"v{self.version}"'
        if method == "GET" and (headers or {}).get("If-None-Match") == etag:
            return Response(304)
        return Response(200, {"workflow_runs": [{"id": self.version}]}, etag)


def test_github_sends_the_etag_back_and_reuses_the_answer():
    server = EtagServer()
    gh = GitHub("me/game", server)                       # type: ignore[arg-type]
    assert gh.runs("deploy.yml") == [{"id": 1}]
    first = gh.runs("deploy.yml")
    assert first == [{"id": 1}] and server.sent[1][2] == {"If-None-Match": 'W/"v1"'}
    first[0]["id"] = 99                                  # the cached answer is a copy
    assert gh.runs("deploy.yml") == [{"id": 1}]
    server.version = 2                                   # something changed: a full answer again
    assert gh.runs("deploy.yml") == [{"id": 2}]
    assert gh.runs("other.yml") == [{"id": 2}] and "If-None-Match" not in server.sent[-1][2]   # per URL


def test_github_cache_is_bounded(monkeypatch):
    monkeypatch.setattr("robops.github.MAX_CACHED", 3)
    gh = GitHub("me/game", EtagServer())                 # type: ignore[arg-type]
    for i in range(10):
        gh.runs(f"w{i}.yml")
    assert len(gh._etags) == 3


class CountingGitHub(FakeGitHub):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[str] = []

    def info(self):
        self.calls.append("info")
        return super().info()

    def workflows(self):
        self.calls.append("workflows")
        return super().workflows()

    def jobs(self, run_id):
        self.calls.append(f"jobs {run_id}")
        return super().jobs(run_id)


def test_deployments_ask_only_what_can_have_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    gh = CountingGitHub()
    made = []
    app = make_dashboard(tmp_path, FakeCloud(), gh)
    app.github_factory = lambda repo, token: made.append(repo) or gh   # type: ignore[assignment,func-returns-value]
    first = app.deployments()
    assert sorted(gh.calls) == ["info", "jobs 6", "jobs 7", "jobs 8", "jobs 9", "workflows"]
    gh.calls.clear()
    again = app.deployments()
    assert gh.calls == ["jobs 9"]                        # only the run still in progress
    assert again == first and len(made) == 1             # same answer, same GitHub client

    monkeypatch.setattr("robops.dashboard.WORKFLOWS_TTL", 0)
    gh.calls.clear()
    app.deployments()
    assert sorted(gh.calls) == ["info", "jobs 9", "workflows"]
    gh.calls.clear()
    app.github_status()                                  # Setup always checks for real
    assert sorted(gh.calls) == ["info", "workflows"]


def test_a_workflow_added_meanwhile_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    gh = CountingGitHub(workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}])
    app = make_dashboard(tmp_path, FakeCloud(), gh)
    with pytest.raises(Exception, match="No deploy workflow"):
        app.deployments()
    gh.workflow_list = gh.workflow_list + [{"name": "Roblox Deploy", "path": ".github/workflows/roblox-deploy.yml"}]
    assert app.deployments()["workflow"]["file"] == "roblox-deploy.yml"   # cached list, then asked again


class CountingCloud(FakeCloud):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.stores["RobopsPlaytests"] = {
            f"playtest-2026-11-0{i}T10:00:00Z": {"format": "robops-playtest/1", "title": f"Run {i}",
                                                  "players": [{"name": "Sim_1", "outcome": "completed"}]}
            for i in range(1, 4)}

    def get_entry(self, *args, **kwargs):
        self.reads += 1
        return super().get_entry(*args, **kwargs)


def test_reports_and_clients_are_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    cloud = CountingCloud()
    keys = []
    app = make_dashboard(tmp_path, cloud)
    app.client_factory = lambda key: keys.append(key) or cloud   # type: ignore[assignment,func-returns-value]
    assert [r["title"] for r in app.playtests("production")["reports"]] == ["Run 3", "Run 2", "Run 1"]
    assert cloud.reads == 3
    app.playtests("production")
    app.playtest("production", "playtest-2026-11-03T10:00:00Z")
    assert cloud.reads == 3 and keys == ["k"]            # nothing read twice, one client for the key
    monkeypatch.setenv("ROBLOX_API_KEY", "other")
    app.live("production")
    assert keys == ["k", "other"]                        # a new key gets its own client


CHECK_CONFIG = """
[environments.production]
universe_id = 1
""" + "".join(f"""
[[environments.production.places]]
name = "{name}"
place_id = {pid}
file = "game.rbxl"
""" for name, pid in (("lobby", 111), ("arena", 222), ("shop", 333)))


def test_check_online_asks_every_place_at_once_and_keeps_their_order(tmp_path, monkeypatch, capsys):
    from robops.client import RobloxApiError

    (tmp_path / "robops.toml").write_text(CHECK_CONFIG, encoding="utf-8")
    (tmp_path / "game.rbxl").write_bytes(b"PLACE")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    barrier = threading.Barrier(3, timeout=5)            # passes only if the 3 places are asked together

    class Client:
        def get_place(self, universe_id, place_id):
            barrier.wait()
            if place_id == 222:
                raise RobloxApiError(403, "forbidden")
            return {"displayName": f"Place {place_id}"}

    monkeypatch.setattr(cli, "_client", lambda env_name: Client())
    assert cli.main(["check", "--online"]) == 1
    text = capsys.readouterr()
    assert [line.split()[1] for line in text.out.splitlines() if "reachable" in line] == ["lobby", "shop"]
    assert "arena: " in text.err and "forbidden" in text.err
