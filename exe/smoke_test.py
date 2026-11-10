"""End-to-end check of the Windows executables before they are published (release.yml, exe.yml).

Serves the files just built in dist/ as a fake GitHub release on 127.0.0.1, then,
in a temporary folder whose name contains a space:
  1. robops-setup.exe installs robops as a folder (robops.exe + _internal);
  2. a single-file robops.exe, as installed by robops <= 1.7, moves to the folder
     with `robops self-update`;
  3. an update to the next version while another robops (the MCP server) runs
     from the same folder: it succeeds, the running robops keeps answering, its
     libraries folder is kept while it runs and deleted at a launch after it stopped;
  4. robops-setup.exe --uninstall removes what robops created;
and prints how long `robops --version` takes as a single file and as a folder.

It writes to the current user's registry (PATH, Apps & features): CI only.

    python exe/build.py && python exe/build.py --dist dist-next --version <version>.1
    python exe/smoke_test.py dist dist-next

Without dist-next, step 3 reinstalls the same version while robops runs.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = "smoke/robops"


def fail(message: str) -> None:
    print(f"✖ {message}", flush=True)
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"✔ {message}", flush=True)


class Releases:
    """A fake GitHub API on 127.0.0.1 serving one dist/ folder as the latest release."""

    def __init__(self) -> None:
        self.dist: Path | None = None
        self.version = ""
        releases = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                base = f"http://127.0.0.1:{self.server.server_address[1]}"
                files = releases.files()
                if self.path == f"/repos/{REPO}/releases/latest":
                    body = json.dumps({"tag_name": f"v{releases.version}", "html_url": "",
                                       "assets": [{"name": n, "url": f"{base}/assets/{n}"} for n in files]}).encode()
                    kind = "application/json"
                elif self.path.startswith("/assets/") and self.path[8:] in files:
                    body, kind = files[self.path[8:]].read_bytes(), "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def files(self) -> dict[str, Path]:
        assert self.dist is not None
        return self.files_of(self.dist)

    def publish(self, dist: Path) -> str:
        for path in [*self.files_of(dist).values(), dist / "folder" / "robops" / "robops.exe"]:
            if not path.is_file():
                fail(f"{path} is missing: run exe/build.py first")
        self.dist = dist
        self.version = version_of(dist / "folder" / "robops" / "robops.exe")
        return self.version

    @staticmethod
    def files_of(dist: Path) -> dict[str, Path]:
        return {n: dist / n for n in ("robops.exe", "robops-setup.exe", "robops-windows.zip", "SHA256SUMS.txt")}


def version_of(exe: Path) -> str:
    done = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                          env={**os.environ, "ROBOPS_NO_UPDATE": "1"})
    return done.stdout.split()[-1]


def run(env: dict, *args: object, expect: int = 0) -> str:
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=300)
    output = done.stdout + done.stderr
    if done.returncode != expect:
        print(output)
        fail(f"{' '.join(str(a) for a in args)} → exit code {done.returncode} (expected {expect})")
    return output


def leftovers(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if ".old" in p.name or p.name.endswith(".deleting")]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # type: ignore[union-attr]  (cp1252 console)
    if os.name != "nt" or not os.environ.get("CI"):
        fail("Windows CI only: this test changes the current user's PATH and Apps & features.")
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    dist_next = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    releases = Releases()
    version = releases.publish(dist)
    root = Path(tempfile.mkdtemp(prefix="robops smoke "))
    env = {**os.environ, "ROBOPS_GITHUB_API": releases.url, "ROBOPS_REPO": REPO, "ROBOPS_NO_UPDATE": "1",
           "ROBOPS_STATE_DIR": str(root / "state")}
    env.pop("GH_TOKEN", None)
    libraries = f"_internal-{version}"

    # 1. Installer.
    install = root / "Programs" / "robops"
    print(run(env, dist / "robops-setup.exe", "--dir", install, "--quiet"))
    exe = install / "robops.exe"
    if not exe.is_file() or not (install / libraries).is_dir():
        fail(f"the installer did not put robops.exe and {libraries} in {install}")
    if version not in run(env, exe, "--version"):
        fail("the installed robops does not give the expected version")
    ok(f"robops-setup.exe installed robops {version} as a folder")
    print(run(env, dist / "robops-setup.exe", "--dir", install, "--quiet"))
    ok("running the installer again: already up to date")

    # 2. A single-file install of robops <= 1.7 moves to the folder.
    legacy = root / "legacy"
    legacy.mkdir()
    shutil.copy2(dist / "robops.exe", legacy / "robops.exe")
    print(run(env, legacy / "robops.exe", "self-update"))
    if not (legacy / libraries).is_dir():
        fail(f"self-update of a single-file robops did not create {libraries}")
    run(env, legacy / "robops.exe", "--version")                 # this launch deletes robops.exe.old
    if leftovers(legacy):
        fail(f"old files left after the move: {leftovers(legacy)}")
    ok("a single-file robops.exe moved to the folder install with robops self-update")

    # 3. Update while another robops runs from the same folder.
    project = root / "game"
    project.mkdir()
    server = subprocess.Popen([str(exe), "mcp", "serve", "--project", str(project)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in server.stdout], daemon=True).start()  # type: ignore[union-attr]

    def ask(message: dict) -> dict:
        server.stdin.write(json.dumps(message).encode() + b"\n")  # type: ignore[union-attr]
        server.stdin.flush()                                      # type: ignore[union-attr]
        try:
            return json.loads(lines.get(timeout=60))
        except queue.Empty:
            fail("the running robops stopped answering")
            raise

    def still_works(step: int) -> None:
        reply = ask({"jsonrpc": "2.0", "id": step, "method": "tools/call", "params": {
            "name": "save_playtest_report",
            "arguments": {"title": f"Smoke {step}", "summary": "ok",
                          "players": [{"name": "Sim_1", "outcome": "completed", "feedback": "ok"}]}}})
        if reply.get("result", {}).get("isError") or "Report saved" not in json.dumps(reply):
            fail(f"the running robops does not work any more: {reply}")

    ask({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    if dist_next:
        new_version = releases.publish(dist_next)
        print(run(env, exe, "self-update"))
        if version_of(exe) != new_version:
            fail(f"robops.exe is not {new_version} after the update")
        if not (install / libraries).is_dir() or not (install / f"_internal-{new_version}").is_dir():
            fail("expected both libraries folders while the old robops runs")
        run(env, exe, "--version")                                # must not delete the folder in use
        if not (install / libraries).is_dir():
            fail("a launch deleted the libraries folder of a robops that is still running")
        still_works(1)
        ok(f"updated {version} → {new_version} while robops {version} was running; it kept working")
    else:
        print(run(env, exe, "self-update", "--force"))
        still_works(1)
        ok("reinstalled while robops was running; it kept working")
    server.stdin.close()                                          # type: ignore[union-attr]
    server.wait(timeout=30)
    run(env, exe, "--version")                                    # nothing uses the old files any more
    if leftovers(install) or (dist_next and (install / libraries).exists()):
        fail(f"old files not deleted after robops stopped: {sorted(p.name for p in install.iterdir())}")
    ok("old files deleted at the next launch")

    # 4. Uninstall.
    print(run(env, install / "robops-setup.exe", "--uninstall", "--dir", install, "--quiet"))
    for _ in range(40):                       # the running setup deletes itself a few seconds later
        if not install.exists():
            break
        time.sleep(0.5)
    if install.exists():
        fail(f"still there after uninstalling: {sorted(p.name for p in install.rglob('*'))}")
    ok("robops-setup.exe --uninstall removed the folder")

    # Startup time.
    def timing(path: Path) -> float:
        samples = []
        for _ in range(9):
            start = time.perf_counter()
            subprocess.run([str(path), "--version"], capture_output=True, env=env)
            samples.append(time.perf_counter() - start)
        return statistics.median(samples)

    single, folder = timing(dist / "robops.exe"), timing(legacy / "robops.exe")
    ok(f"robops --version: {single:.2f} s as a single file, {folder:.2f} s as a folder")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
