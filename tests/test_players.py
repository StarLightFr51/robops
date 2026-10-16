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


# ------------------------------------------------------------------ unit tests

def test_session_states():
    assert pl.session_state(profile({}), NOW).status == "offline"
    assert pl.session_state(profile({}, online=True, age=120), NOW).status == "online"
    stale = pl.session_state(profile({}, online=True, age=3600), NOW)
    assert stale.status == "stale" and stale.writable and "job-abc" in stale.detail
    unknown = profile({}, online=True)
    del unknown["MetaData"]["LastUpdate"]
    assert pl.session_state(unknown, NOW).status == "online"   # cautious without a date


def test_with_data_replaces_only_data_and_unlocks():
    current = profile({"coins": 5}, online=True, age=5000)
    result = pl.with_data(current, {"coins": 500})
    assert result["Data"] == {"coins": 500}
    assert result["MetaData"]["ActiveSession"] is None
    assert result["MetaData"]["SessionLoadCount"] == 7
    assert result["GlobalUpdates"] == current["GlobalUpdates"]
    assert current["MetaData"]["ActiveSession"] is not None   # the original is not modified


def test_apply_edits_and_changed_fields():
    data = {"coins": 5, "stats": {"level": 1, "xp": 0}, "old": True}
    new = pl.apply_edits(data, ["coins=500", "stats.level=10", 'inventory=["sword"]', "title=Champion",
                                "deep.new.key=1"], ["old"])
    assert new == {"coins": 500, "stats": {"level": 10, "xp": 0}, "inventory": ["sword"],
                   "title": "Champion", "deep": {"new": {"key": 1}}}
    assert data["coins"] == 5
    assert pl.changed_fields(data, new) == ["~ coins", "+ deep", "+ inventory", "- old", "~ stats.level", "+ title"]
    with pytest.raises(pl.PlayerError):
        pl.apply_edits({}, ["no_equals_sign"])


def test_load_backup_formats(tmp_path):
    cfg = pl.PlayersConfig()
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"PLAYER_1": profile({"a": 1}), "OTHER": {}}))
    assert list(pl.load_backup(export, cfg)) == ["1"]

    native = tmp_path / "b.json"
    native.write_text(json.dumps({"format": pl.BACKUP_FORMAT, "datastore": "Other", "key_prefix": "PLAYER_",
                                  "profiles": {}}))
    with pytest.raises(pl.PlayerError, match="Other"):
        pl.load_backup(native, cfg)


# ------------------------------------------------------------- fake DataStore

class FakeDataStore:
    """Simulates the DataStore v2 API for a ProfileStore, including the real behaviour observed:

    - profiles are in the "global" scope;
    - listing WITHOUT a scope returns prefixed identifiers "global/PLAYER_…";
    - reading "global/PLAYER_…" WITHOUT a scope answers 404.
    """

    def __init__(self, entries, datastore="Default"):
        self.entries = {k: {"value": v, "etag": f"etag-{k}", "users": ["users/1"]} for k, v in entries.items()}
        self.datastore = datastore
        self.patches, self.deletes, self.scopes = [], [], []
        self.fail_patch_status = None

    def __call__(self, method, url, kw):
        base = f"/data-stores/{self.datastore}"
        assert base in url, url
        tail = url.split(base, 1)[1]
        scope = None
        if tail.startswith("/scopes/"):
            scope, _, rest = tail[len("/scopes/"):].partition("/")
            tail = "/" + rest
        assert tail.startswith("/entries"), url
        tail = tail[len("/entries"):]
        self.scopes.append(scope)
        if method == "GET" and tail == "":
            ids = sorted(self.entries)
            if scope is None:
                ids = [f"global/{k}" for k in ids]
            elif scope != "global":
                ids = []
            return FakeResponse(200, {"dataStoreEntries": [{"id": i} for i in ids]})
        key = unquote(tail.lstrip("/"))
        if scope not in (None, "global"):
            return FakeResponse(404, {"message": "Entry not found."})
        if method == "GET":
            if key not in self.entries:
                return FakeResponse(404, {"message": "Entry not found."})
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
    assert "online on server job-abc" in captured.err


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
    assert "Write refused" in capsys.readouterr().err
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
    assert run("restore", "--all", "--from", "b.json", "--yes") == 1   # player 2 is online
    assert sorted(p[0] for p in store.patches) == ["PLAYER_1", "PLAYER_3"]


