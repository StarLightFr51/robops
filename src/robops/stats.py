"""Data behind the dashboard's charts.

* Live history: roblox.com only gives *current* numbers (players online, visits…),
  so robops records a sample every few minutes while the dashboard runs, or each
  time `robops stats record` runs (e.g. from the Windows Task Scheduler), in
  .robops/stats/<environment>.jsonl.
* Player base: computed from a player backup (robops player backup): new players
  and last activity per day, sessions per player, and the distribution of every
  numeric field of the players' Data.
* Deployments: runs of the deploy workflow per week and their duration.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import statistics
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import players as pl
from .roblox_web import RobloxWeb

SAMPLE_FIELDS = ("playing", "visits", "favorites", "up_votes", "down_votes", "servers")
RANGES = {"6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
MAX_POINTS = 360
MAX_FIELDS = 12
_HEADER_RE = re.compile(r'"(universe_id|created_at|format)":\s*("[^"]*"|\d+)')
_SAFE_NAME = re.compile(r"[^\w.-]")


# ------------------------------------------------------------- live history

class History:
    """Append-only samples per environment (JSON lines, one per sample)."""

    def __init__(self, root: Path):
        self.dir = root / ".robops" / "stats"
        self._lock = threading.Lock()

    def path(self, env: str) -> Path:
        return self.dir / f"{_SAFE_NAME.sub('_', env)}.jsonl"

    def append(self, env: str, sample: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path(env).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    def read(self, env: str, since: float | None = None) -> list[dict]:
        path = self.path(env)
        if not path.is_file():
            return []
        samples = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    sample = json.loads(line)
                except ValueError:
                    continue                      # a line cut by a crash: skip it
                if isinstance(sample, dict) and isinstance(sample.get("t"), (int, float)) \
                        and (since is None or sample["t"] >= since):
                    samples.append(sample)
        return samples

    def last_time(self, env: str) -> float | None:
        samples = self.read(env)
        return samples[-1]["t"] if samples else None


def take_sample(web: RobloxWeb, universe_id: int, now: float | None = None) -> dict:
    game = web.game(universe_id)
    sample: dict[str, Any] = {"t": round(now if now is not None else time.time())}
    for key in ("playing", "visits", "favorites", "up_votes", "down_votes"):
        sample[key] = game.get(key)
    try:
        sample["servers"] = len(web.servers(int(game["root_place_id"]))) if game.get("root_place_id") else None
    except Exception:  # noqa: BLE001 — the server count is a bonus, never lose the sample for it
        sample["servers"] = None
    return sample


def downsample(samples: list[dict], max_points: int = MAX_POINTS) -> list[dict]:
    """At most max_points samples: averages within each time bucket (players, servers)
    and the last value for running totals (visits, favorites, votes)."""
    if len(samples) <= max_points:
        return samples
    start, end = samples[0]["t"], samples[-1]["t"]
    width = (end - start) / max_points or 1
    buckets: dict[int, list[dict]] = {}
    for s in samples:
        buckets.setdefault(min(int((s["t"] - start) / width), max_points - 1), []).append(s)
    result = []
    for group in buckets.values():
        merged: dict[str, Any] = {"t": round(sum(s["t"] for s in group) / len(group))}
        for key in ("playing", "servers"):
            values = [s[key] for s in group if isinstance(s.get(key), (int, float))]
            merged[key] = round(sum(values) / len(values), 1) if values else None
        for key in ("visits", "favorites", "up_votes", "down_votes"):
            merged[key] = next((s[key] for s in reversed(group) if s.get(key) is not None), None)
        result.append(merged)
    return result


# ------------------------------------------------------------- player base

def backup_header(path: Path) -> dict:
    """universe_id / created_at of a `robops player backup` file, read from its first bytes."""
    try:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            head = fh.read(4096)
    except (OSError, EOFError, UnicodeDecodeError):
        return {}
    found = {key: json.loads(value) for key, value in _HEADER_RE.findall(head)}
    return found if found.get("format") == pl.BACKUP_FORMAT else {}


def _day(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _numeric_fields(data: Any) -> dict[str, float]:
    """Numbers of a player's Data, two levels deep; lists count as their length."""
    found: dict[str, float] = {}

    def visit(value: Any, name: str, depth: int) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)) and math.isfinite(value):
            found[name] = float(value)
        elif isinstance(value, list):
            found[f"{name} (count)"] = float(len(value))
        elif isinstance(value, dict) and depth < 2:
            for key, child in value.items():
                visit(child, f"{name}.{key}" if name else str(key), depth + 1)

    visit(data, "", 0)
    return found


def _nice_step(raw: float) -> float:
    exponent = math.floor(math.log10(raw)) if raw > 0 else 0
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * 10 ** exponent
        if step >= raw:
            return step
    return 10 ** (exponent + 1)


def _fmt(value: float) -> str:
    if abs(value) >= 1e9:
        return f"{value / 1e9:g}B"
    if abs(value) >= 1e6:
        return f"{value / 1e6:g}M"
    if abs(value) >= 1e4:
        return f"{value / 1e3:g}K"
    return f"{value:g}"


