import json

import pytest

from robops import __version__, cli

from conftest import FakeResponse, FakeSession

CONFIG = """
[environments.staging]
universe_id = 111

[[environments.staging.places]]
name = "main"
place_id = 222
file = "game.rbxl"
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "robops.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "game.rbxl").write_bytes(b"PLACE")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_http(monkeypatch):
    """Replaces the HTTP session of every client created by the CLI."""
    session = FakeSession(router=lambda method, url, kw: (
        FakeResponse(200, {"versionNumber": 8}) if "/versions" in url else FakeResponse(200, {"value": {"a": 1}})
    ))
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", _patched_init(session), raising=True)
    return session


def _patched_init(session):
    original = cli.OpenCloudClient.__init__

    def init(self, api_key, **kwargs):
        original(self, api_key, session=session, sleep=lambda s: None)
    return init


def test_deploy_dry_run_writes_github_outputs(repo, monkeypatch):
    out_file, summary = repo / "out.txt", repo / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert cli.main(["deploy", "staging", "--dry-run"]) == 0

    outputs = out_file.read_text(encoding="utf-8")
    assert "environment=staging" in outputs
    assert 'versions={"main": null}' in outputs
    assert "dry-run" in summary.read_text(encoding="utf-8")


def test_deploy_without_key_fails_fast(repo, capsys):
    assert cli.main(["deploy", "staging"]) == 2
    assert "ROBLOX_API_KEY" in capsys.readouterr().err


def test_deploy_publishes_with_key(repo, monkeypatch, fake_http, capsys):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    assert cli.main(["deploy", "staging", "--json"]) == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout[stdout.index('{\n  "environment"'):])
    assert payload["versions"] == {"main": 8}
    assert fake_http.calls[0]["url"].endswith("/universes/v1/111/places/222/versions")
    assert fake_http.headers["x-api-key"] == "k"


def test_options_accepted_after_subcommand(repo, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text(CONFIG.replace("staging", "qa"), encoding="utf-8")
    assert cli.main(["deploy", "qa", "--dry-run", "-c", str(other)]) == 0


def test_publish_command_with_explicit_ids(repo, monkeypatch, fake_http):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    assert cli.main(["publish", "game.rbxl", "--universe-id", "5", "--place-id", "6", "--version-type", "Saved"]) == 0
    assert fake_http.calls[0]["params"] == {"versionType": "Saved"}


def test_datastore_get_uses_env_from_config(repo, monkeypatch, fake_http, capsys):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    assert cli.main(["ds", "get", "Config", "motd", "--env", "staging"]) == 0
    assert "/universes/111/data-stores/Config/entries/motd" in fake_http.calls[0]["url"]
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_datastore_import_dry_run(repo, monkeypatch, capsys):
    (repo / "seed.json").write_text(json.dumps({"k1": 1, "k2": [2]}), encoding="utf-8")
    assert cli.main(["ds", "import", "Config", "seed.json", "--universe-id", "1", "--dry-run"]) == 0
    assert "k2" in capsys.readouterr().out


def test_api_error_exit_code(repo, monkeypatch, capsys):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    session = FakeSession(router=lambda m, u, kw: FakeResponse(401, {"message": "Invalid API Key"}))
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", _patched_init(session))
    assert cli.main(["servers", "restart", "--universe-id", "1"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "0.0.0.0/0" in err


def test_check_and_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    assert (tmp_path / "robops.toml").is_file()
    assert (tmp_path / ".github/workflows/roblox-deploy.yml").is_file()
    assert (tmp_path / "rokit.toml").is_file()
    assert ".robops/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert cli.main(["init"]) == 0                      # idempotent: no duplicates
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count(".robops/") == 1
    # the template requires the IDs to be filled in
    assert cli.main(["check"]) == 1


def test_init_action_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--action-repo", "alex/robops"]) == 0
    workflow = (tmp_path / ".github/workflows/roblox-deploy.yml").read_text(encoding="utf-8")
    assert "uses: ./tools/robops" not in workflow
    assert workflow.count(f"uses: alex/robops@v{__version__}") == 2
    backup = (tmp_path / ".github/workflows/roblox-backup.yml").read_text(encoding="utf-8")
    assert f"uses: alex/robops/backup@v{__version__}\n" in backup
    assert "uses: ./tools/robops" not in backup


def test_point_to_action_repo_keeps_ref():
    text = "      - uses: ./tools/robops                  # path\n      - uses: ./tools/robops/backup\n"
    result = cli._point_to_action_repo(text, "alex/robops@v1.2.0")
    assert result == "      - uses: alex/robops@v1.2.0\n      - uses: alex/robops/backup@v1.2.0\n"


def test_missing_key_message_in_github_actions(repo, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "alex/Incremental-Game")
    assert cli.main(["deploy", "staging"]) == 2
    out = capsys.readouterr().out
    assert "alex/Incremental-Game" in out and "Repository secrets" in out


def test_missing_key_message_locally(repo, capsys):
    assert cli.main(["deploy", "staging"]) == 2
    assert "Run workflow" in capsys.readouterr().err


def test_publish_rejects_non_place_file_without_traceback(repo, monkeypatch, capsys):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    (repo / "notes.txt").write_text("x", encoding="utf-8")
    assert cli.main(["publish", "notes.txt", "--universe-id", "1", "--place-id", "2"]) == 2
    assert ".rbxl or .rbxlx" in capsys.readouterr().err


def test_unexpected_errors_become_a_message(repo, monkeypatch, capsys):
    def boom(args):
        raise ZeroDivisionError("oops")
    monkeypatch.setattr(cli, "cmd_check", boom)
    parser_args = cli.build_parser().parse_args(["check"])
    parser_args.func = boom
    assert cli._run(parser_args) == 1
    err = capsys.readouterr().err
    assert "Unexpected error (ZeroDivisionError): oops" in err and "ROBOPS_DEBUG" in err

    monkeypatch.setenv("ROBOPS_DEBUG", "1")
    with pytest.raises(ZeroDivisionError):
        cli._run(parser_args)


def test_failed_deploy_still_writes_the_summary(repo, monkeypatch, capsys):
    from robops.client import PublishResult
    (repo / "robops.toml").write_text(CONFIG + """
