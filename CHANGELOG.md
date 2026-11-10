# Changelog

All notable changes to robops. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`.

## 1.8.0

### Changed
- **robops is installed as a folder**: `robops.exe` plus the `_internal` folder of its libraries, in
  `%LOCALAPPDATA%\Programs\robops`, which `robops-setup.exe` still adds to the PATH. Every command starts about
  4 times faster on Windows: 0.27 s instead of 1.23 s for `robops --version` (median, GitHub's Windows machine).
  A single .exe unpacked its libraries into a temporary folder at every launch.
- Releases publish `robops-windows.zip` (the folder). `robops.exe` (single file) stays published, so that
  robops 1.7 and older can still update themselves.
- `robops self-update` and the installer install the folder and also move a single-file install to it (even at
  the same version). The daily check suggests it.
- Updating works while robops runs from the same folder (dashboard, Claude Desktop's MCP server). Each file in
  use is renamed `*.old`, the new one is moved in, and everything is put back if a step fails. Old files are
  deleted at a later launch, once nothing uses them.
- The zip is checked before anything is replaced: SHA-256, no path leaving the folder, `robops.exe` and
  `_internal` present.
- Uninstalling also removes `_internal`. Apps & features shows the real size.

### Added
- `exe/smoke_test.py`, run by the release before publishing and by the new *Windows executables* workflow
  whenever the install or update code changes. On a real Windows machine, with the files just built served as
  a fake release, it checks:
  - an install with `robops-setup.exe`;
  - the move of a 1.7-style single `robops.exe` to the folder;
  - an update while a robops is running, which keeps answering;
  - the cleanup at the next launch;
  - an uninstall.

## 1.7.1

### Changed
- Faster dashboard. Measured against the GitHub API and with Roblox's latency simulated:
  - **Deployments**: a refresh takes about 0.8 s instead of 3.5 s, with 1 call to GitHub instead of 18, and
    the first load takes 2.3 s instead of 4.9 s. The jobs of a finished run are asked only once, the workflow
    list is kept 5 minutes (asked again at once if a workflow is missing), and the remaining calls are made at
    the same time.
  - **GitHub rate limit**: every GitHub read sends back the ETag of the last answer. When nothing changed, GitHub
    answers 304, which is lighter and not counted in the 5,000 requests per hour of a token. Before, leaving the
    dashboard open during a deployment could use the whole hourly quota.
  - **Simulated players**: reopening the view takes 0.15 s instead of 0.9 s. The DataStore reports, written
    once, are read once, instead of 40 reads of the DataStore budget the game's servers share.
  - Connections to Roblox and GitHub stay open between two requests, so the secure connection is not set up
    again each time.
  - The page refreshes the game, the summary and the runs at the same time instead of one after the other.
- `robops check --online` checks every place at the same time.
- Commands start faster: the HTTP library is loaded only when a command uses the network (`--help`,
  `--version`, `check`, `init`, `mcp …` no longer load it). From source: 85 ms instead of 150 ms.

## 1.7.0

### Added
- Dashboard **Simulated players** view, for what Claude does in Roblox Studio through the Roblox Studio MCP.
  Each report shows the summary, problems found sorted by severity, each simulated player (profile, outcome,
  time, feedback, steps) and measures.
- **robops MCP server** (`robops mcp serve`), so that Claude saves its reports itself, with no instructions to
  paste: tool `save_playtest_report` (written to `.robops/playtests/`), `list_playtest_reports` and
  `get_playtest_report` to compare with earlier runs, and a `simulate_players` prompt (the method to follow)
  in Claude Desktop's "+" menu. Plain JSON-RPC over stdio, no dependency; checked with the official MCP
  TypeScript and Python SDK clients.
- `robops mcp install` / `status` / `uninstall`, and an **Add robops to Claude Desktop** button: adds the
  server to `claude_desktop_config.json` without touching the other servers (copy of the previous file in
  `.robops-backup`; an invalid file is left alone). On Windows it also writes the file the MSIX/Microsoft Store
  install really reads (`%LOCALAPPDATA%\Packages\Claude_…\LocalCache\Roaming\Claude`). Tells whether Roblox
  Studio's MCP server is set up too, and gives the line for Claude Code.
- New reports appear in the view by themselves (checked every 10 s, without calling Roblox).
- **Manual instructions** (Luau code for Studio's `run_code` saving to the DataStore `RobopsPlaytests`,
  `[playtests] datastore` in `robops.toml`) and **Paste a report**, for Claude without the robops tool.
- **Claude's actions in Studio**: when Claude called Studio, how long it took and whether it failed, read from
  Claude Desktop's MCP log (`--claude-logs` to point elsewhere). Reads the summarized lines of current Claude
  Desktop versions (`method="tools/call" id=4`, `id=4 result(1 blocks)`, `error(code=…)`) as well as the full
  JSON-RPC messages of older ones (with the tool, its code and Studio's answer), the MSIX log folder, and the
  Studio server's name from Claude Desktop's config.

### Fixed
- Dashboard on phones: wide tables scroll inside their card, and words are no longer broken mid-way.

## 1.6.1

### Fixed
- Saving a player (dashboard, `player edit`, `player restore`) or any DataStore value holding a null failed with
  `HTTP 400: Value cannot be null. (Parameter 'value')`. Open Cloud's v2 DataStore API refuses an object field
  set to null, and an offline ProfileStore profile has some (`MetaData.ActiveSession`, `ForceLoadSession`).
  Null fields of objects are now left out of what is written. For Luau a missing field and a null are both
  `nil`, so the game sees the same thing; a field you set to `null` in the editor simply disappears.
- The hint shown with an HTTP 400 no longer talks about a "corrupted place file" for DataStore writes.

## 1.6.0

### Changed
- Dashboard: player data and DataStore values are edited in a JSON code editor instead of a plain text box.
  - Syntax colours, line numbers, current line, matching brackets.
  - Automatic indentation, closing of `{` `[` `"`, Tab / Shift+Tab on selections.
  - Lines changed since loading marked in the margin.
  - Errors pointed at their exact line and column with a plain explanation (robops finds the position itself:
    browsers don't always give it).
  - Shortcuts: `Ctrl+S` saves (invalid JSON is refused and the caret goes to the error), `Shift+Alt+F` formats,
    *Revert* restores the loaded value. Native undo/redo keeps working through the automatic edits.
  - The *Save player* button stays disabled until something changed.

### Fixed
- Dashboard on phones: a long backup name no longer makes the page scroll sideways.

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
