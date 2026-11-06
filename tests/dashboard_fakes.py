"""In-memory stand-ins for Open Cloud, roblox.com and GitHub, used by the dashboard
tests (and handy to preview the page without touching a real game)."""

from __future__ import annotations

import copy
import gzip
import json
import time
from pathlib import Path

from robops import players as pl
from robops.client import RobloxApiError
from robops.dashboard import Dashboard

NOW = time.time()


def profile(data, *, online=False, age=30):
    return {
        "Data": data,
        "MetaData": {
            "ProfileCreateTime": int(NOW - 90 * 86400), "SessionLoadCount": 7,
            "ActiveSession": [123, "job-abc", "guid"] if online else None,
            "ForceLoadSession": None, "LastUpdate": NOW - age, "MetaTags": {},
        },
        "UserIds": [1], "RobloxMetaData": {}, "GlobalUpdates": [0, []],
    }


class FakeCloud:
    """The part of OpenCloudClient the dashboard uses, backed by dicts."""

    def __init__(self):
        self.stores = {
            "Default": {"PLAYER_156": profile({"coins": 1250, "level": 14, "pets": ["cat", "dragon"]}),
                        "PLAYER_261": profile({"coins": 80, "level": 2}, online=True, age=40)},
            "GameConfig": {"motd": "Double XP this weekend!", "event": {"x2": True, "ends": "2026-11-09"}},
        }
        self.etags: dict[tuple[str, str], int] = {}
        self.calls: list[tuple] = []

    def get_universe(self, universe_id):
        return {"path": f"universes/{universe_id}", "displayName": "Brick Tycoon", "visibility": "PUBLIC",
                "ageRating": "AGE_RATING_9_PLUS", "createTime": "2025-03-02T10:00:00Z",
                "updateTime": "2026-11-04T14:20:00Z", "desktopEnabled": True, "mobileEnabled": True,
                "tabletEnabled": True, "consoleEnabled": False, "vrEnabled": False}

    def get_place(self, universe_id, place_id):
        return {"displayName": "Brick Tycoon", "serverSize": 24, "updateTime": "2026-11-04T14:20:00Z"}

    def restart_servers(self, universe_id):
        self.calls.append(("restart", universe_id))

    def snapshot_datastores(self, universe_id):
        self.calls.append(("snapshot", universe_id))
        return {"newSnapshotTaken": True}

    def list_datastores(self, universe_id, prefix=None):
        return [s for s in sorted(self.stores) if not prefix or s.startswith(prefix)]

    def list_entries_page(self, universe_id, datastore, scope=None, prefix=None, page_token=None, page_size=100):
        ids = sorted(k for k in self.stores.get(datastore, {}) if not prefix or k.startswith(prefix))
        return ids, None

    def get_entry(self, universe_id, datastore, key, scope=None):
        if key not in self.stores.get(datastore, {}):
            raise RobloxApiError(404, "Entry not found.")
        etag = self.etags.get((datastore, key), 1)
        return {"value": copy.deepcopy(self.stores[datastore][key]), "etag": f"e{etag}", "users": ["users/1"],
                "revisionId": f"r{etag}", "revisionCreateTime": "2026-11-04T13:00:00Z"}

    def set_entry(self, universe_id, datastore, key, value, scope=None, users=None, attributes=None,
                  allow_missing=True, etag=None):
        current = self.etags.get((datastore, key), 1)
        if etag is not None and etag != f"e{current}":
            raise RobloxApiError(412, "etag mismatch")
        self.stores.setdefault(datastore, {})[key] = copy.deepcopy(value)
        self.etags[(datastore, key)] = current + 1
        self.calls.append(("set", datastore, key))
        return {"revisionId": f"r{current + 1}"}

    def delete_entry(self, universe_id, datastore, key, scope=None):
        self.stores.get(datastore, {}).pop(key, None)
        self.calls.append(("delete", datastore, key))


class FakeWeb:
    def game(self, universe_id):
        return {"name": "Brick Tycoon", "root_place_id": 4242, "playing": 187, "visits": 1_284_310,
                "favorites": 20_514, "max_players": 24, "created": "2025-03-02T10:00:00Z",
                "updated": "2026-11-04T14:20:00Z", "up_votes": 9_870, "down_votes": 612}

    def servers(self, place_id, limit=100):
        sizes = [24, 24, 23, 21, 19, 18, 15, 14, 12, 9, 6, 2]
        return [{"id": f"job-{i}", "playing": n, "max_players": 24, "fps": 59.8, "ping": 80 + i}
                for i, n in enumerate(sizes)]

    def user(self, user_id):
        return {"id": user_id, "name": "brickfan", "display_name": "Brick Fan", "created": "2019-06-01T00:00:00Z",
                "banned": False}

    def user_id(self, username):
        return 156 if username.lower() == "brickfan" else None

    def headshot(self, user_id):
        return None