def test_concurrent_change_is_reported(store, capsys):
    store.fail_patch_status = 409
    assert run("edit", "1", "--set", "coins=50") == 1
    assert "changed between the read and the write" in capsys.readouterr().err


def test_edit_dry_run_then_write(store, capsys):
    assert run("edit", "1", "--set", "coins=50", "--set", "stats.level=3", "--dry-run") == 0
    assert store.patches == []
    assert "~ coins" in capsys.readouterr().out
    assert run("edit", "1", "--set", "coins=50") == 0
    assert store.patches[-1][1]["value"]["Data"] == {"coins": 50}
    assert run("edit", "2", "--set", "coins=1") == 1          # online
    assert run("edit", "2", "--set", "coins=1", "--force") == 0


def test_delete_requires_yes(store):
    assert run("delete", "1") == 2
    assert run("delete", "1", "--yes") == 0
    assert store.deletes == ["PLAYER_1"]
    assert run("delete", "2", "--yes") == 1                    # online


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


def test_empty_backup_explains_why(store, capsys):
    store.entries.clear()
    assert run("backup", "--out", "b.json") == 0
    err = capsys.readouterr().err
    assert "No profile found" in err and "Data stores manager" in err


@pytest.mark.parametrize("keys, expected", [
    (["123456", "789012"], 'key_prefix = ""'),
    (["Player_1", "Player_22", "Config"], 'key_prefix = "Player_"'),
])
def test_backup_suggests_prefix_when_keys_dont_match(store, capsys, keys, expected):
    store.entries = {k: {"value": profile({}), "etag": "e", "users": []} for k in keys}
    assert run("backup", "--out", "b.json") == 0
    captured = capsys.readouterr()
    assert "none of them starts with 'PLAYER_'" in captured.err
    assert expected in captured.out


def test_backup_ignores_other_keys_in_same_store(store, tmp_path):
    store.entries["Config"] = {"value": {"motd": "hi"}, "etag": "e", "users": []}
    assert run("backup", "--out", "b.json") == 0
    data = json.loads((tmp_path / "b.json").read_text())
    assert data["count"] == 3 and "Config" not in data["profiles"]


def test_guess_prefix():
    assert pl.guess_prefix(["PLAYER_1", "PLAYER_2", "x"]) == "PLAYER_"
    assert pl.guess_prefix(["42"]) == ""
    assert pl.guess_prefix(["Config", "Settings"]) is None


def test_backup_targets_global_scope_by_default(store, tmp_path):
    assert run("backup", "--out", "b.json") == 0
    data = json.loads((tmp_path / "b.json").read_text())
    assert data["count"] == 3 and data["scope"] == "global" and data["errors"] == {}
    assert set(store.scopes) == {"global"}


def test_backup_without_scope_handles_scope_prefixed_ids(store, tmp_path):
    client = OpenCloudClient("k", session=FakeSession(router=store), sleep=lambda s: None)
    result = pl.backup_players(client, 99, pl.PlayersConfig(scope=None), tmp_path / "b.json")
    assert result.count == 3 and result.errors == {}
    data = json.loads((tmp_path / "b.json").read_text())
    assert set(data["profiles"]) == {"1", "2", "3"}


def test_split_scoped():
    assert pl.split_scoped("global/PLAYER_1", None) == ("global", "PLAYER_1")
    assert pl.split_scoped("global/PLAYER_1", "global") == ("global", "PLAYER_1")
    assert pl.split_scoped("PLAYER_1", "global") == ("global", "PLAYER_1")
    assert pl.split_scoped("a/b", "global") == ("global", "a/b")


def test_scoped_prefix_in_config_is_split(tmp_path):
    from robops.config import load_config
    (tmp_path / "robops.toml").write_text("""
[players]
key_prefix = "global/PLAYER_"

[environments.prod]
universe_id = 5
[[environments.prod.places]]
place_id = 6
file = "game.rbxl"
""")
    players = load_config(tmp_path / "robops.toml").env("prod").players
    assert (players.scope, players.key_prefix) == ("global", "PLAYER_")


def test_guess_prefix_ignores_scope():
    assert pl.guess_prefix(["global/PLAYER_4681560223", "global/PLAYER_522534348"]) == "PLAYER_"