[[environments.staging.places]]
name = "lobby"
place_id = 333
file = "game.rbxl"
""", encoding="utf-8")
    summary, outputs = repo / "summary.md", repo / "out.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("ROBLOX_API_KEY", "k")

    def publish(self, universe_id, place_id, path, *, version_type="Published"):
        if place_id == 333:
            raise cli.RobloxApiError(500, "Internal error")
        return PublishResult(universe_id, place_id, 8, version_type)
    monkeypatch.setattr(cli.OpenCloudClient, "publish_place", publish)

    assert cli.main(["deploy", "staging"]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "failed" in text and "main v8" in text
    assert 'versions={"main": 8, "lobby": null}' in outputs.read_text(encoding="utf-8")
    assert "failed=true" in outputs.read_text(encoding="utf-8")
    assert "Already live before the failure: main v8" in capsys.readouterr().err


# ------------------------------------------------------- check / ds export-import

def test_check_valid_config_offline_and_online(repo, monkeypatch, capsys):
    assert cli.main(["check"]) == 0                       # no key: only a warning offline
    assert "ROBLOX_API_KEY is not set" in capsys.readouterr().err
    assert cli.main(["check", "--online"]) == 1           # but required online

    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    status = {"code": 200}
    session = FakeSession(router=lambda m, u, kw: FakeResponse(status["code"], {"displayName": "My game"}))
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", _patched_init(session))
    assert cli.main(["check", "staging", "--online"]) == 0
    assert "main reachable: 'My game'" in capsys.readouterr().out

    status["code"] = 403                                  # key without access to the place
    assert cli.main(["check", "--online"]) == 1


def test_check_reports_missing_datastore_file(repo, capsys):
    (repo / "robops.toml").write_text(CONFIG + "\n[[environments.staging.datastores]]\nname = 'C'\nfile = 'nope.json'\n", encoding="utf-8")
    assert cli.main(["check"]) == 1
    assert "file not found" in capsys.readouterr().err


def test_datastore_export_then_import_roundtrip(repo, monkeypatch):
    from test_players import FakeDataStore
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    store = FakeDataStore({"motd": "Hello", "event": {"x2": True}}, datastore="GameConfig")
    monkeypatch.setattr(cli.OpenCloudClient, "__init__", _patched_init(FakeSession(router=store)))

    assert cli.main(["ds", "export", "GameConfig", "--out", "export.json", "--universe-id", "99"]) == 0
    exported = json.loads((repo / "export.json").read_text(encoding="utf-8"))
    assert exported == {"event": {"x2": True}, "motd": "Hello"}

    exported["motd"] = "Double XP!"
    (repo / "export.json").write_text(json.dumps(exported), encoding="utf-8")
    assert cli.main(["ds", "import", "GameConfig", "export.json", "--universe-id", "99", "--no-snapshot"]) == 0
    written = {key: body["value"] for key, body, _ in store.patches}
    assert written == {"motd": "Double XP!", "event": {"x2": True}}
    assert all(body["etag"].startswith("etag-") for _, body, _ in store.patches)   # conditioned writes


def test_datastore_import_rejects_non_object(repo, capsys):
    (repo / "seed.json").write_text("[1, 2]", encoding="utf-8")
    assert cli.main(["ds", "import", "Config", "seed.json", "--universe-id", "1"]) == 2
    assert "JSON object" in capsys.readouterr().err
