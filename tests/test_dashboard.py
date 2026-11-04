import json
import threading

import pytest
import requests

from robops import cli, dashboard
from robops.github import GitHub, repo_from_remote

from dashboard_fakes import FakeCloud, FakeGitHub, make_dashboard, profile


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Real HTTP server on a free port, backed by fakes."""
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    cloud, gh = FakeCloud(), FakeGitHub()
    app = make_dashboard(tmp_path, cloud, gh)
    ready = threading.Event()
    info = {}

    def on_ready(url, srv):
        info.update(url=url, server=srv)
        ready.set()

    thread = threading.Thread(target=dashboard.serve, daemon=True,
                              kwargs={"app": app, "port": 0, "open_browser": False, "ready": on_ready})
    thread.start()
    assert ready.wait(5)
    base, token = info["url"].split("/#token=")

    class Client:
        def __init__(self):
            self.base, self.token, self.cloud, self.gh, self.root = base, token, cloud, gh, tmp_path

        def get(self, path, **kw):
            headers = {"X-Robops-Token": self.token, **kw.pop("headers", {})}
            return requests.get(self.base + path, headers=headers, timeout=5, **kw)

        def post(self, path, body, **kw):
            headers = {"X-Robops-Token": self.token, **kw.pop("headers", {})}
            return requests.post(self.base + path, json=body, headers=headers, timeout=5, **kw)

    yield Client()
    info["server"].shutdown()
    thread.join(5)


# ------------------------------------------------------------------ security

def test_page_is_served_with_strict_headers(server):
    resp = requests.get(server.base + "/", timeout=5)
    assert resp.status_code == 200 and "robops dashboard" in resp.text
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["Cache-Control"] == "no-store"
    font = requests.get(server.base + "/fonts/atkinson-hyperlegible-latin-400-normal.woff2", timeout=5)
    assert font.status_code == 200 and font.headers["Content-Type"] == "font/woff2"
    # Raw path (requests would normalise "..")
    import http.client
    conn = http.client.HTTPConnection(server.base.removeprefix("http://"), timeout=5)
    conn.request("GET", "/fonts/../../pyproject.toml")
    assert conn.getresponse().status == 404


def test_api_requires_the_session_token(server):
    assert requests.get(server.base + "/api/summary", timeout=5).status_code == 401
    assert server.get("/api/summary", headers={"X-Robops-Token": "wrong"}).status_code == 401
    assert server.get("/api/summary").status_code == 200


def test_foreign_host_and_origin_are_refused(server):
    port = server.base.rsplit(":", 1)[1]
    # DNS rebinding: evil.example resolving to 127.0.0.1
    assert server.get("/api/summary", headers={"Host": f"evil.example:{port}"}).status_code == 403
    assert requests.get(server.base + "/", headers={"Host": "evil.example"}, timeout=5).status_code == 403
    # A page from another site posting to the dashboard
    resp = server.post("/api/env/staging/restart", {"confirm": "staging"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert server.cloud.calls == []


def test_post_requires_json(server):
    resp = requests.post(server.base + "/api/env/staging/restart", data="confirm=staging",
                         headers={"X-Robops-Token": server.token, "Content-Type": "application/x-www-form-urlencoded"},
                         timeout=5)
    assert resp.status_code == 415


# ------------------------------------------------------------------- reading

def test_summary_lists_environments_and_findings(server):
    data = server.get("/api/summary").json()
    names = [e["name"] for e in data["environments"]]
    assert names == ["staging", "production"]
    prod = data["environments"][1]
    assert prod["protected"] is True and data["environments"][0]["protected"] is False
    assert prod["universe_id"] == 333 and prod["key_set"] is True
    assert any(f["level"] == "ok" for f in prod["findings"])
    assert data["github"] == {"repo": "me/brick-tycoon", "token": True}


def test_live_combines_open_cloud_and_public_stats(server):
    data = server.get("/api/env/production/live").json()
    assert data["universe"]["ok"]["displayName"] == "Brick Tycoon"
    assert data["game"]["ok"]["playing"] == 187
    assert data["places"][0]["info"]["ok"]["serverSize"] == 24
    assert len(data["places"][0]["servers"]["ok"]) == 12


def test_live_without_key_still_shows_public_stats(server, monkeypatch):
    monkeypatch.delenv("ROBLOX_API_KEY")
    data = server.get("/api/env/production/live").json()
    assert "ROBLOX_API_KEY is not set" in data["universe"]["error"]
    assert data["game"]["ok"]["visits"] == 1_284_310


def test_deployments_know_the_target_environment(server):
    runs = server.get("/api/deployments").json()["runs"]
    assert [(r["number"], r["kind"], r["environment"]) for r in runs] == [
        (9, "publish", "production"), (8, "publish", "staging"), (7, "publish", "production"), (6, "verify", None)]
    assert runs[0]["status"] == "in_progress" and runs[2]["conclusion"] == "failure"


def test_backups_list_local_files_and_github_artifacts(server):
    data = server.get("/api/backups").json()
    assert [b["id"] for b in data["local"]] == ["backups/players-2026-11-03.json.gz"]
    assert [a["name"] for a in data["github"]["ok"]] == ["players-production-2026-11-04_0217Z.json.gz.age"]


def test_player_lookup_by_username_and_id(server):
    by_name = server.get("/api/env/production/player", params={"q": "brickfan"}).json()
    assert by_name["user_id"] == "156" and by_name["data"]["coins"] == 1250
    assert by_name["state"]["status"] == "offline" and by_name["user"]["ok"]["name"] == "brickfan"
    online = server.get("/api/env/production/player", params={"q": "261"}).json()
    assert online["state"]["writable"] is False
    missing = server.get("/api/env/production/player", params={"q": "nobody"})
    assert missing.status_code == 404


def test_datastore_browsing(server):
    assert server.get("/api/env/staging/datastores").json()["stores"] == ["Default", "GameConfig"]
    assert server.get("/api/env/staging/entries", params={"datastore": "GameConfig"}).json() == {
        "ids": ["event", "motd"], "next": None}
    entry = server.get("/api/env/staging/entry", params={"datastore": "GameConfig", "key": "motd"}).json()
    assert entry["value"] == "Double XP this weekend!"


# ------------------------------------------------------------------- actions

def test_every_action_requires_the_environment_name(server):
    for path, body in [("restart", {}), ("snapshot", {}), ("deploy", {}),
                       ("entry/set", {"datastore": "GameConfig", "key": "motd", "value": 1}),
                       ("entry/delete", {"datastore": "GameConfig", "key": "motd"})]:
        resp = server.post(f"/api/env/production/{path}", {**body, "confirm": "staging"})
        assert resp.status_code == 400 and "type the environment name" in resp.json()["error"]
    assert server.cloud.calls == [] and server.gh.dispatched == []


def test_restart_snapshot_and_audit_log(server):
    assert server.post("/api/env/staging/restart", {"confirm": "staging"}).status_code == 200
    assert server.post("/api/env/staging/snapshot", {"confirm": "staging"}).json()["message"] == "Snapshot created."
    assert server.cloud.calls == [("restart", 111), ("snapshot", 111)]
    log = (server.root / ".robops" / "audit.log").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["action"] for line in log] == ["servers.restart", "datastores.snapshot"]


def test_deploy_dispatches_the_workflow(server):
    resp = server.post("/api/env/production/deploy", {"confirm": "production", "version_type": "Saved",
                                                      "with_datastores": True, "restart": "no"})
    assert resp.status_code == 200
    assert server.gh.dispatched == [("roblox-deploy.yml", "main", {
        "environment": "production", "version_type": "Saved", "with_datastores": "true", "restart": "no"})]
    assert server.post("/api/backups/run", {}).status_code == 200
    assert server.gh.dispatched[-1] == ("roblox-backup.yml", "main", {})


def test_datastore_set_and_delete(server):
    resp = server.post("/api/env/staging/entry/set", {"confirm": "staging", "datastore": "GameConfig",
                                                      "key": "motd", "value": "Hello"})
    assert resp.status_code == 200
    assert server.cloud.stores["GameConfig"]["motd"] == "Hello"
    resp = server.post("/api/env/staging/entry/delete", {"confirm": "staging", "datastore": "GameConfig", "key": "motd"})
    assert resp.status_code == 200 and "motd" not in server.cloud.stores["GameConfig"]


def test_player_edit_previews_then_writes_with_safety_copy(server):
    body = {"user_id": "156", "data": {"coins": 9999, "level": 14, "pets": ["cat", "dragon"]}}
    preview = server.post("/api/env/production/player/edit", {**body, "preview": True}).json()
    assert preview == {"changes": ["~ coins"], "written": False}
    assert server.cloud.calls == []                                # preview never writes

    assert server.post("/api/env/production/player/edit", body).status_code == 400   # no confirmation
    done = server.post("/api/env/production/player/edit", {**body, "confirm": "production"}).json()
    assert done["written"] is True
    saved = server.cloud.stores["Default"]["PLAYER_156"]
    assert saved["Data"]["coins"] == 9999 and saved["MetaData"]["SessionLoadCount"] == 7
    assert len(list((server.root / ".robops" / "safety").glob("PLAYER_156-*.json"))) == 1


def test_player_edit_refuses_online_player(server):
    body = {"user_id": "261", "data": {"coins": 1}, "confirm": "production"}
    resp = server.post("/api/env/production/player/edit", body)
    assert resp.status_code == 409 and "Write refused" in resp.json()["error"]
    assert server.post("/api/env/production/player/edit", {**body, "force": True}).status_code == 200


def test_player_restore_from_listed_backup_only(server):
    base = {"user_id": "156", "backup": "backups/players-2026-11-03.json.gz"}
    preview = server.post("/api/env/production/player/restore", {**base, "preview": True}).json()
    assert preview["changes"] == ["~ coins", "~ level", "- pets"]
    assert server.post("/api/env/production/player/restore", {**base, "confirm": "production"}).status_code == 200
    assert server.cloud.stores["Default"]["PLAYER_156"]["Data"] == {"coins": 5000, "level": 20}

    # Never an arbitrary file of the PC
    resp = server.post("/api/env/production/player/restore", {**base, "backup": "../../etc/passwd", "preview": True})
    assert resp.status_code == 404
    # Backup of universe 333 restored into staging (universe 111)
    resp = server.post("/api/env/staging/player/restore", {**base, "preview": True})
    assert resp.status_code == 409 and "allow-other-universe" in resp.json()["error"]


def test_player_restore_does_not_resurrect_without_consent(server):
    base = {"user_id": "999", "backup": "backups/players-2026-11-03.json.gz", "confirm": "production"}
    resp = server.post("/api/env/production/player/restore", base)
    assert resp.status_code == 409 and "erasure" in resp.json()["error"]
    assert server.post("/api/env/production/player/restore", {**base, "recreate_missing": True}).status_code == 200
    assert "PLAYER_999" in server.cloud.stores["Default"]


def test_player_delete_needs_the_user_id_typed(server):
    body = {"user_id": "156", "confirm": "production"}
    assert server.post("/api/env/production/player/delete", body).status_code == 400
    assert server.post("/api/env/production/player/delete", {**body, "confirm_user": "156"}).status_code == 200
    assert "PLAYER_156" not in server.cloud.stores["Default"]


def test_errors_are_json_messages(server, monkeypatch):
    assert server.get("/api/env/nope/live").json()["error"].startswith("Environment 'nope' not found")
    assert server.get("/api/nothing").status_code == 404
    monkeypatch.delenv("ROBLOX_API_KEY")
    resp = server.post("/api/env/staging/restart", {"confirm": "staging"})
    assert resp.status_code == 400 and "$env:ROBLOX_API_KEY" in resp.json()["error"]


def test_without_github_repo_deployments_explain_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    app = make_dashboard(tmp_path)
    app.github_repo = None
    with pytest.raises(dashboard.DashboardError, match="--github-repo"):
        app.deployments()
    assert "error" in app.backups()["github"]


# --------------------------------------------------------------- helpers/CLI

@pytest.mark.parametrize("url, repo", [
    ("https://github.com/StarLightFr51/robops.git", "StarLightFr51/robops"),
    ("git@github.com:me/brick-tycoon.git", "me/brick-tycoon"),
    ("https://github.com/me/game", "me/game"),
    ("https://gitlab.com/me/game.git", None),
])
def test_repo_from_remote(tmp_path, monkeypatch, url, repo):
    class Done:
        stdout = url + "\n"
    monkeypatch.setattr("robops.github.subprocess.run", lambda *a, **kw: Done())
    assert repo_from_remote(tmp_path) == repo


def test_github_client_reports_permission_problems(monkeypatch):
    from conftest import FakeResponse

    class Session:
        headers: dict = {}

        def request(self, method, url, **kw):
            return FakeResponse(403, {"message": "Resource not accessible by personal access token"})
    gh = GitHub("me/game", Session())  # type: ignore[arg-type]
    with pytest.raises(Exception, match="Actions: Read"):
        gh.dispatch("roblox-deploy.yml", "main", {})


def test_dashboard_command_starts_the_server(tmp_path, monkeypatch, capsys):
    from dashboard_fakes import make_project
    make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    started = {}
    monkeypatch.setattr(dashboard, "serve", lambda app, port, open_browser: started.update(
        app=app, port=port, open_browser=open_browser))
    monkeypatch.setattr("robops.github.repo_from_remote", lambda root: None)
    assert cli.main(["dashboard", "--no-browser", "--port", "9000"]) == 0
    assert started["port"] == 9000 and started["open_browser"] is False
    assert "GitHub repository not found" in capsys.readouterr().err


def test_profile_helper_shape():
    assert profile({"a": 1})["MetaData"]["ActiveSession"] is None


# ------------------------------------------------- GitHub diagnosis (issue: 404)

def _app(tmp_path, monkeypatch, gh, token="t", **kw):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    app = make_dashboard(tmp_path, gh=gh)
    app.github_token = token
    for key, value in kw.items():
        setattr(app, key, value)
    return app


def test_token_without_access_to_private_repo_says_so(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, FakeGitHub(fail={"info": 404}))
    with pytest.raises(dashboard.DashboardError, match="ROBOPS_GITHUB_TOKEN is set, but this token can't see") as err:
        app.deployments()
    assert "Repository access must include brick-tycoon" in str(err.value)
    assert "can't see" in app.backups()["github"]["error"]
    steps = app.github_status()["steps"]
    assert [s["level"] for s in steps] == ["ok", "error"]


def test_missing_token_on_private_repo_asks_for_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, FakeGitHub(fail={"info": 404}), token=None)
    with pytest.raises(dashboard.DashboardError, match="without a token"):
        app.deployments()
    assert app.github_status()["steps"][0]["level"] == "warn"


def test_expired_token_and_missing_actions_permission(tmp_path, monkeypatch):
    with pytest.raises(dashboard.DashboardError, match="invalid or expired"):
        _app(tmp_path, monkeypatch, FakeGitHub(fail={"info": 401})).deployments()
    with pytest.raises(dashboard.DashboardError, match="Actions: Read"):
        _app(tmp_path, monkeypatch, FakeGitHub(fail={"workflows": 403})).deployments()


def test_renamed_workflow_is_detected(tmp_path, monkeypatch):
    gh = FakeGitHub(workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"},
                               {"name": "Deploy to Roblox", "path": ".github/workflows/deploy.yml"},
                               {"name": "Nightly backup", "path": ".github/workflows/backup-players.yml"}])
    app = _app(tmp_path, monkeypatch, gh)
    assert app.deployments()["workflow"] == {"file": "deploy.yml", "name": "Deploy to Roblox"}
    assert gh.runs_of == ["deploy.yml"]
    app.run_backup({})
    assert gh.dispatched[-1][0] == "backup-players.yml"
    assert [s["level"] for s in app.github_status()["steps"]] == ["ok", "ok", "ok", "ok"]


def test_no_deploy_workflow_lists_what_exists(tmp_path, monkeypatch):
    gh = FakeGitHub(workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}])
    app = _app(tmp_path, monkeypatch, gh)
    with pytest.raises(dashboard.DashboardError, match=r"No deploy workflow .* ci\.yml \(CI\).*--deploy-workflow"):
        app.deployments()
    forced = _app(tmp_path, monkeypatch, gh, deploy_workflow="ci.yml")
    assert forced.deployments()["workflow"]["file"] == "ci.yml"


def test_github_status_endpoint(server):
    steps = server.get("/api/github").json()["steps"]
    assert [s["message"] for s in steps][-2:] == ["Deploy workflow: roblox-deploy.yml (Roblox Deploy).",
                                                  "Backup workflow: roblox-backup.yml (Player Backup)."]
