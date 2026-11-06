"""Simulated players: what Claude did in Roblox Studio through the Roblox MCP.

Two sources, shown in the dashboard's "Simulated players" view:

* Reports. After simulating players in Studio, Claude saves a report (format
  "robops-playtest/1") with the MCP's run_code tool into a DataStore of the
  universe (default "RobopsPlaytests", [playtests] datastore in robops.toml).
  Reports pasted in the dashboard, or put as JSON files in playtests/, are read too.
* Claude Desktop's MCP logs (%APPDATA%\\Claude\\logs on Windows): every tool call
  Claude made to the Roblox Studio server (code run, play mode…) and its result.
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
        "place": _text(value.get("place"), 120),
        "summary": _text(value.get("summary")),
        "players": players, "findings": findings, "metrics": metrics,
        "counts": {"players": len(players), "outcomes": outcomes, "findings": by_severity},
    }


def summary(report: dict) -> dict:
    """What the list of reports shows (without players, steps…)."""
    return {k: report[k] for k in ("key", "source", "title", "started_at", "place", "counts", "recognized")}


def local_report_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in (root / "playtests", root / ".robops" / "playtests"):
        if directory.is_dir():
            files += sorted(directory.glob("*.json"))
    return files


def new_key(now: datetime | None = None) -> str:
    return "playtest-" + (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def instructions(datastore: str) -> str:
    """Text to give Claude so that its simulations show up in the dashboard."""
    return f"""\
Each time you finish simulating players in Roblox Studio (through the Roblox Studio MCP), save a report so that I can read it in the robops dashboard ("Simulated players" view).

Save it with the tool that runs Luau code in Studio (run_code), in edit mode (play mode stopped), with this code, after replacing REPORT by the real results:

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
_SERVER_RE = re.compile(r"\[([^\]]+)\]")
_STUDIO_RE = re.compile(r"roblox|studio|rbx", re.IGNORECASE)
_DECODER = json.JSONDecoder()
MAX_LOG_BYTES = 8 * 1024 * 1024
SESSION_GAP = timedelta(minutes=30)


def claude_log_dirs() -> list[Path]:
    """Where Claude Desktop writes its MCP logs (Windows, macOS, Linux)."""
    dirs = []
    if os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "Claude" / "logs")
    home = Path.home()
    dirs += [home / "AppData" / "Roaming" / "Claude" / "logs", home / "Library" / "Logs" / "Claude",
             home / ".config" / "Claude" / "logs"]
    unique: list[Path] = []
    for d in dirs:
        if d not in unique:
            unique.append(d)
    return unique


def studio_log_files(directory: Path) -> list[Path]:
    """The Roblox Studio server's own log files, otherwise the shared mcp.log."""
    if not directory.is_dir():
        return []
    own = sorted(p for p in directory.glob("mcp*.log*") if p.is_file() and _STUDIO_RE.search(p.name))
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


def _first_json(line: str) -> dict | None:
    start = line.find("{")
    while start != -1:
        try:
            obj, _ = _DECODER.raw_decode(line, start)
        except ValueError:
            start = line.find("{", start + 1)
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


def parse_log_lines(lines: list[str], *, shared: bool = False) -> list[dict]:
    """Tool calls (request + response paired by id) found in Claude Desktop MCP log lines."""
    pending: dict[Any, dict] = {}
    calls: list[dict] = []
    for line in lines:
        if "jsonrpc" not in line:
            continue
        servers = _SERVER_RE.findall(line)
        server = next((s for s in servers if s.lower() not in ("info", "debug", "warn", "warning", "error")), None)
        if shared and not (server and _STUDIO_RE.search(server)):
            continue                                           # mcp.log mixes every server
        msg = _first_json(line)
        if not msg or msg.get("jsonrpc") != "2.0":
            continue
        time = _when(line)
        if msg.get("method") == "tools/call" and "id" in msg:
            raw_params = msg.get("params")
            params: dict = raw_params if isinstance(raw_params, dict) else {}
            call = {"id": msg["id"], "time": time, "server": server, "tool": str(params.get("name", "?")),
                    "arguments": params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
                    "result": None, "is_error": None, "duration_ms": None}
            pending[msg["id"]] = call
            calls.append(call)
        elif "id" in msg and ("result" in msg or "error" in msg) and msg["id"] in pending:
            call = pending.pop(msg["id"])
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]}
                call["result"], call["is_error"] = str(err.get("message", err)), True
            else:
                call["result"] = _result_text(msg["result"])
                call["is_error"] = bool(isinstance(msg["result"], dict) and msg["result"].get("isError"))
            if time and call["time"]:
                call["duration_ms"] = int((time - call["time"]).total_seconds() * 1000)
    return calls


def studio_activity(directories: list[Path], *, max_sessions: int = 20, max_text: int = 20000) -> dict:
    """Claude's tool calls to Roblox Studio, grouped in sessions (newest first)."""
    files: list[Path] = []
    for directory in directories:
        files = studio_log_files(directory)
        if files:
            break
    calls: list[dict] = []
    for path in files:
        try:
            calls += parse_log_lines(_tail_lines(path), shared=path.name == "mcp.log")
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
                "arguments": {k: _text(v, max_text) for k, v in call["arguments"].items()},
                "result": _text(call["result"], max_text), "is_error": call["is_error"],
                "duration_ms": call["duration_ms"]}

    out = []
    for group in reversed(sessions[-max_sessions:]):
        tools: dict[str, int] = {}
        for c in group:
            tools[c["tool"]] = tools.get(c["tool"], 0) + 1
        out.append({"start": group[0]["time"].isoformat(timespec="seconds"),
                    "end": group[-1]["time"].isoformat(timespec="seconds"),
                    "calls": [clean(c) for c in group[-300:]], "count": len(group),
                    "errors": sum(1 for c in group if c["is_error"]), "tools": tools})
    return {"checked": [str(d) for d in directories], "files": [str(f) for f in files], "sessions": out}