def histogram(values: list[float]) -> dict:
    """Bins chosen for reading: one per value for small integer ranges, 1-2-5 steps
    for heavy-tailed amounts (currencies…), otherwise ~12 even bins."""
    values = sorted(values)
    lo, hi = values[0], values[-1]
    median = statistics.median(values)
    integers = all(v.is_integer() for v in values)
    bins: list[dict] = []
    if integers and hi - lo <= 20:
        counts = Counter(values)
        bins = [{"label": _fmt(v), "lo": v, "hi": v, "count": counts.get(v, 0)} for v in range(int(lo), int(hi) + 1)]
        scale = "values"
    elif lo >= 0 and hi > 0 and median > 0 and hi / median >= 20:
        edges = [0.0]
        step = 1.0
        while edges[-1] <= hi:
            for mult in (1, 2, 5):
                edge = mult * step
                if edge > edges[-1]:
                    edges.append(edge)
            step *= 10
        while len(edges) > 2 and edges[-2] > hi:
            edges.pop()
        bins = _count_bins(values, edges, integers)
        scale = "log"
    else:
        step = _nice_step((hi - lo) / 12 or 1)
        start = math.floor(lo / step) * step
        edges = [start + i * step for i in range(int((hi - start) / step) + 2)]
        bins = _count_bins(values, edges, integers)
        scale = "linear"
    return {
        "scale": scale, "bins": bins,
        "summary": {"count": len(values), "min": lo, "median": median,
                    "p90": values[min(len(values) - 1, int(0.9 * len(values)))], "max": hi,
                    "mean": round(statistics.fmean(values), 2)},
    }


def _count_bins(values: list[float], edges: list[float], integers: bool) -> list[dict]:
    bins = []
    index = 0
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        count = 0
        while index < len(values) and (values[index] < hi or (last and values[index] <= hi)):
            count += 1
            index += 1
        # Small whole numbers: inclusive ranges (2–4); large ones read better as round bounds (10K–20K).
        top = hi - 1 if integers and not last and hi < 1e4 else hi
        label = _fmt(lo) if integers and top <= lo else f"{_fmt(lo)}–{_fmt(top)}"
        bins.append({"label": label, "lo": lo, "hi": hi, "count": count})
    return bins


def _per_day(counter: Counter, end: str, days: int) -> list[dict]:
    last = datetime.strptime(end, "%Y-%m-%d")
    return [{"day": d, "count": counter.get(d, 0)}
            for d in ((last - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1))]


def analyze_backup(path: Path, cfg: pl.PlayersConfig, *, days: int = 60) -> dict:
    """Charts of the player base, from one backup file."""
    header = backup_header(path)
    backup = pl.load_backup(path, cfg, allow_other_universe=True)
    created_at = header.get("created_at")
    reference = (datetime.fromisoformat(created_at).timestamp() if isinstance(created_at, str)
                 else path.stat().st_mtime)
    created: Counter = Counter()
    active: Counter = Counter()
    sessions: list[float] = []
    fields: dict[str, list[float]] = {}
    profiles = 0
    recent = {1: 0, 7: 0, 30: 0}
    new_week = 0
    for record in backup.values():
        value = record.get("value") if isinstance(record, dict) else None
        if not pl.is_profile(value):
            continue
        profiles += 1
        meta = value.get("MetaData") or {}
        if (day := _day(meta.get("ProfileCreateTime"))) is not None:
            created[day] += 1
            if reference - meta["ProfileCreateTime"] <= 7 * 86400:
                new_week += 1
        last = meta.get("LastUpdate")
        if isinstance(last, (int, float)) and (day := _day(last)) is not None:
            active[day] += 1
            for span in recent:
                if reference - last <= span * 86400:
                    recent[span] += 1
        if isinstance(meta.get("SessionLoadCount"), (int, float)):
            sessions.append(float(meta["SessionLoadCount"]))
        for name, number in _numeric_fields(value.get("Data")).items():
            fields.setdefault(name, []).append(number)

    end = datetime.fromtimestamp(reference, UTC).strftime("%Y-%m-%d")
    kept = sorted((name for name, values in fields.items()
                   if len(values) >= max(3, 0.1 * profiles) and len(set(values)) > 1),
                  key=lambda name: (-len(fields[name]), name))[:MAX_FIELDS]
    return {
        "backup": {"name": path.name, "universe_id": header.get("universe_id"), "created_at": created_at,
                   "reference": datetime.fromtimestamp(reference, UTC).isoformat(timespec="seconds")},
        "profiles": profiles,
        "active": {"day": recent[1], "week": recent[7], "month": recent[30]},
        "new_week": new_week,
        "created_per_day": _per_day(created, end, days),
        "active_per_day": _per_day(active, end, days),
        "sessions": histogram(sessions) if sessions else None,
        "fields": [{"name": name, "present": len(fields[name]), **histogram(fields[name])} for name in kept],
    }


# ------------------------------------------------------------- deployments

def deployment_stats(runs: list[dict]) -> dict:
    """Runs of the deploy workflow: one row per run (oldest first) and counts per ISO week."""
    rows = []
    weeks: dict[str, Counter] = {}
    for run in runs:
        created = run.get("created_at")
        if not created:
            continue
        start = datetime.fromisoformat(created.replace("Z", "+00:00"))
        status = run.get("conclusion") if run.get("status") == "completed" else "running"
        result = status if status in ("success", "failure", "running") else "other"
        minutes = None
        if run.get("status") == "completed" and run.get("updated_at"):
            begin = run.get("run_started_at") or created
            minutes = round((datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                             - datetime.fromisoformat(begin.replace("Z", "+00:00"))).total_seconds() / 60, 1)
        rows.append({"number": run.get("run_number"), "created_at": created, "result": result,
                     "minutes": minutes, "branch": run.get("head_branch"), "url": run.get("html_url")})
        year, week, _ = start.isocalendar()
        weeks.setdefault(f"{year}-W{week:02d}", Counter())[result] += 1
    rows.sort(key=lambda r: r["created_at"])
    return {
        "runs": rows,
        "weeks": [{"week": w, **{k: c.get(k, 0) for k in ("success", "failure", "other", "running")}}
                  for w, c in sorted(weeks.items())],
    }
