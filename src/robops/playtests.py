"""Simulated players: what Claude did in Roblox Studio through the Roblox MCP.

Two sources, shown in the dashboard's "Simulated players" view:

* Reports (format "robops-playtest/1"). Claude saves them with the robops MCP
  server's save_playtest_report tool (robops mcp install adds it to Claude
  Desktop) into .robops/playtests/ of the project. Without it, Claude can save
  them with Studio's run_code into a DataStore of the universe (default
  "RobopsPlaytests", [playtests] datastore in robops.toml). Reports pasted in the
  dashboard, or put as JSON files in playtests/, are read too.
* Claude Desktop's MCP logs: when Claude called the Roblox Studio server and
  whether it worked. Recent Claude Desktop versions only log a summary of each
  message (method="tools/call" id=4, id=4 result(1 blocks)); older ones logged
  the whole JSON-RPC message, with the tool name, its arguments and the answer.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPORT_FORMAT = "robops-playtest/1"
MAX_TEXT = 4000
OUTCOMES = {
    "completed": "completed", "complete": "completed", "finished": "completed", "success": "completed",
    "succeeded": "completed", "done": "completed", "won": "completed",
    "stuck": "stuck", "blocked": "stuck", "lost": "stuck",
    "quit": "quit", "left": "quit", "abandoned": "quit", "gave up": "quit",
    "error": "error", "crash": "error", "crashed": "error", "failed": "error", "bug": "error",
}
SEVERITIES = {"critical": "high", "blocker": "high", "high": "high", "major": "high",
              "medium": "medium", "moderate": "medium", "normal": "medium",
              "low": "low", "minor": "low", "info": "low", "suggestion": "low"}


# ------------------------------------------------------------------ reports

def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def is_report(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("format") == REPORT_FORMAT or isinstance(value.get("players"), list)
        or isinstance(value.get("findings"), list))


def normalize_report(key: str, value: Any, source: str) -> dict:
    """A report in a predictable shape, whatever Claude actually wrote (tolerant)."""
    if not isinstance(value, dict):
        value = {"summary": _text(value)}
    players: list[dict[str, Any]] = []
    for i, raw in enumerate(_list(value.get("players"))):
        if not isinstance(raw, dict):
            raw = {"feedback": raw}
        outcome = str(raw.get("outcome") or "").strip().lower()
        steps = []
        for step in _list(raw.get("steps"))[:200]:
            if isinstance(step, dict):
                steps.append({"t": _number(step.get("t")) if _number(step.get("t")) is not None else _text(step.get("t"), 40),
                              "action": _text(step.get("action"), 500) or "", "result": _text(step.get("result"), 500)})
            else:
                steps.append({"t": None, "action": _text(step, 500) or "", "result": None})
        players.append({
            "name": _text(raw.get("name"), 80) or f"Player {i + 1}",
            "profile": _text(raw.get("profile") or raw.get("persona"), 300),
            "outcome": OUTCOMES.get(outcome, "other" if outcome else "unknown"),
            "outcome_text": _text(raw.get("outcome"), 80),
            "duration_s": _number(raw.get("duration_s") or raw.get("duration")),
            "feedback": _text(raw.get("feedback")),
            "issues": [t for t in (_text(x, 500) for x in _list(raw.get("issues"))) if t],
            "steps": steps,
        })
    findings: list[dict[str, Any]] = []
    for raw in _list(value.get("findings")):
        if not isinstance(raw, dict):
            raw = {"title": raw}
        severity = str(raw.get("severity") or "").strip().lower()
        findings.append({
            "severity": SEVERITIES.get(severity, "low"),
            "title": _text(raw.get("title"), 300) or "(untitled)",
            "detail": _text(raw.get("detail")),
            "player": _text(raw.get("player"), 80),
            "where": _text(raw.get("where"), 300),
        })
    findings.sort(key=lambda f: ["high", "medium", "low"].index(f["severity"]))
    metrics = {}
    if isinstance(value.get("metrics"), dict):
        for name, metric in list(value["metrics"].items())[:50]:
            metrics[str(name)[:80]] = metric if isinstance(metric, (int, float, str)) and not isinstance(metric, bool) \
                else _text(metric, 200)
    outcomes: dict[str, int] = {}
    for player in players:
        outcomes[player["outcome"]] = outcomes.get(player["outcome"], 0) + 1
    by_severity = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}
    return {
        "key": key, "source": source,
        "recognized": is_report(value),
        "title": _text(value.get("title"), 200) or key,
        "started_at": _text(value.get("started_at"), 40),
        "ended_at": _text(value.get("ended_at"), 40),
        "saved_at": _text(value.get("saved_at"), 40),
        "saved_by": _text(value.get("saved_by"), 120),
        "place": _text(value.get("place"), 120),
        "summary": _text(value.get("summary")),
        "players": players, "findings": findings, "metrics": metrics,
        "counts": {"players": len(players), "outcomes": outcomes, "findings": by_severity},
    }


def summary(report: dict) -> dict:
    """What the list of reports shows (without players, steps…)."""
    return {k: report[k] for k in ("key", "source", "title", "started_at", "saved_at", "saved_by", "place", "counts",
                                   "recognized")}


_KEY_TIME_RE = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d)[:-](\d\d)[:-](\d\d(?:\.\d+)?)Z?")


def report_date(report: dict) -> str:
    """When the simulation ran: started_at, else saved_at, else the time in the key; "" if unknown."""
    if report.get("started_at") or report.get("saved_at"):
        return str(report.get("started_at") or report.get("saved_at"))
    found = _KEY_TIME_RE.search(str(report.get("key", "")))
    return f"{found.group(1)}T{found.group(2)}:{found.group(3)}:{found.group(4)}Z" if found else ""


def sort_reports(reports: list[dict]) -> list[dict]:
    """Newest first; undated ones last."""
    return sorted(reports, key=lambda r: (report_date(r) != "", report_date(r)), reverse=True)


def local_report_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in (root / "playtests", root / ".robops" / "playtests"):
        if directory.is_dir():
            files += sorted(directory.glob("*.json"))
    return files


def local_reports(root: Path) -> dict[str, Path]:
    """The project's report files by key ("file:<path relative to the project>")."""
    out = {}
    for path in local_report_files(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        out["file:" + rel] = path
    return out


def local_stamp(root: Path) -> str:
    """Changes when a local report is added, changed or removed (cheap: no file is read)."""
    parts = []
    for path in local_report_files(root):
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")
    return f"{len(parts)}|" + "|".join(parts)[-2000:]


def new_key(now: datetime | None = None) -> str:
    return "playtest-" + (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def save_local_report(root: Path, report: dict, *, saved_by: str, now: datetime | None = None) -> tuple[str, Path]:
    """Writes a report into <root>/.robops/playtests/ (read by the dashboard). Returns (key, file)."""
    now = now or datetime.now(UTC)
    data = {"format": REPORT_FORMAT, **report}
    data.setdefault("saved_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    data.setdefault("saved_by", saved_by)
    folder = root / ".robops" / "playtests"
    folder.mkdir(parents=True, exist_ok=True)
    stem = new_key(now).replace(":", "-")
    path, n = folder / f"{stem}.json", 1
    while path.exists():
        path, n = folder / f"{stem}-{n}.json", n + 1
    tmp = folder / f".{path.name}.tmp"                    # never a half-written report in the list
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return "file:" + path.relative_to(root).as_posix(), path


def instructions(datastore: str) -> str:
    """Text to give Claude so that its simulations show up in the dashboard."""
    return f"""\
Each time you finish simulating players in Roblox Studio (through the Roblox Studio MCP), save a report so that I can read it in the robops dashboard ("Simulated players" view).

If you have the robops tool save_playtest_report, use it and ignore the rest of this message. Otherwise, save it with the tool that runs Luau code in Studio (run_code), in edit mode (play mode stopped), with this code, after replacing REPORT by the real results:

local DataStoreService = game:GetService("DataStoreService")
local REPORT = {{
\tformat = "{REPORT_FORMAT}",
\ttitle = "What was tested, e.g. Tutorial with 5 new players",
\tstarted_at = "2026-11-06T21:30:00Z", -- ISO 8601 UTC, DateTime.now():ToIsoDate()
\tended_at = "2026-11-06T21:42:00Z",
\tplace = "Name of the place tested",
\tsummary = "2 to 5 sentences: what worked, what did not.",
\tplayers = {{
\t\t{{
\t\t\tname = "Sim_1",
\t\t\tprofile = "Who this player imitates, e.g. new player on mobile",
\t\t\toutcome = "completed", -- completed | stuck | quit | error
\t\t\tduration_s = 312,
\t\t\tfeedback = "What this player would say about the game.",
\t\t\tissues = {{ "Could not find the shop" }},
\t\t\tsteps = {{ {{ t = 0, action = "Spawned", result = "OK" }} }}, -- at most 30 steps
\t\t}},
\t}},
\tfindings = {{
\t\t{{ severity = "high", title = "Short title", detail = "What happens and how to reproduce it", player = "Sim_1", where = "ServerScriptService.Shop" }},
\t}}, -- severity: high | medium | low
\tmetrics = {{ coins_earned_avg = 120 }}, -- optional numbers
}}
local key = "playtest-" .. DateTime.now():ToIsoDate()
DataStoreService:GetDataStore("{datastore}"):SetAsync(key, REPORT)
print("robops report saved: " .. key)

Rules:
- Keep the field names above; the texts can be in any language.
- Leave out a field rather than setting it to nil, and keep the report under 100 KB.
- Studio needs "Enable Studio Access to API Services" (Game Settings > Security) and a published place. The report goes to the universe of the place open in Studio.
- If SetAsync fails, show me the error instead of retrying in a loop.
"""


# --------------------------------------------------------- Claude Desktop logs

_TIME_RE = re.compile(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)")
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_LEVELS = {"info", "debug", "warn", "warning", "error", "trace", "log"}
_STUDIO_RE = re.compile(r"roblox|studio|rbx", re.IGNORECASE)
_MESSAGE_RE = re.compile(r"Message from (client|server):\s?(.*)$")
# Summarized messages (Claude Desktop 2026):  method="tools/call" id=4 params   /   id=4 result(1 blocks)
_SUMMARY_REQUEST_RE = re.compile(r'^method="([^"]+)"(?:\s+id=(\S+))?')
_SUMMARY_ANSWER_RE = re.compile(r"^id=(\S+)\s+(result|error)\b(?:\(([^)]*)\))?")
_CONNECTION_END = ("Shutting down server", "Server transport closed", "Server disconnected", "Server started")
_DECODER = json.JSONDecoder()
MAX_LOG_BYTES = 8 * 1024 * 1024
SESSION_GAP = timedelta(minutes=30)


def claude_log_dirs() -> list[Path]:
    """Where Claude Desktop writes its MCP logs (Windows, including the MSIX install, macOS, Linux)."""
    from . import claude_desktop

    return claude_desktop.log_dirs()


def studio_log_files(directory: Path, names: tuple[str, ...] | list[str] = ()) -> list[Path]:
    """The Roblox Studio server's own log files, otherwise the shared mcp.log."""
    if not directory.is_dir():
        return []
    wanted = {f"mcp-server-{n}.log".lower() for n in names}
    own = sorted(p for p in directory.glob("mcp*.log*") if p.is_file() and p.name != "mcp.log"
                 and (_STUDIO_RE.search(p.name) or p.name.lower() in wanted))
    if own:
        return own
    shared = directory / "mcp.log"
    return [shared] if shared.is_file() else []


def _tail_lines(path: Path, max_bytes: int = MAX_LOG_BYTES) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - max_bytes))
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines            # the first line may be cut


def _first_json(text: str) -> dict | None:
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = _DECODER.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _when(line: str) -> datetime | None:
    m = _TIME_RE.search(line)
    if not m:
        return None
    try:
        t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        parts = []
        for item in _list(result.get("content")):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(f"[{item.get('type', 'content')}]")
        if parts:
            return "\n".join(parts)
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _server_of(line: str, before: int) -> str | None:
    """The server's name: the [bracket] before the message that is not a log level.

    Per-server files write "[Roblox Studio] [info]", mcp.log writes "[info] [Roblox Studio]".
    """
    for name in _BRACKET_RE.findall(line[:before]):
        if name.strip().lower() not in _LEVELS:
            return name.strip()
    return None


def _id(raw: Any) -> str:
    return str(raw).strip().strip('"').rstrip(",")


class _Calls:
    """Pairs tool calls with their answers, per connection (ids restart at 0 on each connection)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.pending: dict[tuple[str | None, str], dict] = {}

    def reset(self, server: str | None) -> None:
        for key in [k for k in self.pending if k[0] == server]:
            del self.pending[key]

    def request(self, server: str | None, msg_id: Any, time: datetime | None, tool: str | None,
                arguments: dict, detailed: bool) -> None:
        call = {"time": time, "server": server, "tool": tool, "arguments": arguments, "detailed": detailed,
                "result": None, "is_error": None, "duration_ms": None}
        self.pending[(server, _id(msg_id))] = call
        self.calls.append(call)

    def answer(self, server: str | None, msg_id: Any, time: datetime | None, *, result: str | None,
               is_error: bool) -> None:
        call = self.pending.pop((server, _id(msg_id)), None)
        if call is None:
            return
        call["result"], call["is_error"] = result, is_error
        if time and call["time"]:
            call["duration_ms"] = max(0, int((time - call["time"]).total_seconds() * 1000))

    def json_message(self, server: str | None, msg: dict, time: datetime | None) -> None:
        method = msg.get("method")
        if method == "initialize":
            self.reset(server)
        if method == "tools/call" and "id" in msg:
            raw = msg.get("params")
            params: dict = raw if isinstance(raw, dict) else {}
            raw = params.get("arguments")
            arguments: dict = raw if isinstance(raw, dict) else {}
            self.request(server, msg["id"], time, str(params.get("name") or "?"), arguments, True)
        elif method is None and "id" in msg and ("result" in msg or "error" in msg):
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]}
                self.answer(server, msg["id"], time, result=str(err.get("message", err)), is_error=True)
            else:
                res = msg["result"]
                self.answer(server, msg["id"], time, result=_result_text(res),
                            is_error=bool(isinstance(res, dict) and res.get("isError")))


def parse_log_lines(lines: list[str], *, shared: bool = False, servers: tuple[str, ...] | list[str] = ()) -> list[dict]:
    """Tool calls (request + answer paired by id) found in Claude Desktop MCP log lines.

    shared: lines from mcp.log, which mixes every server: only the Roblox Studio one is kept
    (its name is in servers, or contains roblox/studio/rbx).
    """
    wanted = {s.lower() for s in servers}

    def is_studio(server: str | None) -> bool:
        return server is not None and (server.lower() in wanted or bool(_STUDIO_RE.search(server)))

    found = _Calls()
    messages = 0
    for line in lines:
        m = _MESSAGE_RE.search(line)
        if not m:
            if any(marker in line for marker in _CONNECTION_END):
                found.reset(_server_of(line, len(line)))
            continue
        server = _server_of(line, m.start())
        if shared and not is_studio(server):
            continue
        messages += 1
        direction, rest = m.group(1), m.group(2).strip()
        time = _when(line)
        if rest.startswith("{"):                            # whole JSON-RPC message (older versions)
            msg = _first_json(rest)
            if msg:
                found.json_message(server, msg, time)
            continue
        if direction == "client":
            req = _SUMMARY_REQUEST_RE.match(rest)
            if not req:
                continue
            if req.group(1) == "initialize":
                found.reset(server)
            elif req.group(1) == "tools/call" and req.group(2):
                found.request(server, req.group(2), time, None, {}, False)
        else:
            ans = _SUMMARY_ANSWER_RE.match(rest)
            if not ans:
                continue
            detail = ans.group(3) or ""
            failed = ans.group(2) == "error" or "error" in detail.lower()
            found.answer(server, ans.group(1), time, is_error=failed,
                         result=f"Error ({detail})" if ans.group(2) == "error" and detail else None)
    if messages or found.calls:
        return found.calls
    # No "Message from" line: some servers log their own JSON-RPC traffic in their file.
    for line in lines:
        if "jsonrpc" not in line:
            continue
        server = _server_of(line, line.find("{") if "{" in line else len(line))
        if shared and not is_studio(server):
            continue
        msg = _first_json(line)
        if msg and msg.get("jsonrpc") == "2.0":
            found.json_message(server, msg, _when(line))
    return found.calls


def _newest(files: list[Path]) -> float:
    times = []
    for f in files:
        try:
            times.append(f.stat().st_mtime)
        except OSError:
            pass
    return max(times, default=0.0)


def studio_activity(directories: list[Path], *, servers: tuple[str, ...] | list[str] = (),
                    max_sessions: int = 20, max_text: int = 20000) -> dict:
    """Claude's tool calls to Roblox Studio, grouped in sessions (newest first).

    servers: names of the Roblox Studio server in Claude Desktop's config, if known.
    """
    candidates = [(d, studio_log_files(d, servers)) for d in directories]
    candidates = [(d, f) for d, f in candidates if f]
    files: list[Path] = max(candidates, key=lambda c: _newest(c[1]))[1] if candidates else []
    calls: list[dict] = []
    for path in files:
        try:
            calls += parse_log_lines(_tail_lines(path), shared=path.name == "mcp.log", servers=servers)
        except OSError:
            continue
    calls = [c for c in calls if c["time"] is not None]
    calls.sort(key=lambda c: c["time"])
    sessions: list[list[dict]] = []
    for call in calls:
        if sessions and call["time"] - sessions[-1][-1]["time"] <= SESSION_GAP:
            sessions[-1].append(call)
        else:
            sessions.append([call])

    def clean(call: dict) -> dict:
        return {"time": call["time"].isoformat(timespec="seconds"), "server": call["server"], "tool": call["tool"],
                "detailed": call["detailed"],
                "arguments": {k: _text(v, max_text) for k, v in call["arguments"].items()},
                "result": _text(call["result"], max_text), "is_error": call["is_error"],
                "duration_ms": call["duration_ms"]}

    out = []
    for group in reversed(sessions[-max_sessions:]):
        tools: dict[str, int] = {}
        for c in group:
            if c["tool"]:
                tools[c["tool"]] = tools.get(c["tool"], 0) + 1
        out.append({"start": group[0]["time"].isoformat(timespec="seconds"),
                    "end": group[-1]["time"].isoformat(timespec="seconds"),
                    "calls": [clean(c) for c in group[-300:]], "count": len(group),
                    "errors": sum(1 for c in group if c["is_error"]), "tools": tools,
                    "detailed": any(c["detailed"] for c in group)})
    return {"checked": [str(d) for d in directories], "files": [str(f) for f in files], "sessions": out,
            "detailed": any(s["detailed"] for s in out)}
