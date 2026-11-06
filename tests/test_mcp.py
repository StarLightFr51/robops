"""robops MCP server (Claude saves its reports) and its installation in Claude Desktop."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from robops import claude_desktop as cd
from robops import mcp_server
from robops import playtests as pt
from robops.cli import main

from dashboard_fakes import make_dashboard

REPORT = {"title": "Tutorial", "summary": "Two players out of three finish.",
          "players": [{"name": "Sim_1", "outcome": "completed", "feedback": "Fun"},
                      {"name": "Sim_2", "outcome": "stuck", "feedback": "Où est la boutique ?",
                       "steps": [{"t": 0, "action": "Spawn"}]}],
          "findings": [{"severity": "high", "title": "Arrow behind a wall", "where": "Workspace.Tutorial"}]}


def rpc(server, method, params=None, msg_id=1):
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}))
    return json.loads(reply)


def call(server, tool, args):
    res = rpc(server, "tools/call", {"name": tool, "arguments": args})["result"]
    return res["content"][0]["text"], res.get("isError", False)


# ------------------------------------------------------------------ protocol

def test_handshake_negotiates_the_protocol_version(tmp_path):
    server = mcp_server.McpServer(tmp_path, log=lambda m: None)
    res = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai", "version": "1"}})
    assert res["result"]["protocolVersion"] == "2025-06-18"
    assert res["result"]["serverInfo"]["name"] == "robops" and "save_playtest_report" in res["result"]["instructions"]
    assert set(res["result"]["capabilities"]) == {"tools", "prompts"}
    newer = rpc(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"]
    assert newer == mcp_server.PROTOCOL_VERSIONS[0]                 # unknown: the newest this server speaks
    assert server.handle_line('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    assert rpc(server, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_errors_follow_json_rpc(tmp_path):
    server = mcp_server.McpServer(tmp_path, log=lambda m: None)
    assert json.loads(server.handle_line("{nope"))["error"]["code"] == -32700
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert rpc(server, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert server.handle_line("   ") is None
    assert server.handle_line('{"jsonrpc":"2.0","id":9,"result":{}}') is None      # an answer: nothing to say
    batch = server.handle_line(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                           {"jsonrpc": "2.0", "method": "notifications/initialized"}]))
    assert json.loads(batch) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tools_only_declare_name_description_and_input_schema(tmp_path):
    tools = rpc(mcp_server.McpServer(tmp_path, log=lambda m: None), "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["save_playtest_report", "list_playtest_reports", "get_playtest_report"]
    assert all(set(t) == {"name", "description", "inputSchema"} for t in tools)
    assert tools[0]["inputSchema"]["required"] == ["title", "summary", "players"]


# --------------------------------------------------------------------- tools

def test_save_list_and_get_reports(tmp_path):
    server = mcp_server.McpServer(tmp_path, log=lambda m: None)
    rpc(server, "initialize", {"clientInfo": {"name": "claude-ai", "version": "0.14"}})
    text, error = call(server, "save_playtest_report", REPORT)
    assert not error and "2 player(s) (1 completed, 1 stuck), 1 problem(s)" in text
    files = list((tmp_path / ".robops" / "playtests").glob("*.json"))
    assert len(files) == 1 and not list((tmp_path / ".robops" / "playtests").glob(".*"))   # no temp file left
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["format"] == pt.REPORT_FORMAT and saved["saved_by"] == "robops mcp (claude-ai 0.14)"
    assert saved["players"][1]["feedback"] == "Où est la boutique ?"

    call(server, "save_playtest_report", {"report": {**REPORT, "title": "Shop", "started_at": "2099-01-01T00:00:00Z"}})
    listing = json.loads(call(server, "list_playtest_reports", {"limit": 5})[0])
    assert listing["total"] == 2 and [r["title"] for r in listing["reports"]] == ["Shop", "Tutorial"]
    full = json.loads(call(server, "get_playtest_report", {"key": listing["reports"][1]["key"]})[0])
    assert full["findings"][0]["where"] == "Workspace.Tutorial" and full["players"][1]["steps"][0]["action"] == "Spawn"
    assert call(server, "get_playtest_report", {"key": "file:../../etc/passwd"})[1] is True      # only listed files


def test_report_tolerance_and_refusals(tmp_path):
    server = mcp_server.McpServer(tmp_path, log=lambda m: None)
    as_text = {**REPORT, "players": json.dumps(REPORT["players"]), "findings": json.dumps(REPORT["findings"])}
    assert call(server, "save_playtest_report", as_text)[1] is False         # nested JSON sent as text
    text, error = call(server, "save_playtest_report", {"title": "x", "summary": "y"})
    assert error and "players" in text
    text, error = call(server, "save_playtest_report", {**REPORT, "summary": "x" * 1_100_000})
    assert error and "limit" in text
    assert call(server, "list_playtest_reports", {"limit": "lots"})[1] is False


def test_simulate_players_prompt(tmp_path):
    server = mcp_server.McpServer(tmp_path, log=lambda m: None)
    assert rpc(server, "prompts/list")["result"]["prompts"][0]["name"] == "simulate_players"
    msg = rpc(server, "prompts/get", {"name": "simulate_players", "arguments": {"players": "3", "focus": "the shop"}})
    text = msg["result"]["messages"][0]["content"]["text"]
    assert text.startswith("Simulate 3 players") and "the shop" in text and "save_playtest_report" in text
    assert rpc(server, "prompts/get", {"name": "other"})["error"]["code"] == -32602


def test_stdio_server_writes_only_protocol_on_stdout(tmp_path):
    """The real program, as Claude Desktop starts it: one JSON message per line, nothing else on stdout."""
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "save_playtest_report", "arguments": REPORT}}]
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "ROBOPS_STATE_DIR": str(tmp_path / "state")}
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "robops", "mcp", "serve", "--project", str(tmp_path)],
                          input="".join(json.dumps(m, ensure_ascii=False) + "\n" for m in lines).encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [0, 1, 2]
    assert "Report saved" in replies[2]["result"]["content"][0]["text"]
    assert "MCP server" in proc.stderr.decode("utf-8", "replace")
    assert len(list((tmp_path / ".robops" / "playtests").glob("*.json"))) == 1


# ------------------------------------------------------------ Claude Desktop

def test_config_files_prefer_the_msix_folder_on_windows(tmp_path):
    msix = tmp_path / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert cd.config_files(environ=env, home=tmp_path, platform="win32") == []          # Claude Desktop absent
    msix.mkdir(parents=True)
    assert cd.config_files(environ=env, home=tmp_path, platform="win32") == [msix / cd.CONFIG_NAME]
    (tmp_path / "Roaming" / "Claude").mkdir(parents=True)        # created by "Edit Config": both are written
    assert len(cd.config_files(environ=env, home=tmp_path, platform="win32")) == 2
    mac = tmp_path / "Library" / "Application Support" / "Claude"
    mac.mkdir(parents=True)
    assert cd.config_files(environ={}, home=tmp_path, platform="darwin") == [mac / cd.CONFIG_NAME]


def test_install_keeps_the_other_servers_and_a_backup(tmp_path):
    config = tmp_path / "Claude" / cd.CONFIG_NAME
    config.parent.mkdir()
    original = {"mcpServers": {"Roblox Studio": {"command": "C:\\Roblox\\StudioMCP.exe", "args": ["--stdio"]},
                               "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}},
                "preferences": {"menuBarEnabled": False}}
    config.write_text("\ufeff" + json.dumps(original), encoding="utf-8")          # BOM written by Notepad
    project = tmp_path / "game"
    project.mkdir()
    entry = {"command": "C:\\robops\\robops.exe", "args": ["mcp", "serve", "--project", str(project)]}
    [change] = cd.install(project, files=[config], entry=entry)
    assert change.status == "added" and change.backup and change.backup.read_text(encoding="utf-8-sig") == json.dumps(original)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["mcpServers"]["robops"] == entry and data["preferences"] == original["preferences"]
    assert set(data["mcpServers"]) == {"Roblox Studio", "filesystem", "robops"}
    assert cd.install(project, files=[config], entry=entry)[0].status == "unchanged"

    data["mcpServers"]["robops"]["env"] = {"ROBOPS_NO_UPDATE": "1"}                # added by hand: kept
    config.write_text(json.dumps(data), encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    new_entry = {"command": "C:\\robops\\robops.exe", "args": ["mcp", "serve", "--project", str(other)]}
    [change] = cd.install(other, files=[config], entry=new_entry)
    assert change.status == "updated" and change.previous_project == str(project)
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["robops"]["env"] == {"ROBOPS_NO_UPDATE": "1"}

    assert cd.uninstall(files=[config])[0].status == "removed"
    assert set(json.loads(config.read_text(encoding="utf-8"))["mcpServers"]) == {"Roblox Studio", "filesystem"}
    assert cd.uninstall(files=[config])[0].status == "absent"


def test_install_refuses_a_broken_config_without_touching_it(tmp_path):
    good, bad = tmp_path / "a.json", tmp_path / "b.json"
    good.write_text("{}", encoding="utf-8")
    bad.write_text('{"mcpServers": {', encoding="utf-8")
    with pytest.raises(cd.ClaudeDesktopError, match="not valid JSON"):
        cd.install(tmp_path, files=[good, bad])
    assert good.read_text(encoding="utf-8") == "{}" and bad.read_text(encoding="utf-8") == '{"mcpServers": {'
    bad.write_text('{"mcpServers": []}', encoding="utf-8")
    with pytest.raises(cd.ClaudeDesktopError, match="not an object"):
        cd.install(tmp_path, files=[bad])
    with pytest.raises(cd.ClaudeDesktopError, match="not found"):
        cd.install(tmp_path, files=[])


def test_status_finds_robops_and_roblox_studio(tmp_path):
    config = tmp_path / cd.CONFIG_NAME
    assert cd.status(tmp_path, files=[config])["installed"] is False
    cd.install(tmp_path, files=[config])                                  # this python, really there
    config_data = json.loads(config.read_text(encoding="utf-8"))
    config_data["mcpServers"]["Roblox_Studio"] = {"command": "x", "args": ["--stdio"]}
    config_data["mcpServers"]["mine"] = {"command": "C:\\tools\\rbx-studio-mcp.exe"}
    config_data["mcpServers"]["files"] = {"command": "npx", "args": ["C:\\Roblox Games"]}    # not Studio
    config.write_text(json.dumps(config_data), encoding="utf-8")
    state = cd.status(tmp_path, files=[config])
    assert state["installed"] is True and state["studio"] == ["Roblox_Studio", "mine"]
    assert state["configs"][0]["robops"]["up_to_date"] is True
    assert cd.status(tmp_path / "elsewhere", files=[config])["installed"] is False


def test_entry_starts_this_robops():
    entry = cd.server_entry(Path("."))
    assert entry["command"] == sys.executable and entry["args"][:4] == ["-m", "robops", "mcp", "serve"]
    assert Path(entry["args"][-1]).is_absolute()
    cmd = cd.claude_code_command({"command": "C:\\My Tools\\robops.exe", "args": ["mcp", "serve", "--project", "D:\\game"]})
    assert cmd == 'claude mcp add robops -- "C:\\My Tools\\robops.exe" mcp serve --project D:\\game'


# ----------------------------------------------------------------------- CLI

def test_cli_install_status_uninstall(tmp_path, monkeypatch, capsys):
    (tmp_path / "robops.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "claude" / cd.CONFIG_NAME
    assert main(["mcp", "install", "--config-file", str(config)]) == 0
    out = capsys.readouterr()
    assert "robops added to" in out.out and "Quit Claude Desktop completely" in out.out
    assert "Roblox Studio's MCP server is not in Claude Desktop's config" in out.err
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["robops"]["args"][-1] == str(tmp_path.resolve())
    assert main(["mcp", "status", "--config-file", str(config)]) == 0
    assert "installed for this project" in capsys.readouterr().out
    assert main(["mcp", "uninstall", "--config-file", str(config)]) == 0
    assert "robops removed" in capsys.readouterr().out
    config.write_text("{broken", encoding="utf-8")
    assert main(["mcp", "install", "--config-file", str(config)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


# ----------------------------------------------------------------- dashboard

def test_dashboard_setup_install_and_new_report_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBLOX_API_KEY", "k")
    app = make_dashboard(tmp_path)
    setup = app.claude_setup()
    assert setup["installed"] is False and setup["studio"] == [] and "claude mcp add robops" in setup["claude_code"]
    assert json.loads(setup["snippet"])["mcpServers"]["robops"]["args"][-1] == str(app.root)
    assert "added" in app.claude_install({})["message"]
    assert app.claude_setup()["installed"] is True
    assert "already" in app.claude_install({})["message"]
    assert '"action": "claude.install"' in (app.root / ".robops" / "audit.log").read_text(encoding="utf-8")

    before = app.playtests_stamp()["stamp"]
    server = mcp_server.McpServer(app.root, log=lambda m: None)
    call(server, "save_playtest_report", REPORT)                        # Claude saves a report…
    assert app.playtests_stamp()["stamp"] != before                     # …the page notices it…
    reports = app.playtests("staging")["reports"]
    assert [r["title"] for r in reports] == ["Tutorial"] and reports[0]["saved_at"]   # …and lists it
    assert app.playtest("staging", reports[0]["key"])["counts"]["players"] == 2

    app.claude_config_files = []
    with pytest.raises(Exception, match="Claude Desktop was not found"):
        app.claude_install({})
