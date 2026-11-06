import json

import pytest

from robops import playtests as pt
from robops.config import ConfigError, load_config

from dashboard_fakes import FakeCloud, make_dashboard

# ------------------------------------------------------------------ config

def _cfg(tmp_path, text):
    path = tmp_path / "robops.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


ENVS = """
[environments.staging]
universe_id = 1
[[environments.staging.places]]
place_id = 2
file = "x.rbxl"
[environments.production]
universe_id = 3
[environments.production.playtests]
datastore = "ProdReports"
[[environments.production.places]]
place_id = 4
file = "x.rbxl"
"""


def test_playtests_datastore_default_global_and_per_environment(tmp_path):
    cfg = _cfg(tmp_path, ENVS)
    assert cfg.env("staging").playtests_datastore == "RobopsPlaytests"
    assert cfg.env("production").playtests_datastore == "ProdReports"
    cfg = _cfg(tmp_path, '[playtests]\ndatastore = "SimReports"\n' + ENVS)
    assert cfg.env("staging").playtests_datastore == "SimReports"
    with pytest.raises(ConfigError, match="did you mean 'datastore'"):
        _cfg(tmp_path, '[playtests]\ndatastor = "x"\n' + ENVS)


# ----------------------------------------------------------------- reports

def test_normalize_report_is_tolerant():
    rep = pt.normalize_report("k", {
        "format": pt.REPORT_FORMAT, "title": "T",
        "players": [{"name": "A", "outcome": "Finished", "duration_s": "12"}, {"outcome": "blocked", "steps": ["walk", {"t": 3, "action": "jump"}]},
                    "just a sentence", {"outcome": "teleported"}],
        "findings": [{"severity": "minor", "title": "x"}, {"severity": "CRITICAL", "title": "y"}, "z"],
        "metrics": {"coins": 5, "nested": {"a": 1}, "flag": True},
    }, "datastore")
    assert [p["outcome"] for p in rep["players"]] == ["completed", "stuck", "unknown", "other"]
    assert rep["players"][0]["duration_s"] == 12.0 and rep["players"][1]["name"] == "Player 2"
    assert rep["players"][1]["steps"] == [{"t": None, "action": "walk", "result": None}, {"t": 3.0, "action": "jump", "result": None}]
    assert [f["severity"] for f in rep["findings"]] == ["high", "low", "low"]          # sorted, unknown = low
    assert rep["metrics"] == {"coins": 5, "nested": '{"a": 1}', "flag": "true"}
    assert rep["counts"]["outcomes"] == {"completed": 1, "stuck": 1, "unknown": 1, "other": 1}
    assert pt.normalize_report("k", "free text", "file")["recognized"] is False


def test_instructions_name_the_datastore_and_format():
    text = pt.instructions("MyReports")
    assert 'GetDataStore("MyReports")' in text and pt.REPORT_FORMAT in text and "run_code" in text


# --------------------------------------------------------- Claude Desktop logs

def _rpc(i, name, args):
    return {"method": "tools/call", "params": {"name": name, "arguments": args}, "jsonrpc": "2.0", "id": i}


def _res(i, text, err=False):
    return {"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": text}], "isError": err}}


def _line(t, server, direction, msg):
    return f"2026-11-06T{t}.000Z [{server}] [info] Message from {direction}: {json.dumps(msg)} {{ metadata: undefined }}"


