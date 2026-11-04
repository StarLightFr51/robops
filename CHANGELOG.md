# Changelog

All notable changes to robops. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`.

## 1.5.0

### Added
- Dashboard **Charts** view, with a chart for every data that has a trend or a distribution:
  - live game over time (players online, public servers, new visits per hour/day, favorites, likes), period from
    6 hours to 30 days;
  - player base from a backup: activity tiles (active in 24 h / 7 days / 30 days, new in 7 days), new players per
    day, players by last visit, sessions per player, and one histogram per numeric field of the players' Data;
  - deployment runs per week by result, and the duration of each run;
  - size of each backup over time.
  Each chart has a crosshair or per-column tooltip (mouse and arrow keys) and a table view. The colours were
  validated for colour blindness and contrast in light and dark mode.
- The dashboard records the public live stats every 5 minutes (`--sample-every`, `.robops/stats/`), and
  `robops stats record` records one sample (to schedule while the dashboard is closed).

## 1.4.1

### Fixed
- Dashboard: a GitHub 404 on a private repository no longer says "set ROBOPS_GITHUB_TOKEN" when the token is set.
  It now tells which of these is the problem:
  - the token is missing;
  - the token is invalid or expired;
  - the token can't see the repository (resource owner, repository access);
  - the token lacks the Actions permission;
  - the workflow was not found (the message lists the workflows that exist).
- The deploy and backup workflows are detected even when renamed (e.g. `deploy.yml`); `--deploy-workflow` and
  `--backup-workflow` force a file name.

### Added
- Setup view: step-by-step check of the GitHub connection, with a *Check again* button.

## 1.4.0

### Added
- `robops dashboard`: a local web dashboard served on 127.0.0.1.
  - Live game: players, visits, likes, public servers, places, experience settings.
  - Deployment runs from GitHub Actions.
  - Player lookup with edit / restore / delete.
  - Local and GitHub backups.
  - A DataStore browser and editor.
  - Configuration checks.
  - Actions (deploy, restart, snapshot, backup, writes) need a confirmation with the environment name, follow
    the CLI's player protections and are logged in `.robops/audit.log`.
- `OpenCloudClient.get_universe` and `list_entries_page`; `robops check` now shares its checks with the dashboard.

## 1.3.0

### Changed (behaviour)
- **Updates are announced, not installed silently.** The daily check now only tells you a new
  version exists; `robops self-update` installs it. Set `ROBOPS_AUTO_UPDATE=1` to get the old
  silent installation back.
- `robops init --action-repo owner/robops` pins the generated workflows to the tag of the installed
  version (`@v1.3.0`) instead of `@main`.
- `robops player restore --all` no longer recreates profiles deleted since the backup (they may have
  been erased at the player's request); `--recreate-missing` restores the old behaviour.
- `robops player restore` refuses a backup taken in another universe unless `--allow-other-universe`.
- Unknown fields in `robops.toml` are now an error with a suggestion instead of being ignored.

### Fixed
- Uninstalling from *Apps & features* now targets the folder chosen with `robops-setup.exe --dir`
  (the uninstall command records it; older installs are found from the setup's own location).
- Uninstalling only deletes robops' own files, then the folder if it is empty — never other files
  in a folder passed with `--dir`.
- Self-deletion of the install folder on Windows: the `cmd` command line was mangled by Python's
  quoting and never ran.
- `restart_servers = "${VAR}"` with `VAR=false` restarted the servers (any non-empty text was true).
- Build commands on Windows lost their backslashes (`build\game.rbxl` → `buildgame.rbxl`).
- `robops publish notes.txt` and `robops player edit --unset ""` printed a Python traceback;
  unexpected errors now print a one-line message (`ROBOPS_DEBUG=1` for the traceback).

### Added
- When a deployment fails halfway, the run summary and the `versions` / `failed` outputs still say
  which places are already live.
- Optional encryption of the nightly player backups with [age](https://github.com/FiloSottile/age)
  (`age-recipient` input of the backup action, `BACKUP_AGE_RECIPIENT` variable in the template).
- Build provenance attestations on releases (`gh attestation verify robops.exe --repo …`).
- Commented `environment:` line in the deploy template for GitHub Environments with required reviewers.
- `LICENSE` file (MIT), this changelog, Dependabot, ruff + mypy and Windows tests in CI.

### Safety
- Publishing a place is no longer retried when Roblox may already have applied it (5xx, read
  timeout), which could create a duplicate version. `Retry-After` is capped at 60 s.
- DataStore overwrites (`deploy --with-datastores`, `ds set`, `ds import`) are conditioned on the
  etag that was read, like player writes already were.
- The release workflow only grants `contents: write` to the job that publishes.
