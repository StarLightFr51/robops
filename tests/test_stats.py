import gzip
import json
import time

import pytest

from robops import cli
from robops import players as pl
from robops.stats import History, analyze_backup, backup_header, deployment_stats, downsample, histogram, take_sample

from dashboard_fakes import FakeWeb, make_project, profile


def test_history_appends_reads_and_skips_broken_lines(tmp_path):
    history = History(tmp_path)
    history.append("production", {"t": 100, "playing": 5})
    history.append("production", {"t": 200, "playing": 7})
    with history.path("production").open("a", encoding="utf-8") as fh:
        fh.write('{"t": 300, "play')                     # cut by a crash
    assert [s["t"] for s in history.read("production")] == [100, 200]
    assert [s["t"] for s in history.read("production", since=150)] == [200]
    assert history.last_time("production") == 200
    assert history.read("staging") == []
    assert History(tmp_path).path("../evil").name == ".._evil.jsonl"   # stays in .robops/stats


def test_take_sample_uses_public_stats():
    sample = take_sample(FakeWeb(), 333, now=1000)
    assert sample == {"t": 1000, "playing": 187, "visits": 1_284_310, "favorites": 20_514,
                      "up_votes": 9_870, "down_votes": 612, "servers": 12}


def test_downsample_averages_players_and_keeps_last_totals():
    samples = [{"t": i, "playing": i % 2 * 10, "servers": 1, "visits": i, "favorites": 1, "up_votes": 1,
                "down_votes": 0} for i in range(1000)]
    small = downsample(samples, max_points=100)
    assert len(small) == 100
    assert all(4 <= s["playing"] <= 6 for s in small)         # mean of 0/10 alternation
    assert small[-1]["visits"] == 999                         # running total: last value, not a mean
    assert downsample(samples[:10]) == samples[:10]


@pytest.mark.parametrize("values, scale, labels", [
    ([1, 2, 2, 3, 5], "values", ["1", "2", "3", "4", "5"]),
    ([0, 1, 3, 7, 15, 40, 90, 150, 400, 900, 1500, 3000, 25000], "log", None),
    ([1.5, 2.25, 3.0, 7.75], "linear", None),
])
def test_histogram_bins(values, scale, labels):
    result = histogram([float(v) for v in values])
    assert result["scale"] == scale
    assert sum(b["count"] for b in result["bins"]) == len(values)        # nothing lost at the edges
    assert result["summary"]["max"] == max(values)
    if labels:
        assert [b["label"] for b in result["bins"]] == labels


def _write_backup(path, profiles, universe=333, created_at="2026-11-04T02:17:00+00:00"):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"format": pl.BACKUP_FORMAT, "created_at": created_at, "universe_id": universe,
                   "datastore": "Default", "key_prefix": "PLAYER_", "scope": "global",
                   "profiles": {k: {"value": v, "users": []} for k, v in profiles.items()}}, fh)


def test_analyze_backup(tmp_path):
    ref = 1_793_000_000                                         # 2026-10-25
    def p(created_days, last_days, data, sessions=3):
        prof = profile(data)
        prof["MetaData"].update(ProfileCreateTime=ref - created_days * 86400, LastUpdate=ref - last_days * 86400,
                                SessionLoadCount=sessions)
        return prof
    profiles = {str(i): p(i % 10, i % 3, {"coins": i * 10, "level": i % 5 + 1, "pets": ["a"] * (i % 4),
                                             "stats": {"wins": i}, "vip": True, "name": "x"}) for i in range(1, 41)}
    profiles["broken"] = {"not": "a profile"}
    path = tmp_path / "b.json.gz"
    created = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ref))
    _write_backup(path, profiles, created_at=created)
    assert backup_header(path) == {"format": pl.BACKUP_FORMAT, "created_at": created, "universe_id": 333}

    a = analyze_backup(path, pl.PlayersConfig(), days=30)
    assert a["profiles"] == 40 and a["backup"]["universe_id"] == 333
    assert a["active"]["day"] == sum(1 for i in range(1, 41) if i % 3 <= 1)
    assert sum(d["count"] for d in a["created_per_day"]) == 40 and len(a["created_per_day"]) == 30
    names = [f["name"] for f in a["fields"]]
    assert set(names) == {"coins", "level", "pets (count)", "stats.wins"}   # booleans and text ignored
    level = next(f for f in a["fields"] if f["name"] == "level")
    assert [b["label"] for b in level["bins"]] == ["1", "2", "3", "4", "5"] and level["present"] == 40


def test_deployment_stats_weeks_and_durations():
    runs = [
        {"run_number": 2, "status": "completed", "conclusion": "failure", "created_at": "2026-11-03T10:00:00Z",
         "run_started_at": "2026-11-03T10:00:10Z", "updated_at": "2026-11-03T10:03:10Z"},
        {"run_number": 1, "status": "completed", "conclusion": "success", "created_at": "2026-10-27T10:00:00Z",
         "updated_at": "2026-10-27T10:02:00Z"},
        {"run_number": 3, "status": "in_progress", "conclusion": None, "created_at": "2026-11-04T10:00:00Z"},
        {"run_number": 4, "status": "completed", "conclusion": "cancelled", "created_at": "2026-11-04T11:00:00Z",
         "updated_at": "2026-11-04T11:00:30Z"},
    ]
    stats = deployment_stats(runs)
    assert [r["number"] for r in stats["runs"]] == [1, 2, 3, 4]
    assert [r["minutes"] for r in stats["runs"]] == [2.0, 3.0, None, 0.5]
    assert stats["weeks"] == [
        {"week": "2026-W44", "success": 1, "failure": 0, "other": 0, "running": 0},
        {"week": "2026-W45", "success": 0, "failure": 1, "other": 1, "running": 1},
    ]


def test_stats_record_command(tmp_path, monkeypatch, capsys):
    make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("robops.roblox_web.RobloxWeb", FakeWeb)
    assert cli.main(["stats", "record"]) == 0
    assert "production: 187 playing" in capsys.readouterr().out
    assert len(History(tmp_path).read("staging")) == 1 and len(History(tmp_path).read("production")) == 1
