"""Minimal GitHub REST client for the dashboard: deployment runs, backup artifacts,
and launching the deploy / backup workflows (workflow_dispatch).

The token comes from ROBOPS_GITHUB_TOKEN or GH_TOKEN. A fine-grained token on the
game repository with "Actions: Read" is enough to read; launching a workflow also
needs "Actions: Read and write".
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .updater import make_session

DEPLOY_WORKFLOW = "roblox-deploy.yml"
BACKUP_WORKFLOW = "roblox-backup.yml"
_REMOTE_RE = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def token() -> str | None:
    return os.environ.get("ROBOPS_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def repo_from_remote(directory: Path) -> str | None:
    """owner/name of the repository's "origin" remote (https or ssh), None if unknown."""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=directory,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = _REMOTE_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


@dataclass
class GitHub:
    repo: str
    session: requests.Session
    api: str = "https://api.github.com"

    @classmethod
    def create(cls, repo: str, token_value: str | None) -> GitHub:
        api = os.environ.get("ROBOPS_GITHUB_API", "https://api.github.com").rstrip("/")
        return cls(repo, make_session(token_value), api)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self.session.request(method, f"{self.api}/repos/{self.repo}{path}", timeout=20, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError(0, f"Unable to reach GitHub: {exc}") from exc
        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = ""
            hint = {
                401: "invalid or expired token (ROBOPS_GITHUB_TOKEN)",
                403: "the token lacks the permission (Actions: Read, or Read and write to launch a workflow)",
                404: f"repository {self.repo} or workflow not found, or not visible to the token",
                422: "GitHub refused the inputs (does the workflow accept workflow_dispatch on this branch?)",
            }.get(resp.status_code, "")
            raise GitHubError(resp.status_code, f"GitHub HTTP {resp.status_code}: {message} {('— ' + hint) if hint else ''}".strip())
        return resp.json()

    def default_branch(self) -> str:
        return str(self._call("GET", "")["default_branch"])

    def runs(self, workflow: str, *, per_page: int = 15) -> list[dict]:
        payload = self._call("GET", f"/actions/workflows/{workflow}/runs", params={"per_page": per_page})
        return list(payload.get("workflow_runs", []))

    def jobs(self, run_id: int) -> list[dict]:
        return list(self._call("GET", f"/actions/runs/{run_id}/jobs").get("jobs", []))

    def artifacts(self, *, per_page: int = 100) -> list[dict]:
        return list(self._call("GET", "/actions/artifacts", params={"per_page": per_page}).get("artifacts", []))

    def dispatch(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self._call("POST", f"/actions/workflows/{workflow}/dispatches", json={"ref": ref, "inputs": inputs})
