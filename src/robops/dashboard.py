"""`robops dashboard`: a local web dashboard (live game, deployments, players,
backups, DataStores, configuration) with confirmed actions.

Security model — the page runs on the user's PC and uses their Roblox key:
  * the server only listens on 127.0.0.1 and refuses any other Host header
    (protection against DNS rebinding);
  * every /api call must carry the random session token generated at startup
    (X-Robops-Token header): another website open in the browser can neither
    read the data nor trigger an action (a custom header forces a CORS
    preflight, which this server never accepts);
  * every write must also repeat the environment name ("confirm"), and the
    player rules of the CLI apply (no write to an online player, etag, local
    safety copy before any change);
  * every action is appended to .robops/audit.log.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import claude_desktop as cd
from . import output as out
from . import players as pl
from . import playtests as pt
from .checks import check_environment
from .client import OpenCloudClient, RobloxApiError
from .config import ConfigError, Environment, load_config
from .deploy import overwrite_entry
from .github import GitHub, GitHubError, pick_workflow, workflow_file
from .roblox_web import RobloxWeb, WebApiError
from .stats import RANGES, History, analyze_backup, backup_header, deployment_stats, downsample, take_sample

DEFAULT_PORT = 8765
BACKUP_SUFFIXES = (".json", ".json.gz")
_STATIC = {"/": "index.html", "/app.js": "app.js", "/charts.js": "charts.js", "/editor.js": "editor.js", "/app.css": "app.css"}
_FONT_RE = re.compile(r"/fonts/([\w.-]+\.woff2)")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
        "img-src 'self' data: https://*.rbxcdn.com; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


WORKFLOWS_TTL = 300.0


class DashboardError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _attempt(func: Callable[[], Any]) -> dict:
    """{"ok": value} or {"error": message}: one failing source must not hide the others."""
    try:
        return {"ok": func()}
    except RobloxApiError as exc:
        return {"error": exc.describe()}
    except (WebApiError, GitHubError, DashboardError, pl.PlayerError, ConfigError, OSError) as exc:
        return {"error": str(exc)}


@dataclass
class Dashboard:
    config_path: Path
    github_repo: str | None = None
    github_token: str | None = None
    web: RobloxWeb = field(default_factory=RobloxWeb)
    client_factory: Callable[[str], OpenCloudClient] = OpenCloudClient
    github_factory: Callable[[str, str | None], GitHub] = GitHub.create
    deploy_workflow: str | None = None        # forced file name (--deploy-workflow), otherwise detected
    backup_workflow: str | None = None
    sample_every: float = 300                 # seconds between two live samples (0 = no recording)
    claude_logs: Path | None = None           # Claude Desktop's log folder (default: the usual places)
    claude_config_files: list[Path] | None = None   # claude_desktop_config.json (default: the ones it reads)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _history: History | None = None
    _analysis_cache: dict = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    # Kept between requests: connections stay open, and what can't change is not asked again.
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)
    _clients: dict = field(default_factory=dict)          # (factory, API key) → OpenCloudClient
    _github_client: tuple | None = None                   # ((factory, repo, token), GitHub)
    _workflows: tuple | None = None                       # (time, repo, workflows)
    _jobs: dict = field(default_factory=dict)             # (run id, attempt) → jobs of a finished run
    _reports: dict = field(default_factory=dict)          # (universe, store, entry) → playtest report

    # ---------------------------------------------------------------- helpers

    @property
    def root(self) -> Path:
        return self.config_path.resolve().parent

    @property
    def history(self) -> History:
        if self._history is None:
            self._history = History(self.root)
        return self._history

    @property
    def safety_dir(self) -> Path:
        return self.root / ".robops" / "safety"

    def _config(self):
        return load_config(self.config_path)       # re-read on each call: edits are picked up

    def env(self, name: str) -> Environment:
        return self._config().env(name)

    def client(self, env: Environment) -> OpenCloudClient:
        """One client per API key for the dashboard's lifetime: its connections to Roblox stay open."""
        key = os.environ.get(env.api_key_env, "").strip()
        if not key:
            raise DashboardError(
                400, f"{env.api_key_env} is not set in the terminal that launched the dashboard. "
                     f"In PowerShell:  $env:{env.api_key_env} = \"your-key\"  then run robops dashboard again.")
        ident = (self.client_factory, key)
        with self._cache_lock:
            if ident not in self._clients:
                self._clients[ident] = self.client_factory(key)
            return self._clients[ident]

    def github(self) -> GitHub:
        if not self.github_repo:
            raise DashboardError(400, "GitHub repository unknown: run the dashboard from your game's Git "
                                      "folder, or pass --github-repo owner/name.")
        ident = (self.github_factory, self.github_repo, self.github_token)
        with self._cache_lock:
            if self._github_client is None or self._github_client[0] != ident:
                self._github_client = (ident, self.github_factory(self.github_repo, self.github_token))
            return self._github_client[1]

    def _repo_error(self, exc: GitHubError) -> DashboardError:
        """Turns GitHub's opaque 401/403/404 into what to fix (a private repo always answers 404)."""
        repo = self.github_repo
        if exc.status == 401:
            return DashboardError(502, "GitHub refuses ROBOPS_GITHUB_TOKEN: it is invalid or expired. Create a new "
                                       "one on github.com/settings/personal-access-tokens.")
        if exc.status == 404 and not self.github_token:
            return DashboardError(502, f"GitHub can't show {repo} without a token (private repository). Set "
                                       "ROBOPS_GITHUB_TOKEN in the terminal, then run robops dashboard again.")
        if exc.status == 404:
            return DashboardError(502, f"ROBOPS_GITHUB_TOKEN is set, but this token can't see {repo}. On "
                                       "github.com/settings/personal-access-tokens, open the token: its Resource owner "
                                       f"must be {repo.split('/')[0] if repo else 'the owner'}, and Repository access must "
                                       f"include {repo.split('/')[-1] if repo else 'the game repository'}.")
        return DashboardError(502, str(exc))

    def _workflow_list(self, gh: GitHub, *, fresh: bool = False) -> list[dict]:
        """The repository's workflows, kept 5 minutes (they rarely change): one round trip less per refresh."""
        with self._cache_lock:
            kept = self._workflows
        if not fresh and kept and kept[1] == gh.repo and time.monotonic() - kept[0] < WORKFLOWS_TTL:
            return list(kept[2])
        with ThreadPoolExecutor(max_workers=2) as pool:      # both at once; info() only explains errors
            info, workflows = pool.submit(gh.info), pool.submit(gh.workflows)
            try:
                info.result()
            except GitHubError as exc:
                raise self._repo_error(exc) from exc
            try:
                found = workflows.result()
            except GitHubError as exc:
                if exc.status in (403, 404):
                    raise DashboardError(502, f"The token can see {gh.repo} but not its Actions: edit it on "
                                              "github.com/settings/personal-access-tokens and give it the permission "
                                              "Actions: Read (Read and write for the Deploy and Back up buttons).") from exc
                raise
        with self._cache_lock:
            self._workflows = (time.monotonic(), gh.repo, list(found))
        return found

    def _resolve(self, workflows: list[dict], kind: str) -> dict:
        forced = self.deploy_workflow if kind == "deploy" else self.backup_workflow
        found = (next((w for w in workflows if workflow_file(w) == forced), None) if forced
                 else pick_workflow(workflows, kind))
        if found is None:
            names = ", ".join(f"{workflow_file(w)} ({w.get('name')})" for w in workflows) or "none"
            option = "--deploy-workflow" if kind == "deploy" else "--backup-workflow"
            what = f"Workflow '{forced}'" if forced else f"No {kind} workflow"
            raise DashboardError(404, f"{what} not found in {self.github_repo}. Workflows of this repository: {names}. "
                                      f"Run robops dashboard with {option} <file.yml>, or create it with robops init.")
        return found

    def _workflow(self, gh: GitHub, kind: str) -> dict:
        """The deploy / backup workflow: robops init's file, a renamed one, or the one forced by option."""
        try:
            return self._resolve(self._workflow_list(gh), kind)
        except DashboardError as exc:
            if exc.status != 404:
                raise
            return self._resolve(self._workflow_list(gh, fresh=True), kind)   # maybe just added

    def _artifacts(self) -> list[dict]:
        try:
            return self.github().artifacts()
        except GitHubError as exc:
            raise self._repo_error(exc) from exc

    def github_status(self) -> dict:
        """Step-by-step diagnosis of the GitHub connection, shown in Setup."""
        if not self.github_repo:
            return {"repo": None, "steps": [{"level": "error", "message": "Repository not found: run the dashboard "
                                             "from your game's Git folder, or pass --github-repo owner/name."}]}
        steps = [{"level": "ok", "message": "ROBOPS_GITHUB_TOKEN is set."} if self.github_token else
                 {"level": "warn", "message": "ROBOPS_GITHUB_TOKEN is not set: only a public repository can be read."}]
        try:
            workflows = self._workflow_list(self.github(), fresh=True)
        except (DashboardError, GitHubError) as exc:
            steps.append({"level": "error", "message": str(exc)})
            return {"repo": self.github_repo, "steps": steps}
        steps.append({"level": "ok", "message": f"Repository {self.github_repo} and its Actions are readable."})
        for kind in ("deploy", "backup"):
            try:
                w = self._resolve(workflows, kind)
                steps.append({"level": "ok", "message": f"{kind.capitalize()} workflow: {workflow_file(w)} "
                                                        f"({w.get('name')})."})
            except DashboardError as exc:
                steps.append({"level": "error", "message": str(exc)})
        return {"repo": self.github_repo, "steps": steps}

    def _confirm(self, env: Environment, body: dict) -> None:
        if body.get("confirm") != env.name:
            raise DashboardError(400, f"Confirmation missing: type the environment name ({env.name}).")

    def audit(self, action: str, env: str | None, **details: Any) -> None:
        line = json.dumps({"time": _now(), "action": action, "environment": env, **details}, ensure_ascii=False)
        out.info(f"[dashboard] {line}")
        try:
            path = self.root / ".robops" / "audit.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------- reading

    def summary(self) -> dict:
        cfg = self._config()
        environments = []
        for name in cfg.names:
            env, findings = check_environment(cfg, name)
            item: dict[str, Any] = {"name": name, "findings": [f.to_dict() for f in findings],
                                    "protected": _is_protected(name)}
            if env is not None:
                item.update({
                    "universe_id": env.universe_id,
                    "version_type": env.version_type,
                    "restart_servers": env.restart_servers,
                    "key_env": env.api_key_env,
                    "key_set": bool(os.environ.get(env.api_key_env)),
                    "places": [{"name": p.name, "place_id": p.place_id, "file": str(_rel(p.file, cfg.root)),
                                "file_present": p.file.is_file()} for p in env.places],
                    "datastores": [{"name": d.name, "scope": d.scope, "file": str(_rel(d.file, cfg.root))}
                                   for d in env.datastores],
                    "players": {"datastore": env.players.datastore, "key_prefix": env.players.key_prefix,
                                "scope": env.players.scope},
                })
            environments.append(item)
        return {
            "version": __version__,
            "config": str(self.config_path),
            "root": str(cfg.root),
            "github": {"repo": self.github_repo, "token": bool(self.github_token)},
            "environments": environments,
        }

    def live(self, name: str) -> dict:
        env = self.env(name)
        client: OpenCloudClient | None = None
        key_error: str | None = None
        try:
            client = self.client(env)
        except DashboardError as exc:
            key_error = str(exc)

        def need_key() -> OpenCloudClient:
            if client is None:
                raise DashboardError(400, key_error or "API key missing")
            return client

        with ThreadPoolExecutor(max_workers=8) as pool:
            universe = pool.submit(_attempt, lambda: need_key().get_universe(env.universe_id))
            game = pool.submit(_attempt, lambda: self.web.game(env.universe_id))
            def place_info(place_id: int) -> dict:
                return _attempt(lambda: need_key().get_place(env.universe_id, place_id))

            def place_servers(place_id: int) -> dict:
                return _attempt(lambda: self.web.servers(place_id))

            places = [(p, pool.submit(place_info, p.place_id), pool.submit(place_servers, p.place_id))
                      for p in env.places]
            return {
                "environment": env.name,
                "universe_id": env.universe_id,
                "universe": universe.result(),
                "game": game.result(),
                "places": [{"name": p.name, "place_id": p.place_id, "info": info.result(),
                            "servers": servers.result()} for p, info, servers in places],
            }

    def deployments(self) -> dict:
        gh = self.github()
        workflow = self._workflow(gh, "deploy")
        runs = gh.runs(workflow_file(workflow), per_page=15)

        def describe(run: dict) -> dict:
            jobs = self._run_jobs(gh, run)
            targets = [j.get("name", "") for j in jobs.get("ok") or []]
            env = next((t.split(" ", 1)[1] for t in targets if t.startswith("Publish ")), None)
            kind = "publish" if env else ("verify" if any(t.startswith("Build") for t in targets) else None)
            return {
                "id": run.get("id"), "number": run.get("run_number"), "title": run.get("display_title"),
                "event": run.get("event"), "branch": run.get("head_branch"),
                "sha": (run.get("head_sha") or "")[:7], "actor": (run.get("actor") or {}).get("login"),
                "status": run.get("status"), "conclusion": run.get("conclusion"),
                "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
                "url": run.get("html_url"), "environment": env, "kind": kind,
            }

        with ThreadPoolExecutor(max_workers=max(1, min(16, len(runs)))) as pool:
            return {"repo": gh.repo, "workflow": {"file": workflow_file(workflow), "name": workflow.get("name")},
                    "runs": list(pool.map(describe, runs))}

    def _run_jobs(self, gh: GitHub, run: dict) -> dict:
        """Jobs of a run; a finished run's jobs never change, so they are asked only once."""
        ident = (gh.repo, run.get("id"), run.get("run_attempt"))
        with self._cache_lock:
            if ident in self._jobs:
                return self._jobs[ident]
        jobs = _attempt(lambda: gh.jobs(int(run["id"])))
        if run.get("status") == "completed" and "ok" in jobs:
            with self._cache_lock:
                self._jobs[ident] = jobs
                while len(self._jobs) > 200:
                    del self._jobs[next(iter(self._jobs))]
        return jobs

    def backups(self) -> dict:
        local: list[dict[str, Any]] = []
        for path in self._backup_files():
            stat = path.stat()
            local.append({"id": str(_rel(path, self.root)).replace("\\", "/"), "name": path.name,
                          "size": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds")})
        local.sort(key=lambda b: str(b["modified"]), reverse=True)
        safety = sorted(self.safety_dir.glob("*.json")) if self.safety_dir.is_dir() else []
        remote: dict = {"error": "GitHub repository unknown"} if not self.github_repo else _attempt(
            lambda: [
                {"id": a.get("id"), "name": a.get("name"), "size": a.get("size_in_bytes"),
                 "created_at": a.get("created_at"), "expires_at": a.get("expires_at"),
                 "expired": a.get("expired"),
                 "run_url": f"https://github.com/{self.github_repo}/actions/runs/{(a.get('workflow_run') or {}).get('id')}"}
                for a in self._artifacts() if str(a.get("name", "")).startswith("players-")
            ])
        return {"local": local, "safety_copies": len(safety), "github": remote}

    def _backup_dirs(self) -> list[Path]:
        return [self.root / "backups", self.root / "robops-backup"]

    def _backup_files(self) -> list[Path]:
        files = []
        for directory in self._backup_dirs():
            if directory.is_dir():
                files += [p for p in directory.rglob("*") if p.is_file() and p.name.endswith(BACKUP_SUFFIXES)]
        return files

    def _backup_path(self, backup_id: str) -> Path:
        """Only a file listed by backups() can be read (never an arbitrary path)."""
        wanted = (self.root / backup_id).resolve()
        for path in self._backup_files():
            if path.resolve() == wanted:
                return path
        raise DashboardError(404, f"Backup not found: {backup_id}")

    def player(self, name: str, query: str) -> dict:
        env = self.env(name)
        query = query.strip()
        if not query:
            raise DashboardError(400, "Enter a UserId or a username.")
        if query.isdigit():
            user_id = query
        else:
            found = self.web.user_id(query)
            if found is None:
                raise DashboardError(404, f"No Roblox user named '{query}'.")
            user_id = str(found)
        client = self.client(env)
        with ThreadPoolExecutor(max_workers=3) as pool:
            user = pool.submit(_attempt, lambda: self.web.user(int(user_id)))
            headshot = pool.submit(lambda: _attempt(lambda: self.web.headshot(int(user_id))))
            current = pl.fetch_profile(client, env.universe_id, env.players, user_id)
            result: dict[str, Any] = {
                "user_id": user_id, "key": env.players.key(user_id), "datastore": env.players.datastore,
                "user": user.result(), "headshot": headshot.result().get("ok"), "exists": current.exists,
            }
        if current.exists:
            result["is_profile"] = pl.is_profile(current.value)
            if pl.is_profile(current.value):
                state = pl.session_state(current.value)
                result.update({"state": {"status": state.status, "detail": state.detail, "writable": state.writable},
                               "data": current.value.get("Data"), "meta": current.value.get("MetaData")})
            else:
                result["raw"] = current.value
        return result

    def datastores(self, name: str, prefix: str | None) -> dict:
        env = self.env(name)
        return {"stores": list(self.client(env).list_datastores(env.universe_id, prefix=prefix or None))}

    def entries(self, name: str, store: str, scope: str | None, prefix: str | None, cursor: str | None) -> dict:
        env = self.env(name)
        ids, nxt = self.client(env).list_entries_page(env.universe_id, store, scope=scope or None,
                                                      prefix=prefix or None, page_token=cursor or None)
        return {"ids": ids, "next": nxt}

    def entry(self, name: str, store: str, key: str, scope: str | None) -> dict:
        env = self.env(name)
        scope, key = pl.split_scoped(key, scope or None)
        return self.client(env).get_entry(env.universe_id, store, key, scope=scope)

    # ---------------------------------------------------------------- charts

    def record_samples(self) -> dict[str, str]:
        """One live sample per environment (public roblox.com stats, no API key needed)."""
        cfg = self._config()
        results = {}
        for name in cfg.names:
            try:
                env = cfg.env(name)
                self.history.append(name, take_sample(self.web, env.universe_id))
                results[name] = "ok"
            except (ConfigError, WebApiError, OSError) as exc:
                results[name] = str(exc)
        return results

    def start_sampler(self) -> threading.Thread | None:
        """Records the live stats every sample_every seconds while the dashboard runs."""
        if self.sample_every <= 0:
            return None

        def loop() -> None:
            names = self._config().names
            last = max((self.history.last_time(n) or 0 for n in names), default=0)
            wait = max(0.0, last + self.sample_every - time.time())
            while not self._stop.wait(wait):
                self.record_samples()
                wait = self.sample_every

        thread = threading.Thread(target=loop, name="robops-sampler", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def history_view(self, name: str, range_key: str | None) -> dict:
        self.env(name)                              # unknown environment → clear error
        key = range_key if range_key in RANGES else "24h"
        span = RANGES[key]
        samples = self.history.read(name, since=time.time() - span if span else None)
        first = self.history.read(name)[:1]
        return {"range": key, "samples": downsample(samples), "recorded": len(samples),
                "first": first[0]["t"] if first else None, "every": self.sample_every,
                "file": str(_rel(self.history.path(name), self.root)).replace("\\", "/")}

    def playerbase(self, name: str, backup_id: str | None) -> dict:
        env = self.env(name)
        candidates = []
        for path in sorted(self._backup_files(), key=lambda p: p.stat().st_mtime, reverse=True):
            header = backup_header(path)
            candidates.append({"id": str(_rel(path, self.root)).replace("\\", "/"), "name": path.name,
                               "universe_id": header.get("universe_id"), "created_at": header.get("created_at"),
                               "matches": header.get("universe_id") == env.universe_id})
        if not candidates:
            return {"candidates": [], "analysis": None}
        chosen = (next((c for c in candidates if c["id"] == backup_id), None) if backup_id
                  else next((c for c in candidates if c["matches"]), candidates[0]))
        if chosen is None:
            raise DashboardError(404, f"Backup not found: {backup_id}")
        path = self._backup_path(str(chosen["id"]))
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size, env.players.datastore, env.players.key_prefix)
        if key not in self._analysis_cache:
            self._analysis_cache.clear()           # keep only the latest analysis in memory
            self._analysis_cache[key] = analyze_backup(path, env.players)
        return {"candidates": candidates, "chosen": chosen["id"], "analysis": self._analysis_cache[key]}

    def deployment_stats(self) -> dict:
        gh = self.github()
        workflow = self._workflow(gh, "deploy")
        return {"workflow": {"file": workflow_file(workflow), "name": workflow.get("name")},
                **deployment_stats(gh.runs(workflow_file(workflow), per_page=100))}

    # ------------------------------------------------------ simulated players

    def _local_playtests(self) -> dict[str, Path]:
        return pt.local_reports(self.root)

    def playtests(self, name: str) -> dict:
        """Reports saved by Claude: the DataStore of the environment + local JSON files."""
        env = self.env(name)
        reports: list[dict] = []
        remote_error = None
        try:
            client = self.client(env)
            ids: list[str] = []
            cursor = None
            while len(ids) < 500:
                page, cursor = client.list_entries_page(env.universe_id, env.playtests_datastore,
                                                        page_token=cursor, page_size=256)
                ids += page
                if not cursor:
                    break
            latest = sorted(ids, key=lambda k: k.rsplit("/", 1)[-1], reverse=True)[:40]

            def load(entry_id: str) -> dict:
                return pt.summary(self._remote_report(client, env, entry_id))

            with ThreadPoolExecutor(max_workers=8) as pool:
                loaded = list(pool.map(lambda i: _attempt(lambda: load(i)), latest))
            reports += [item["ok"] for item in loaded if "ok" in item]      # an unreadable entry is skipped
        except RobloxApiError as exc:
            if exc.status != 404:                          # 404: no report saved yet in this universe
                remote_error = exc.describe()
        except DashboardError as exc:
            remote_error = str(exc)
        for key, path in self._local_playtests().items():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                reports.append({"key": key, "source": "file", "title": path.name, "started_at": None,
                                "saved_at": None, "saved_by": None, "place": None,
                                "counts": {"players": 0, "outcomes": {}, "findings": {}}, "recognized": False,
                                "error": f"Unreadable file: {exc}"})
                continue
            reports.append(pt.summary(pt.normalize_report(key, value, "file")))
        return {"datastore": env.playtests_datastore, "universe_id": env.universe_id,
                "reports": pt.sort_reports(reports), "remote_error": remote_error,
                "stamp": pt.local_stamp(self.root)}

    def playtests_stamp(self) -> dict:
        """Cheap check, polled by the page: changes when Claude saves a report through robops."""
        return {"stamp": pt.local_stamp(self.root)}

    def playtest(self, name: str, key: str) -> dict:
        env = self.env(name)
        if key.startswith("file:"):
            path = self._local_playtests().get(key)
            if path is None:
                raise DashboardError(404, f"Report not found: {key}")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise DashboardError(400, f"{path.name} is not valid JSON: {exc}") from exc
            return pt.normalize_report(key, value, "file")
        return self._remote_report(self.client(env), env, key)

    def _remote_report(self, client: OpenCloudClient, env: Environment, entry_id: str) -> dict:
        """A report of the DataStore. Each one is written once under a new key (playtest-<time>), so it is
        read once: reopening the view costs one list call instead of 40 reads of the game's DataStore
        budget, which live servers share."""
        ident = (env.universe_id, env.playtests_datastore, entry_id)
        with self._cache_lock:
            if ident in self._reports:
                return self._reports[ident]
        scope, key = pl.split_scoped(entry_id, None)
        entry = client.get_entry(env.universe_id, env.playtests_datastore, key, scope=scope)
        report = pt.normalize_report(entry_id, entry.get("value"), "datastore")
        with self._cache_lock:
            self._reports[ident] = report
            while len(self._reports) > 500:
                del self._reports[next(iter(self._reports))]
        return report

    def playtest_instructions(self, name: str) -> dict:
        env = self.env(name)
        return {"datastore": env.playtests_datastore, "text": pt.instructions(env.playtests_datastore)}

    def import_playtest(self, body: dict) -> dict:
        """A report pasted in the page (e.g. copied from the conversation with Claude)."""
        report = body.get("report")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except ValueError as exc:
                raise DashboardError(400, f"The pasted text is not valid JSON: {exc}") from exc
        if not isinstance(report, dict) or not pt.is_report(report):
            raise DashboardError(400, "This JSON is not a report: it needs a players or findings list "
                                      f"(format {pt.REPORT_FORMAT}).")
        key, path = pt.save_local_report(self.root, report, saved_by="robops dashboard (pasted)")
        self.audit("playtest.import", None, file=str(_rel(path, self.root)))
        return {"key": key, "message": f"Report saved in {_rel(path, self.root)}."}

    def claude_activity(self) -> dict:
        dirs = [self.claude_logs] if self.claude_logs else pt.claude_log_dirs()
        return pt.studio_activity(dirs, servers=self._claude_status()["studio"])

    def _claude_status(self) -> dict:
        try:
            return cd.status(self.root, files=self.claude_config_files)
        except OSError as exc:
            return {"found": False, "configs": [], "installed": False, "studio": [], "entry": None, "error": str(exc)}

    def claude_setup(self) -> dict:
        """Is Claude Desktop ready: robops' tool for the reports, and Roblox Studio's MCP server."""
        state = self._claude_status()
        entry = cd.server_entry(self.root)
        return {**state, "entry": entry, "restart": cd.RESTART, "studio_hint": cd.STUDIO_HINT,
                "claude_code": cd.claude_code_command(entry), "snippet": cd.manual_snippet(entry)}

    def claude_install(self, body: dict) -> dict:
        try:
            changes = cd.install(self.root, files=self.claude_config_files)
        except cd.ClaudeDesktopError as exc:
            raise DashboardError(400, str(exc)) from exc
        changed = [c for c in changes if c.status != "unchanged"]
        self.audit("claude.install", None, files=[str(c.path) for c in changed])
        if not changed:
            return {"message": "robops was already set up in Claude Desktop."}
        return {"message": "robops added to Claude Desktop. " + cd.RESTART}

    # --------------------------------------------------------------- actions

    def restart(self, name: str, body: dict) -> dict:
        env = self.env(name)
        self._confirm(env, body)
        self.client(env).restart_servers(env.universe_id)
        self.audit("servers.restart", env.name)
        return {"message": f"Server restart requested for {env.name}."}

    def deploy(self, name: str, body: dict) -> dict:
        env = self.env(name)
        self._confirm(env, body)
        gh = self.github()
        ref = str(body.get("ref") or gh.default_branch())
        restart = {"yes": "yes", "no": "no"}.get(str(body.get("restart")), "per config")
        inputs = {
            "environment": env.name,
            "version_type": "Saved" if body.get("version_type") == "Saved" else "Published",
            "with_datastores": "true" if body.get("with_datastores") else "false",
            "restart": restart,
        }
        gh.dispatch(workflow_file(self._workflow(gh, "deploy")), ref, inputs)
        self.audit("deploy.dispatch", env.name, ref=ref, **inputs)
        return {"message": f"Deployment of {env.name} launched on GitHub ({ref}). It appears in the list within seconds."}

    def run_backup(self, body: dict) -> dict:
        gh = self.github()
        workflow = workflow_file(self._workflow(gh, "backup"))
        ref = gh.default_branch()
        gh.dispatch(workflow, ref, {})
        self.audit("backup.dispatch", None, ref=ref, workflow=workflow)
        return {"message": "Player backup launched on GitHub. The file appears under GitHub backups when the run ends."}

    def snapshot(self, name: str, body: dict) -> dict:
        env = self.env(name)
        self._confirm(env, body)
        snap = self.client(env).snapshot_datastores(env.universe_id)
        self.audit("datastores.snapshot", env.name, new=bool(snap.get("newSnapshotTaken")))
        return {"message": "Snapshot created." if snap.get("newSnapshotTaken")
                else f"A snapshot was already taken today ({snap.get('latestSnapshotTime')})."}

    def entry_set(self, name: str, body: dict) -> dict:
        env = self.env(name)
        self._confirm(env, body)
        store, key, scope = _text(body, "datastore"), _text(body, "key"), body.get("scope") or None
        if "value" not in body:
            raise DashboardError(400, "Value missing.")
        scope, key = pl.split_scoped(key, scope)
        entry = overwrite_entry(self.client(env), env.universe_id, store, key, body["value"], scope=scope)
        self.audit("datastore.set", env.name, datastore=store, key=key, scope=scope)
        return {"message": f"{store}/{key} saved (revision {entry.get('revisionId', '?')})."}

    def entry_delete(self, name: str, body: dict) -> dict:
        env = self.env(name)
        self._confirm(env, body)
        store, key, scope = _text(body, "datastore"), _text(body, "key"), body.get("scope") or None
        scope, key = pl.split_scoped(key, scope)
        self.client(env).delete_entry(env.universe_id, store, key, scope=scope)
        self.audit("datastore.delete", env.name, datastore=store, key=key, scope=scope)
        return {"message": f"{store}/{key} deleted (purged by Roblox within 30 days)."}

    def _writable_profile(self, env: Environment, client: OpenCloudClient, user_id: str,
                          force: bool) -> pl.CurrentProfile:
        current = pl.fetch_profile(client, env.universe_id, env.players, user_id)
        if current.exists and pl.is_profile(current.value):
            state = pl.session_state(current.value)
            if not state.writable and not force:
                raise DashboardError(409, f"Player {user_id}: {state.detail}. Write refused: their server would "
                                          "overwrite the change on its next save. Wait for them to leave.")
        return current

    def player_edit(self, name: str, body: dict) -> dict:
        env = self.env(name)
        user_id = _user_id(body)
        if "data" not in body:
            raise DashboardError(400, "Data missing.")
        client = self.client(env)
        current = self._writable_profile(env, client, user_id, bool(body.get("force")))
        if not current.exists or not pl.is_profile(current.value):
            raise DashboardError(404, f"No ProfileStore profile for {env.players.key(user_id)}.")
        changes = pl.changed_fields(current.value.get("Data"), body["data"])
        if body.get("preview") or not changes:
            return {"changes": changes, "written": False}
        self._confirm(env, body)
        safety = pl.save_safety_copy(self.safety_dir, env.players, user_id, current)
        pl.write_profile(client, env.universe_id, env.players, user_id,
                         pl.with_data(current.value, body["data"]), current)
        self.audit("player.edit", env.name, user_id=user_id, changes=changes[:50], safety_copy=str(safety))
        return {"changes": changes, "written": True, "message": f"Player {user_id} saved (previous profile: {safety})."}

    def player_restore(self, name: str, body: dict) -> dict:
        env = self.env(name)
        user_id = _user_id(body)
        path = self._backup_path(_text(body, "backup"))
        backup = pl.load_backup(path, env.players, universe_id=env.universe_id,
                                allow_other_universe=bool(body.get("allow_other_universe")))
        record = backup.get(user_id)
        if record is None:
            raise DashboardError(404, f"Player {user_id} is not in {path.name}.")
        backup_profile = record.get("value")
        if not pl.is_profile(backup_profile):
            raise DashboardError(409, f"Player {user_id}: the backup entry is not a ProfileStore profile.")
        client = self.client(env)
        current = self._writable_profile(env, client, user_id, bool(body.get("force")))
        if not current.exists and not body.get("recreate_missing"):
            raise DashboardError(409, f"Player {user_id} has no current profile. Recreating it is only allowed if "
                                      "they were not deleted following a data erasure request: tick the option to confirm.")
        current_ok = current.exists and pl.is_profile(current.value)
        changes = pl.changed_fields(current.value.get("Data") if current_ok else None, backup_profile["Data"])
        if body.get("preview"):
            return {"changes": changes, "written": False, "exists": current.exists}
        self._confirm(env, body)
        safety = pl.save_safety_copy(self.safety_dir, env.players, user_id, current)
        new_profile = pl.with_data(current.value if current_ok else None, backup_profile["Data"],
                                   fallback_profile=backup_profile)
        pl.write_profile(client, env.universe_id, env.players, user_id, new_profile, current,
                         users=record.get("users") or None)
        self.audit("player.restore", env.name, user_id=user_id, backup=path.name, safety_copy=str(safety))
        return {"changes": changes, "written": True, "message": f"Player {user_id} restored from {path.name}."}

    def player_delete(self, name: str, body: dict) -> dict:
        env = self.env(name)
        user_id = _user_id(body)
        self._confirm(env, body)
        if str(body.get("confirm_user")) != user_id:
            raise DashboardError(400, f"Type the UserId ({user_id}) to confirm the deletion.")
        client = self.client(env)
        current = self._writable_profile(env, client, user_id, bool(body.get("force")))
        if not current.exists:
            return {"message": f"No profile for {user_id}: nothing to delete."}
        safety = pl.save_safety_copy(self.safety_dir, env.players, user_id, current)
        client.delete_entry(env.universe_id, env.players.datastore, env.players.key(user_id), scope=env.players.scope)
        self.audit("player.delete", env.name, user_id=user_id, safety_copy=str(safety))
        return {"message": f"Profile of {user_id} deleted (local copy: {safety})."}


def _is_protected(name: str) -> bool:
    """Environments that ask for the name to be typed before any action in the page."""
    return bool(re.search(r"prod|live", name, re.IGNORECASE))


def _rel(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DashboardError(400, f"Field '{key}' missing.")
    return value.strip()


def _user_id(body: dict) -> str:
    value = str(body.get("user_id", "")).strip()
    if not value.isdigit():
        raise DashboardError(400, "Invalid UserId.")
    return value


# ------------------------------------------------------------------- HTTP

def _routes(app: Dashboard) -> list[tuple[str, re.Pattern, Callable[..., Any]]]:
    def q(params: dict, key: str) -> str | None:
        return params.get(key, [None])[0]

    env = r"(?P<env>[\w.-]+)"
    return [
        ("GET", re.compile(r"/api/summary"), lambda p, b: app.summary()),
        ("GET", re.compile(rf"/api/env/{env}/live"), lambda p, b, env: app.live(env)),
        ("GET", re.compile(r"/api/deployments"), lambda p, b: app.deployments()),
        ("GET", re.compile(r"/api/backups"), lambda p, b: app.backups()),
        ("GET", re.compile(r"/api/github"), lambda p, b: app.github_status()),
        ("GET", re.compile(rf"/api/env/{env}/history"), lambda p, b, env: app.history_view(env, q(p, "range"))),
        ("GET", re.compile(rf"/api/env/{env}/playerbase"), lambda p, b, env: app.playerbase(env, q(p, "backup"))),
        ("GET", re.compile(r"/api/stats/deployments"), lambda p, b: app.deployment_stats()),
        ("GET", re.compile(rf"/api/env/{env}/playtests"), lambda p, b, env: app.playtests(env)),
        ("GET", re.compile(rf"/api/env/{env}/playtest"), lambda p, b, env: app.playtest(env, q(p, "key") or "")),
        ("GET", re.compile(rf"/api/env/{env}/playtests/instructions"), lambda p, b, env: app.playtest_instructions(env)),
        ("GET", re.compile(r"/api/claude/studio"), lambda p, b: app.claude_activity()),
        ("GET", re.compile(r"/api/claude/setup"), lambda p, b: app.claude_setup()),
        ("GET", re.compile(r"/api/playtests/stamp"), lambda p, b: app.playtests_stamp()),
        ("POST", re.compile(r"/api/claude/install"), lambda p, b: app.claude_install(b)),
        ("POST", re.compile(r"/api/playtests/import"), lambda p, b: app.import_playtest(b)),
        ("GET", re.compile(rf"/api/env/{env}/player"), lambda p, b, env: app.player(env, q(p, "q") or "")),
        ("GET", re.compile(rf"/api/env/{env}/datastores"), lambda p, b, env: app.datastores(env, q(p, "prefix"))),
        ("GET", re.compile(rf"/api/env/{env}/entries"),
         lambda p, b, env: app.entries(env, q(p, "datastore") or "", q(p, "scope"), q(p, "prefix"), q(p, "cursor"))),
        ("GET", re.compile(rf"/api/env/{env}/entry"),
         lambda p, b, env: app.entry(env, q(p, "datastore") or "", q(p, "key") or "", q(p, "scope"))),
        ("POST", re.compile(rf"/api/env/{env}/restart"), lambda p, b, env: app.restart(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/deploy"), lambda p, b, env: app.deploy(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/snapshot"), lambda p, b, env: app.snapshot(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/entry/set"), lambda p, b, env: app.entry_set(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/entry/delete"), lambda p, b, env: app.entry_delete(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/player/edit"), lambda p, b, env: app.player_edit(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/player/restore"), lambda p, b, env: app.player_restore(env, b)),
        ("POST", re.compile(rf"/api/env/{env}/player/delete"), lambda p, b, env: app.player_delete(env, b)),
        ("POST", re.compile(r"/api/backups/run"), lambda p, b: app.run_backup(b)),
    ]


def make_handler(app: Dashboard, token: str, port_ref: list[int]) -> type[BaseHTTPRequestHandler]:
    routes = _routes(app)
    web = resources.files("robops") / "web"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"robops-dashboard/{__version__}"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:   # noqa: A002 — quiet console
            pass

        def _allowed_origins(self) -> set[str]:
            port = port_ref[0]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _guard(self) -> bool:
            """Host (anti DNS rebinding) and Origin checks for every request."""
            if self.headers.get("Host", "") not in self._allowed_origins():
                self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden host."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin.removeprefix("http://") not in self._allowed_origins():
                self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden origin."})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlsplit(self.path).path
            font = _FONT_RE.fullmatch(path)
            resource = web / _STATIC[path] if path in _STATIC else (web / "fonts" / font.group(1) if font else None)
            if resource is not None:
                if not resource.is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                    return
                self._send(HTTPStatus.OK, resource.read_bytes(), _TYPES[Path(resource.name).suffix])
                return
            self._api("GET")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            self._api("POST")

        def _api(self, method: str) -> None:
            parts = urlsplit(self.path)
            if not parts.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            if not hmac.compare_digest(self.headers.get("X-Robops-Token", ""), token):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Session expired: reopen the link printed by "
                                                              "robops dashboard."})
                return
            body: dict = {}
            if method == "POST":
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON expected."})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON."})
                    return
                if not isinstance(body, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object expected."})
                    return
            params = parse_qs(parts.query)
            for verb, pattern, handler in routes:
                match = pattern.fullmatch(parts.path)
                if match and verb == method:
                    self._run(handler, params, body, match.groupdict())
                    return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint."})

        def _run(self, handler: Callable[..., Any], params: dict, body: dict, groups: dict) -> None:
            try:
                self._json(HTTPStatus.OK, handler(params, body, **groups))
            except DashboardError as exc:
                self._json(exc.status, {"error": str(exc)})
            except ConfigError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except pl.PlayerError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except RobloxApiError as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": exc.describe(), "status": exc.status})
            except (GitHubError, WebApiError) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — the server must keep running
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": f"Unexpected error ({type(exc).__name__}): {exc}"})

    return Handler


def serve(app: Dashboard, *, port: int = DEFAULT_PORT, open_browser: bool = True,
          ready: Callable[[str, ThreadingHTTPServer], None] | None = None) -> None:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    handler = make_handler(app, token, port_ref)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)     # port busy: any free port
    port_ref[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/#token={token}"
    out.success(f"robops dashboard running: {url}")
    out.info("  Keep this terminal open; Ctrl+C to stop. The link contains a session key: don't share it.")
    if app.sample_every > 0:
        out.info(f"  Live stats recorded every {app.sample_every / 60:g} min in .robops/stats/ (Charts view).")
        app.start_sampler()
    if ready:
        ready(url, server)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()
        out.info("Dashboard stopped.")
