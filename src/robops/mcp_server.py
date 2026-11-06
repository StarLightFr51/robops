"""robops MCP server: lets Claude save its player simulation reports into the project.

Claude Desktop (or Claude Code) starts it:  robops mcp serve --project <game folder>
`robops mcp install` adds that line to Claude Desktop's config.

Tools: save_playtest_report (writes .robops/playtests/<key>.json, shown by the
dashboard's "Simulated players" view), list_playtest_reports and
get_playtest_report (to compare with earlier simulations). Prompt:
simulate_players, the method to follow, offered in Claude Desktop's "+" menu.

Protocol: MCP stdio transport, JSON-RPC 2.0, one message per line on
stdin/stdout. Nothing else may be written on stdout: logs go to stderr, which
Claude Desktop keeps in mcp-server-robops.log. No dependency.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import IO, Any

from . import __version__
from . import playtests as pt

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")    # newest first
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
MAX_REPORT_BYTES = 1_000_000

INSTRUCTIONS = (
    "robops keeps the reports of player simulations for the developer's robops dashboard. "
    "When you simulate players in Roblox Studio (with the Roblox Studio MCP tools), call save_playtest_report "
    "once at the end with the results. list_playtest_reports and get_playtest_report read earlier reports, "
    "for example to check whether a problem found last time is fixed."
)

# ------------------------------------------------------------------ tools

_STEP = {"type": "object", "properties": {
    "t": {"type": "number", "description": "Seconds since the player joined."},
    "action": {"type": "string", "description": "What the player did."},
    "result": {"type": "string", "description": "What happened."},
}, "required": ["action"]}

_PLAYER = {"type": "object", "properties": {
    "name": {"type": "string", "description": "e.g. Sim_1"},
    "profile": {"type": "string", "description": "Who this player imitates, e.g. new player on mobile, "
                                                 "experienced player who rushes, player who reads nothing."},
    "outcome": {"type": "string", "enum": ["completed", "stuck", "quit", "error"],
                "description": "completed: reached the goal; stuck: could not go on; quit: would have left; "
                               "error: a script error stopped them."},
    "duration_s": {"type": "number", "description": "How long the player played, in seconds."},
    "feedback": {"type": "string", "description": "What this player would say about the game, in their words."},
    "issues": {"type": "array", "items": {"type": "string"}, "description": "Problems this player met."},
    "steps": {"type": "array", "items": _STEP, "description": "What the player did, in order (at most 30 steps)."},
}, "required": ["name", "outcome", "feedback"]}

_FINDING = {"type": "object", "properties": {
    "severity": {"type": "string", "enum": ["high", "medium", "low"],
                 "description": "high: blocks or breaks the game; medium: hurts the experience; low: polish."},
    "title": {"type": "string"},
    "detail": {"type": "string", "description": "What happens, how to reproduce it, and the fix you suggest."},
    "player": {"type": "string", "description": "Name of the simulated player who met it."},
    "where": {"type": "string", "description": "Script or instance path, e.g. ServerScriptService.Shop"},
}, "required": ["severity", "title"]}

REPORT_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string", "description": "What was tested, e.g. Tutorial with 5 new players."},
    "summary": {"type": "string", "description": "2 to 5 sentences: what worked, what did not."},
    "place": {"type": "string", "description": "Name of the place tested."},
    "started_at": {"type": "string", "description": "ISO 8601 UTC, e.g. 2026-11-06T21:30:00Z"},
    "ended_at": {"type": "string", "description": "ISO 8601 UTC."},
    "players": {"type": "array", "items": _PLAYER, "description": "One entry per simulated player."},
    "findings": {"type": "array", "items": _FINDING, "description": "Every problem found, one entry each."},
    "metrics": {"type": "object", "description": "Optional measures, e.g. {\"coins_earned_avg\": 120}."},
}, "required": ["title", "summary", "players"]}

# Only name, description and inputSchema: some clients refuse tools that declare more (outputSchema…).
TOOLS = [
    {"name": "save_playtest_report",
     "description": "Save the report of a player simulation done in Roblox Studio, so the developer reads it in "
                    "the robops dashboard (Simulated players view). Call it once at the end of each simulation, "
                    "after stopping play mode, with what each simulated player did and every problem found. "
                    "Texts can be in any language.",
     "inputSchema": REPORT_SCHEMA},
    {"name": "list_playtest_reports",
     "description": "The latest saved player simulation reports, newest first: key, title, date, players and "
                    "problems found. Use it to compare with earlier simulations.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many (default 10)."}}}},
    {"name": "get_playtest_report",
     "description": "One saved player simulation report in full (players, steps, problems found).",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "The report's key, from list_playtest_reports."}},
         "required": ["key"]}},
]

PROMPTS = [
    {"name": "simulate_players",
     "description": "Simulate players on the game open in Roblox Studio and save a report for the robops dashboard.",
     "arguments": [
         {"name": "players", "description": "How many players to simulate (default 5).", "required": False},
         {"name": "focus", "description": "What to test, e.g. the tutorial, the shop, the first 10 minutes.",
          "required": False},
     ]},
]


def simulate_prompt(players: str | None = None, focus: str | None = None) -> str:
    count = (players or "").strip() or "5"
    target = f", focusing on: {focus.strip()}" if focus and focus.strip() else ""
    return f"""\
