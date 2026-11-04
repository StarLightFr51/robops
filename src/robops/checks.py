"""Offline checks of one environment, shared by `robops check` and the dashboard."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .config import Config, ConfigError, Environment


@dataclass
class Finding:
    level: str          # "ok" | "info" | "warn" | "error"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def check_environment(cfg: Config, name: str) -> tuple[Environment | None, list[Finding]]:
    """Validates the environment (fields, ${VAR}, files, API key presence) without any network call."""
    try:
        env = cfg.env(name)
    except ConfigError as exc:
        return None, [Finding("error", str(exc))]
    findings = [Finding("ok", f"universe {env.universe_id}, {len(env.places)} place(s), version {env.version_type}")]
    if env.build_command:
        findings.append(Finding("info", f"build: {env.build_command}"))
    findings.append(Finding("info", f"player profiles: DataStore {env.players.datastore}, keys "
                                    f"{env.players.key_prefix}<UserId>"
                                    + (f", scope {env.players.scope}" if env.players.scope else "")))
    for place in env.places:
        state = "present" if place.file.is_file() else "missing (will be produced by the build?)"
        findings.append(Finding("info", f"  • {place.name} → {place.place_id} : {place.file.name} {state}"))
    for seed in env.datastores:
        if seed.file.is_file():
            findings.append(Finding("info", f"  • DataStore {seed.name} ← {seed.file.name} present"))
        else:
            findings.append(Finding("error", f"  • DataStore {seed.name}: file not found ({seed.file}). Create it, "
                                             f"or comment out the [[environments.{name}.datastores]] section "
                                             "if you don't use it."))
    if not os.environ.get(env.api_key_env):
        findings.append(Finding(
            "warn",
            f"{env.api_key_env} is not set on this machine. This is normal if you publish through "
            "GitHub Actions: a GitHub secret only exists during runs on GitHub. "
            f"To test locally, in PowerShell:  $env:{env.api_key_env} = \"your-key\"",
        ))
    return env, findings
