import gzip
import json
import time
from urllib.parse import unquote

import pytest

from robops import cli
from robops import players as pl
from robops.client import OpenCloudClient

from conftest import FakeResponse, FakeSession

NOW = time.time()


def profile(data, *, online=False, age=30):
    return {
        "Data": data,
        "MetaData": {
            "ProfileCreateTime": 1_700_000_000,
            "SessionLoadCount": 7,
            "ActiveSession": [111, "job-abc", "guid"] if online else None,
            "ForceLoadSession": None,
            "LastUpdate": NOW - age,
            "MetaTags": {},
        },
        "GlobalUpdates": [3, [[3, {"gift": 50}]]],
        "UserIds": [1],
        "RobloxMetaData": {},
    }


# ------------------------------------------------------------------ unitaires

def test_session_states():
    assert pl.session_state(profile({}), NOW).status == "offline"
    assert pl.session_state(profile({}, online=True, age=120), NOW).status == "online"
    stale = pl.session_state(profile({}, online=True, age=3600), NOW)
    assert stale.status == "stale" and stale.writable and "job-abc" in stale.detail
    unknown = profile({}, online=True)
    del unknown["MetaData"]["LastUpdate"]
    assert pl.session_state(unknown, NOW).status == "online"   # prudence sans date


def test_with_data_replaces_only_data_and_unlocks():
    current = profile({"coins": 5}, online=True, age=5000)
    result = pl.with_data(current, {"coins": 500})
    assert result["Data"] == {"coins": 500}
    assert result["MetaData"]["ActiveSession"] is None
    assert result["MetaData"]["SessionLoadCount"] == 7
    assert result["GlobalUpdates"] == current["GlobalUpdates"]
    assert current["MetaData"]["ActiveSession"] is not None   # l'original n'est pas modifié


def test_apply_edits_and_changed_fields():
    data = {"coins": 5, "stats": {"level": 1, "xp": 0}, "old": True}
    new = pl.apply_edits(data, ["coins=500", "stats.level=10", 'inventory=["sword"]', "title=Champion",
                                "deep.new.key=1"], ["old"])
    assert new == {"coins": 500, "stats": {"level": 10, "xp": 0}, "inventory": ["sword"],
                   "title": "Champion", "deep": {"new": {"key": 1}}}
    assert data["coins"] == 5
    assert pl.changed_fields(data, new) == ["~ coins", "+ deep", "+ inventory", "- old", "~ stats.level", "+ title"]
    with pytest.raises(pl.PlayerError):
        pl.apply_edits({}, ["sans_egal"])


def test_load_backup_formats(tmp_path):
    cfg = pl.PlayersConfig()
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"PLAYER_1": profile({"a": 1}), "OTHER": {}}))
    assert list(pl.load_backup(export, cfg)) == ["1"]

    native = tmp_path / "b.json"
    native.write_text(json.dumps({"format": pl.BACKUP_FORMAT, "datastore": "Autre", "key_prefix": "PLAYER_",
                                  "profiles": {}}))
    with pytest.raises(pl.PlayerError, match="Autre"):
        pl.load_backup(native, cfg)


# ------------------------------------------------------------- faux DataStore

class FakeDataStore:
    """Simule l'API DataStore v2 pour un ProfileStore."""

    def __init__(self, entries, datastore="Default"):
        self.entries = {k: {"value": v, "etag": f"etag-{k}", "users": ["users/1"]} for k, v in entries.items()}
        self.datastore = datastore
        self.patches, self.deletes = [], []
        self.fail_patch_status = None

    def __call__(self, method, url, kw):
        base = f"/data-stores/{self.datastore}/entries"
        assert base in url, url
        tail = url.split(base, 1)[1]
        if method == "GET" and tail == "":
            return FakeResponse(200, {"dataStoreEntries": [{"id": k} for k in sorted(self.entries)
                                                           if k.startswith("PLAYER_")]})
        key = unquote(tail.lstrip("/"))
        if method == "GET":
            if key not in self.entries:
                return FakeResponse(404, {"message": "NOT_FOUND"})
            return FakeResponse(200, self.entries[key])
        if method == "PATCH":
            if self.fail_patch_status:
                return FakeResponse(self.fail_patch_status, {"message": "etag mismatch"})
            self.patches.append((key, kw["json"], kw["params"]))
            self.entries[key] = {"value": kw["json"]["value"], "etag": "new", "users": kw["json"].get("users")}
            return FakeResponse(200, {"revisionId": "r2"})
        if method == "DELETE":
            self.deletes.append(key)
            return FakeResponse(200, {})
        raise AssertionError(method)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    fake = FakeDataStore({
        "PLAYER_1": profile({"coins": 10}),
        "PLAYER_2": profile({"coins": 20}, online=True, age=60),
        "PLAYER_3": profile({"coins": 30}, online=True, age=5000),
    })
    original = OpenCloudClient.__init__

    def init(self, api_key, **kwargs):
        original(self, api_key, session=FakeSession(router=fake), sleep=lambda s: None)
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", init)
    return fake


def run(*argv):
    return cli.main(["player", *argv, "--universe-id", "99"])


# ------------------------------------------------------------------- CLI

def test_get_prints_data_and_status(store, capsys):
    assert run("get", "2") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"coins": 20}
    assert "en ligne sur le serveur job-abc" in captured.err