class FakeGitHub:
    repo = "me/brick-tycoon"

    def __init__(self, workflows=None, fail=None):
        self.dispatched: list[tuple] = []
        self.workflow_list = workflows if workflows is not None else [
            {"name": "CI", "path": ".github/workflows/ci.yml"},
            {"name": "Roblox Deploy", "path": ".github/workflows/roblox-deploy.yml"},
            {"name": "Player Backup", "path": ".github/workflows/roblox-backup.yml"},
        ]
        self.fail = fail or {}            # {"info": status, "workflows": status}
        self.runs_of: list[str] = []

    def _maybe_fail(self, what):
        from robops.github import GitHubError
        if what in self.fail:
            raise GitHubError(self.fail[what], f"GitHub HTTP {self.fail[what]}: Not Found")

    def info(self):
        self._maybe_fail("info")
        return {"default_branch": "main"}

    def default_branch(self):
        return self.info()["default_branch"]

    def workflows(self):
        self._maybe_fail("workflows")
        return self.workflow_list

    def runs(self, workflow, per_page=15):
        self.runs_of.append(workflow)
        base = [
            (9, "in_progress", None, "workflow_dispatch", "main", "Release 2.4: winter event", 300),
            (8, "completed", "success", "push", "develop", "Fix pet spawn rate", 5_400),
            (7, "completed", "failure", "workflow_dispatch", "main", "Release 2.3", 90_000),
            (6, "completed", "success", "push", "main", "Merge pull request #41", 95_000),
        ]
        runs = []
        for number, status, conclusion, event, branch, title, age in base:
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age))
            updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age + 110))
            runs.append({"id": number, "run_number": number, "display_title": title, "event": event,
                         "head_branch": branch, "head_sha": f"{number}abc1234ef", "actor": {"login": "StarLightFr51"},
                         "status": status, "conclusion": conclusion, "created_at": created, "updated_at": updated,
                         "html_url": f"https://github.com/{self.repo}/actions/runs/{number}"})
        return runs

    def jobs(self, run_id):
        if run_id == 6:
            return [{"name": "Build and checks"}]
        env = "staging" if run_id == 8 else "production"
        return [{"name": "Choose the target"}, {"name": f"Publish {env}"}]

    def artifacts(self, per_page=100):
        self._maybe_fail("info")               # a repository the token can't see: every endpoint fails
        return [{"id": 1, "name": "players-production-2026-11-04_0217Z.json.gz.age", "size_in_bytes": 48_211,
                 "created_at": "2026-11-04T02:18:00Z", "expires_at": "2026-12-04T02:18:00Z", "expired": False,
                 "workflow_run": {"id": 55}},
                {"id": 2, "name": "place-production-abc", "size_in_bytes": 1000}]

    def dispatch(self, workflow, ref, inputs):
        self.dispatched.append((workflow, ref, inputs))


CONFIG = """
[players]
datastore = "Default"
key_prefix = "PLAYER_"

[environments.staging]
universe_id = 111
restart_servers = true

[[environments.staging.places]]
name = "main"
place_id = 222
file = "build/game.rbxl"

[environments.production]
universe_id = 333

[[environments.production.places]]
name = "main"
place_id = 4242
file = "build/game.rbxl"
"""


def make_project(root: Path) -> Path:
    (root / "robops.toml").write_text(CONFIG, encoding="utf-8")
    (root / "build").mkdir(exist_ok=True)
    (root / "build" / "game.rbxl").write_bytes(b"PLACE")
    backups = root / "backups"
    backups.mkdir(exist_ok=True)
    backup = {"format": pl.BACKUP_FORMAT, "universe_id": 333, "datastore": "Default", "key_prefix": "PLAYER_",
              "scope": "global", "profiles": {"156": {"value": profile({"coins": 5000, "level": 20}), "users": []},
                                              "999": {"value": profile({"coins": 1}), "users": []}}}
    with gzip.open(backups / "players-2026-11-03.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(backup, fh)
    return root / "robops.toml"


def make_dashboard(root: Path, cloud: FakeCloud | None = None, gh: FakeGitHub | None = None) -> Dashboard:
    cloud = cloud or FakeCloud()
    gh = gh or FakeGitHub()
    return Dashboard(make_project(root), github_repo=gh.repo, github_token="t", web=FakeWeb(),  # type: ignore[arg-type]
                     client_factory=lambda key: cloud,  # type: ignore[arg-type,return-value]
                     github_factory=lambda repo, token: gh,  # type: ignore[arg-type,return-value]
                     sample_every=0,                  # tests start the sampler themselves
                     claude_logs=root / "claude-logs",     # never this computer's own Claude Desktop
                     claude_config_files=[root / "claude" / "claude_desktop_config.json"])