Simulate {count} players on the game open in Roblox Studio{target}, with the Roblox Studio MCP tools, then save a report with the robops tool save_playtest_report.

1. First look at the game (explore the place and its scripts with the Studio tools) to know what a player can do and what the goal is.
2. Give each simulated player a different profile, for example: a player new to Roblox, a player on mobile, an experienced player who rushes, a player who reads nothing, a player who tries to break things.
3. For each player: start play mode, drive their character with code run in play mode (move, jump, touch parts, use buttons and ProximityPrompts, fire the remotes a real client would fire), read the console output for errors, then stop play mode. Act as that player would: no developer knowledge they would not have.
4. Note for each one what they did step by step (at most 30 steps), how it ended (completed, stuck, quit or error), how long it took, what they would say about the game and the problems they met.
5. Group the problems as findings, each with a severity (high: blocks or breaks the game, medium: hurts the experience, low: polish), how to reproduce it and where in the game (script or instance path).
6. Do not change the game's scripts during the simulation: put the fixes you suggest in the findings' detail.
7. Stop play mode, then call save_playtest_report once with everything. Tell me the key it returns and the 3 most important problems.
"""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ToolError(Exception):
    """A tool that could not do its job: returned to Claude as an error result (isError)."""


def _decode_nested(report: dict) -> dict:
    """Some clients send nested lists/objects as JSON text: decode them."""
    out = dict(report)
    for field in ("players", "findings", "metrics"):
        value = out.get(field)
        if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
            try:
                out[field] = json.loads(value)
            except ValueError:
                pass
    return out


class McpServer:
    def __init__(self, project: Path, *, log: Callable[[str], None] | None = None):
        self.project = project
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.client = "?"

    # ------------------------------------------------------------ protocol

    def handle_line(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            return _dump(_error(None, PARSE_ERROR, "Parse error: not JSON"))
        if isinstance(message, list):                           # batch (protocol 2025-03-26)
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return _dump(replies) if replies else None
        reply = self.handle(message)
        return _dump(reply) if reply is not None else None

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid request: not a JSON object")
        method = message.get("method")
        if not isinstance(method, str) or "id" not in message:
            return None                 # a notification, or an answer to a request this server never sends
        msg_id = message["id"]
        params = _obj(message.get("params"))
        handlers: dict[str, Callable[[dict], dict]] = {
            "initialize": self.initialize, "ping": lambda p: {},
            "tools/list": lambda p: {"tools": TOOLS}, "tools/call": self.call_tool,
            "prompts/list": lambda p: {"prompts": PROMPTS}, "prompts/get": self.get_prompt,
        }
        handler = handlers.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": handler(params)}
        except RpcError as exc:
            return _error(msg_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — the server must keep running
            self.log(traceback.format_exc())
            return _error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        info = _obj(params.get("clientInfo"))
        self.client = f"{info.get('name', '?')} {info.get('version', '')}".strip()
        self.log(f"Connected: {self.client}, protocol {version} (asked: {asked}).")
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                "serverInfo": {"name": "robops", "version": __version__},
                "instructions": INSTRUCTIONS}

    def get_prompt(self, params: dict) -> dict:
        if params.get("name") != "simulate_players":
            raise RpcError(INVALID_PARAMS, f"Unknown prompt: {params.get('name')}")
        args = _obj(params.get("arguments"))
        text = simulate_prompt(_opt(args.get("players")), _opt(args.get("focus")))
        return {"description": PROMPTS[0]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = _obj(params.get("arguments"))
        tools: dict[str, Callable[[dict], str]] = {
            "save_playtest_report": self.save_report, "list_playtest_reports": self.list_reports,
            "get_playtest_report": self.get_report,
        }
        tool = tools.get(str(name))
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            text = tool(args)
        except ToolError as exc:
            self.log(f"{name}: {exc}")
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": text}]}

    # --------------------------------------------------------------- tools

    def save_report(self, args: dict) -> str:
        report = args["report"] if isinstance(args.get("report"), dict) else args    # {"report": {...}} too
        report = _decode_nested(report)
        if not pt.is_report(report):
            raise ToolError("Not saved: the report needs a \"players\" list (one entry per simulated player) "
                            "and/or a \"findings\" list. Call save_playtest_report again with them.")
        size = len(json.dumps(report, ensure_ascii=False).encode("utf-8"))
        if size > MAX_REPORT_BYTES:
            raise ToolError(f"Not saved: the report is {size // 1000} KB, the limit is {MAX_REPORT_BYTES // 1000} KB. "
                            "Keep at most 30 steps per player and shorten the texts, then call it again.")
        try:
            key, path = pt.save_local_report(self.project, report, saved_by=f"robops mcp ({self.client})")
        except OSError as exc:
            raise ToolError(f"Not saved: cannot write in {self.project / '.robops' / 'playtests'}: {exc}") from exc
        rep = pt.normalize_report(key, report, "file")
        c = rep["counts"]
        outcomes = ", ".join(f"{n} {k}" for k, n in c["outcomes"].items()) or "no player"
        problems = sum(c["findings"].values())
        self.log(f"Report saved: {path}")
        return (f"Report saved (key: {key}): {c['players']} player(s) ({outcomes}), {problems} problem(s) found.\n"
                f"File: {path}\n"
                "The developer sees it in the robops dashboard, Simulated players view (it appears by itself).")

    def _summaries(self) -> list[dict]:
        reports = []
        for key, path in pt.local_reports(self.project).items():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            reports.append(pt.summary(pt.normalize_report(key, value, "file")))
        return pt.sort_reports(reports)

    def list_reports(self, args: dict) -> str:
        try:
            limit = max(1, min(50, int(args.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10
        reports = self._summaries()
        if not reports:
            return "No report saved yet in this project."
        items = [{"key": r["key"], "title": r["title"], "date": pt.report_date(r) or None, "place": r["place"],
                  "players": r["counts"]["players"], "outcomes": r["counts"]["outcomes"],
                  "problems": r["counts"]["findings"]} for r in reports[:limit]]
        return json.dumps({"total": len(reports), "reports": items}, ensure_ascii=False, indent=2)

    def get_report(self, args: dict) -> str:
        key = str(args.get("key") or "").strip()
        files = pt.local_reports(self.project)
        path = files.get(key) or files.get("file:" + key)
        if path is None:
            raise ToolError(f"No report with the key {key!r}: use list_playtest_reports to get the keys.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(f"Cannot read {path.name}: {exc}") from exc
        return json.dumps(pt.normalize_report(key, value, "file"), ensure_ascii=False, indent=2)


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _dump(message: Any) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def serve(project: Path, *, stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None) -> int:
    """Answers the MCP client on stdin/stdout until it closes stdin."""
    stdin = stdin or sys.stdin.buffer
    protocol = stdout or sys.stdout.buffer
    server = McpServer(project)
    server.log(f"robops {__version__} MCP server, project: {project}")
    # Anything printed by mistake goes to stderr: one stray line on stdout breaks the connection.
    with redirect_stdout(sys.stderr):
        while True:
            raw = stdin.readline()
            if not raw:
                break
            reply = server.handle_line(raw.decode("utf-8", errors="replace"))
            if reply is not None:
                protocol.write(reply.encode("utf-8") + b"\n")
                protocol.flush()
    server.log("Client disconnected.")
    return 0