def test_backup_then_restore_roundtrip(store, tmp_path):
    assert run("backup", "--out", "backups/players.json.gz", "--workers", "2") == 0
    with gzip.open(tmp_path / "backups" / "players.json.gz", "rt") as fh:
        backup = json.load(fh)
    assert backup["count"] == 3 and set(backup["profiles"]) == {"1", "2", "3"}
    assert backup["profiles"]["1"]["users"] == ["users/1"]
    assert not list((tmp_path / "backups").glob("*.tmp*"))

    store.entries["PLAYER_1"]["value"]["Data"] = {"coins": 0, "bug": True}
    assert run("restore", "1", "--from", "backups/players.json.gz") == 0

    key, body, params = store.patches[-1]
    assert key == "PLAYER_1"
    assert body["value"]["Data"] == {"coins": 10}
    assert body["etag"] == "etag-PLAYER_1"
    assert body["users"] == ["users/1"]
    assert params == {"allowMissing": "true"}
    safety = list((tmp_path / ".robops" / "safety").glob("PLAYER_1-*.json"))
    assert len(safety) == 1 and json.loads(safety[0].read_text())["value"]["Data"]["bug"] is True


def test_restore_refuses_online_player(store, capsys):
    with open("b.json", "w") as fh:
        json.dump({"PLAYER_2": profile({"coins": 999})}, fh)
    assert run("restore", "2", "--from", "b.json") == 1
    assert "Écriture refusée" in capsys.readouterr().err
    assert store.patches == []


def test_restore_over_stale_lock_clears_it(store):
    with open("b.json", "w") as fh:
        json.dump({"PLAYER_3": profile({"coins": 333}, online=True, age=99999)}, fh)
    assert run("restore", "3", "--from", "b.json") == 0
    body = store.patches[-1][1]["value"]
    assert body["Data"] == {"coins": 333}
    assert body["MetaData"]["ActiveSession"] is None
    assert body["MetaData"]["SessionLoadCount"] == 7


def test_restore_recreates_deleted_profile_without_lock(store):
    with open("b.json", "w") as fh:
        json.dump({"PLAYER_9": profile({"coins": 9}, online=True, age=10)}, fh)
    assert run("restore", "9", "--from", "b.json") == 0
    body = store.patches[-1][1]
    assert body["value"]["MetaData"]["ActiveSession"] is None
    assert "etag" not in body


def test_restore_all_requires_yes_and_skips_online(store):
    with open("b.json", "w") as fh:
        json.dump({k: profile({"coins": 1}) for k in ("PLAYER_1", "PLAYER_2", "PLAYER_3")}, fh)
    assert run("restore", "--all", "--from", "b.json") == 2
    assert store.patches == []
    assert run("restore", "--all", "--from", "b.json", "--dry-run") == 0
    assert store.patches == []
    assert run("restore", "--all", "--from", "b.json", "--yes") == 1   # le joueur 2 est en ligne
    assert sorted(p[0] for p in store.patches) == ["PLAYER_1", "PLAYER_3"]


def test_concurrent_change_is_reported(store, capsys):
    store.fail_patch_status = 409
    assert run("edit", "1", "--set", "coins=50") == 1
    assert "a changé entre la lecture et l'écriture" in capsys.readouterr().err


def test_edit_dry_run_then_write(store, capsys):
    assert run("edit", "1", "--set", "coins=50", "--set", "stats.level=3", "--dry-run") == 0
    assert store.patches == []
    assert "~ coins" in capsys.readouterr().out
    assert run("edit", "1", "--set", "coins=50") == 0
    assert store.patches[-1][1]["value"]["Data"] == {"coins": 50}
    assert run("edit", "2", "--set", "coins=1") == 1          # en ligne
    assert run("edit", "2", "--set", "coins=1", "--force") == 0


def test_delete_requires_yes(store):
    assert run("delete", "1") == 2
    assert run("delete", "1", "--yes") == 0
    assert store.deletes == ["PLAYER_1"]
    assert run("delete", "2", "--yes") == 1                    # en ligne


def test_players_section_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    (tmp_path / "robops.toml").write_text("""
[players]
datastore = "PlayerData"
key_prefix = "Player_"

[environments.prod]
universe_id = 5
[[environments.prod.places]]
place_id = 6
file = "game.rbxl"
""")
    fake = FakeDataStore({"Player_1": profile({"coins": 1})}, datastore="PlayerData")
    original = OpenCloudClient.__init__

    def init(self, api_key, **kwargs):
        original(self, api_key, session=FakeSession(router=fake), sleep=lambda s: None)
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", init)

    assert cli.main(["player", "edit", "1", "--set", "coins=2", "--env", "prod"]) == 0
    assert fake.patches[-1][0] == "Player_1"


def test_safety_copies_never_overwrite_each_other(tmp_path):
    cfg = pl.PlayersConfig()
    current = pl.CurrentProfile(exists=True, value=profile({"coins": 1}))
    first = pl.save_safety_copy(tmp_path, cfg, "1", current)
    current.value = profile({"coins": 2})
    second = pl.save_safety_copy(tmp_path, cfg, "1", current)
    assert first != second
    assert json.loads(first.read_text())["value"]["Data"] == {"coins": 1}
    assert json.loads(second.read_text())["value"]["Data"] == {"coins": 2}
