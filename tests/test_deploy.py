import json
import sys

import pytest

from robops.client import PublishResult, RobloxApiError
from robops.config import load_config
from robops.deploy import DeployError, DeployOptions, deploy


class FakeClient:
    def __init__(self, fail_metadata=False, existing_entries=None):
        self.calls = []
        self.fail_metadata = fail_metadata
        self.existing = existing_entries or {}

    def publish_place(self, universe_id, place_id, path, version_type):
        self.calls.append(("publish", place_id, path.name, version_type))
        return PublishResult(universe_id, place_id, 100 + len(self.calls), version_type)

    def update_place(self, universe_id, place_id, **fields):
        self.calls.append(("update_place", place_id, fields))
        if self.fail_metadata:
            raise RobloxApiError(409, "Team Create session active")
        return {}

    def snapshot_datastores(self, universe_id):
        self.calls.append(("snapshot",))
        return {"newSnapshotTaken": True}

    def get_entry(self, universe_id, ds, key, scope=None):
        if key in self.existing:
            return self.existing[key]
        raise RobloxApiError(404, "not found")

    def set_entry(self, universe_id, ds, key, value, scope=None, users=None, attributes=None):
        self.calls.append(("set_entry", ds, key, value, users, attributes))
        return {"revisionId": "r"}

    def restart_servers(self, universe_id):
        self.calls.append(("restart", universe_id))


@pytest.fixture
def project(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text(json.dumps({"motd": "Hello", "event": {"x2": True}}))
    builder = f'{sys.executable} -c "open(\'build/game.rbxl\',\'wb\').write(b\'PLACE\')"'
    (tmp_path / "robops.toml").write_text(f"""
[build]
command = '''{builder}'''

[environments.prod]
universe_id = 10
restart_servers = true

[[environments.prod.places]]
name = "main"
place_id = 20
file = "build/game.rbxl"
display_name = "My game"
server_size = 30

[[environments.prod.places]]
name = "lobby"
place_id = 21
file = "build/game.rbxl"

[[environments.prod.datastores]]
name = "GameConfig"
file = "data/config.json"
""")
    return tmp_path


def run(project, opts, client):
    cfg = load_config(project / "robops.toml")
    return deploy(cfg.env("prod"), opts, project_root=cfg.root, client_factory=lambda: client)


def test_full_deploy_runs_every_step_in_order(project):
    client = FakeClient(existing_entries={"motd": {"value": "old", "users": ["users/1"], "attributes": {"a": 1}}})

    report = run(project, DeployOptions(with_datastores=True), client)

    kinds = [c[0] for c in client.calls]
    assert kinds == ["publish", "publish", "update_place", "snapshot", "set_entry", "set_entry", "restart"]
    assert report.versions() == {"main": 101, "lobby": 102}
    assert client.calls[2] == ("update_place", 20, {"display_name": "My game", "description": None, "server_size": 30})
    # existing users/attributes kept, missing entry created without metadata
    assert client.calls[4] == ("set_entry", "GameConfig", "motd", "Hello", ["users/1"], {"a": 1})
    assert client.calls[5] == ("set_entry", "GameConfig", "event", {"x2": True}, None, None)
    assert report.datastore_writes == 2 and report.restarted
    assert "| main | `20` |" in report.to_markdown()


def test_dry_run_builds_but_never_creates_client(project):
    def boom():
        raise AssertionError("no client must be created in dry-run")

    cfg = load_config(project / "robops.toml")
    report = deploy(cfg.env("prod"), DeployOptions(dry_run=True, with_datastores=True),
                    project_root=cfg.root, client_factory=boom)

    assert (project / "build" / "game.rbxl").read_bytes() == b"PLACE"
    assert report.dry_run and report.versions() == {"main": None, "lobby": None}


def test_datastores_are_opt_in_and_restart_can_be_disabled(project):
    client = FakeClient()
    run(project, DeployOptions(restart=False), client)
    assert [c[0] for c in client.calls] == ["publish", "publish", "update_place"]


def test_place_filter_and_saved_skips_restart(project):
    client = FakeClient()
    report = run(project, DeployOptions(only_places=["lobby"], version_type="Saved"), client)
    assert client.calls == [("publish", 21, "game.rbxl", "Saved")]
    assert not report.restarted


def test_unknown_place_filter(project):
    with pytest.raises(DeployError, match="Unknown place"):
        run(project, DeployOptions(only_places=["nope"]), FakeClient())


def test_metadata_failure_is_a_warning_unless_strict(project):
    report = run(project, DeployOptions(restart=False), FakeClient(fail_metadata=True))
    assert report.places[0].metadata == "failed (409)"
    with pytest.raises(RobloxApiError):
        run(project, DeployOptions(restart=False, strict_metadata=True), FakeClient(fail_metadata=True))


def test_preflight_fails_before_any_api_call(project):
    toml = (project / "robops.toml").read_text().replace("command = ", "# command = ")
    (project / "robops.toml").write_text(toml)
    client = FakeClient()
    with pytest.raises(DeployError, match="not found"):
        run(project, DeployOptions(), client)
    assert client.calls == []


def test_failed_build_stops_deploy(project):
    toml = (project / "robops.toml").read_text()
    toml = toml.replace(toml.split("\n")[2], f"command = '{sys.executable} -c \"raise SystemExit(3)\"'")
    (project / "robops.toml").write_text(toml)
    with pytest.raises(DeployError, match="code 3"):
        run(project, DeployOptions(), FakeClient())