def test_parse_log_pairs_calls_and_ignores_noise():
    lines = [
        "2026-11-06T21:00:00.000Z [Roblox Studio] [info] Server started and connected successfully",
        _line("21:00:01", "Roblox Studio", "client", _rpc(1, "run_code", {"command": "print(1)"})),
        "garbage { not json",
        _line("21:00:02", "Roblox Studio", "server", _res(1, "1")),
        _line("21:00:03", "Roblox Studio", "client", _rpc(2, "start_stop_play", {"mode": "start_play"})),   # no answer
        _line("21:00:04", "Roblox Studio", "server", {"jsonrpc": "2.0", "id": 3, "result": {}}),            # unknown id
        _line("21:00:05", "Roblox Studio", "client", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]
    calls = pt.parse_log_lines(lines)
    assert [(c["tool"], c["result"], c["is_error"], c["duration_ms"]) for c in calls] == [
        ("run_code", "1", False, 1000), ("start_stop_play", None, None, None)]


def test_shared_mcp_log_keeps_only_the_studio_server():
    lines = [_line("21:00:01", "filesystem", "client", _rpc(1, "read_file", {})),
             _line("21:00:01", "Roblox_Studio", "client", _rpc(1, "run_code", {"command": "x"})),
             _line("21:00:02", "Roblox_Studio", "server", _res(1, "ok", err=True))]
    calls = pt.parse_log_lines(lines, shared=True)
    assert [(c["tool"], c["is_error"]) for c in calls] == [("run_code", True)]


def test_studio_activity_groups_sessions_and_finds_files(tmp_path):
    logs = tmp_path / "logs"
    assert pt.studio_activity([logs])["sessions"] == []                      # missing folder: no crash
    logs.mkdir()
    (logs / "mcp-server-filesystem.log").write_text(_line("10:00:00", "filesystem", "client", _rpc(1, "x", {})), encoding="utf-8")
    lines = [_line("10:00:00", "Roblox Studio", "client", _rpc(1, "run_code", {"command": "a"})),
             _line("10:00:01", "Roblox Studio", "server", _res(1, "a")),
             _line("11:00:00", "Roblox Studio", "client", _rpc(2, "run_code", {"command": "b\n" * 3})),
             _line("11:00:02", "Roblox Studio", "server", _res(2, "boom", err=True))]
    (logs / "mcp-server-Roblox Studio.log").write_text("\n".join(lines), encoding="utf-8")
    act = pt.studio_activity([tmp_path / "elsewhere", logs])
    assert act["files"] == [str(logs / "mcp-server-Roblox Studio.log")]
    assert [(s["count"], s["errors"]) for s in act["sessions"]] == [(1, 1), (1, 0)]   # newest first, gap > 30 min
    assert act["sessions"][0]["calls"][0]["arguments"]["command"] == "b\n" * 3


def test_claude_log_dirs_cover_windows_and_mac(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    dirs = pt.claude_log_dirs()
    assert dirs[0] == tmp_path / "Roaming" / "Claude" / "logs"
    assert any(d.parts[-3:] == ("Library", "Logs", "Claude") for d in dirs)


# --------------------------------------------------------------- dashboard

REPORT = {"format": pt.REPORT_FORMAT, "title": "Tutorial", "started_at": "2026-11-06T21:30:00Z",
          "players": [{"name": "Sim_1", "outcome": "completed"}, {"name": "Sim_2", "outcome": "stuck"}],
          "findings": [{"severity": "high", "title": "Arrow behind a wall"}]}


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    cloud = FakeCloud()
    cloud.stores["RobopsPlaytests"] = {"playtest-2026-11-06T21:42:31.120Z": REPORT,
                                       "playtest-2026-11-05T10:00:00.000Z": {**REPORT, "title": "Older", "started_at": "2026-11-05T10:00:00Z"}}
    return make_dashboard(tmp_path, cloud), cloud


def test_dashboard_lists_and_opens_reports(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    (tmp_path / "playtests").mkdir()
    (tmp_path / "playtests" / "manual.json").write_text(json.dumps({**REPORT, "title": "Manual", "started_at": "2026-11-07T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "playtests" / "broken.json").write_text("{nope", encoding="utf-8")
    data = app.playtests("production")
    assert data["datastore"] == "RobopsPlaytests" and data["remote_error"] is None
    titles = [r["title"] for r in data["reports"]]
    assert titles[:3] == ["Manual", "Tutorial", "Older"] and "broken.json" in titles
    assert data["reports"][1]["counts"] == {"players": 2, "outcomes": {"completed": 1, "stuck": 1},
                                            "findings": {"high": 1, "medium": 0, "low": 0}}
    full = app.playtest("production", "playtest-2026-11-06T21:42:31.120Z")
    assert full["findings"][0]["title"] == "Arrow behind a wall" and full["source"] == "datastore"
    assert app.playtest("production", "file:playtests/manual.json")["title"] == "Manual"
    with pytest.raises(Exception, match="not found"):
        app.playtest("production", "file:../../secret.json")       # only listed files can be read


def test_missing_report_datastore_is_not_an_error(tmp_path, monkeypatch):
    app, cloud = _app(tmp_path, monkeypatch)
    del cloud.stores["RobopsPlaytests"]
    assert app.playtests("production") == {"datastore": "RobopsPlaytests", "universe_id": 333, "reports": [], "remote_error": None}


def test_without_api_key_local_reports_still_show(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    monkeypatch.delenv("ROBLOX_API_KEY")
    app.import_playtest({"report": REPORT})
    data = app.playtests("production")
    assert "ROBLOX_API_KEY" in data["remote_error"] and [r["source"] for r in data["reports"]] == ["file"]


def test_import_pasted_report(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    done = app.import_playtest({"report": json.dumps(REPORT)})
    assert done["key"].startswith("file:.robops/playtests/playtest-")
    assert app.playtest("production", done["key"])["title"] == "Tutorial"
    for bad, message in [("{oops", "not valid JSON"), ({"hello": 1}, "not a report"), (None, "not a report")]:
        with pytest.raises(Exception, match=message):
            app.import_playtest({"report": bad})
    log = (tmp_path / ".robops" / "audit.log").read_text(encoding="utf-8")
    assert '"action": "playtest.import"' in log


def test_claude_activity_and_instructions(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "mcp-server-Roblox Studio.log").write_text(
        "\n".join([_line("10:00:00", "Roblox Studio", "client", _rpc(1, "run_code", {"command": "a"})),
                   _line("10:00:01", "Roblox Studio", "server", _res(1, "a"))]), encoding="utf-8")
    app.claude_logs = logs
    assert app.claude_activity()["sessions"][0]["tools"] == {"run_code": 1}
    assert 'GetDataStore("RobopsPlaytests")' in app.playtest_instructions("staging")["text"]
