"""End-to-end check of the Windows executables before they are published (release.yml, exe.yml).

Serves the files just built in dist/ as a fake GitHub release on 127.0.0.1, then,
in a temporary folder whose name contains a space:
  1. robops-setup.exe installs robops as a folder (robops.exe + _internal);
  2. a single-file robops.exe, as installed by robops <= 1.7, moves to the folder
     with `robops self-update`;
  3. an update while another robops (the MCP server) runs from the same folder:
     it succeeds, the running robops keeps answering, and the old files are
     deleted at the next launch;
  4. robops-setup.exe --uninstall removes what robops created;
and prints how long `robops --version` takes as a single file and as a folder.

It writes to the current user's registry (PATH, Apps & features): CI only.

    python exe/smoke_test.py dist
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


def serve_release(dist: Path, version: str) -> str:
    files = {name: dist / name for name in ("robops.exe", "robops-setup.exe", "robops-windows.zip", "SHA256SUMS.txt")}
    for path in files.values():
        if not path.is_file():
            fail(f"{path} is missing: run exe/build.py first")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            if self.path == f"/repos/{REPO}/releases/latest":
                body = json.dumps({"tag_name": f"v{version}", "html_url": "",
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def run(env: dict, *args: object, expect: int = 0) -> str:
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=300)
    output = done.stdout + done.stderr
    if done.returncode != expect:
        print(output)
        fail(f"{' '.join(str(a) for a in args)} → exit code {done.returncode} (expected {expect})")
    return output


def leftovers(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if ".old" in p.name]


def main() -> None:
    if os.name != "nt" or not os.environ.get("CI"):
        fail("Windows CI only: this test changes the current user's PATH and Apps & features.")
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    folder_exe = dist / "folder" / "robops" / "robops.exe"
    version = subprocess.run([folder_exe, "--version"], capture_output=True, text=True,
                             env={**os.environ, "ROBOPS_NO_UPDATE": "1"}).stdout.split()[-1]
    base = serve_release(dist, version)
    root = Path(tempfile.mkdtemp(prefix="robops smoke "))
    env = {**os.environ, "ROBOPS_GITHUB_API": base, "ROBOPS_REPO": REPO, "ROBOPS_NO_UPDATE": "1",
           "ROBOPS_STATE_DIR": str(root / "state")}
    env.pop("GH_TOKEN", None)

    # 1. Installer.
    install = root / "Programs" / "robops"
    print(run(env, dist / "robops-setup.exe", "--dir", install, "--quiet"))
    exe = install / "robops.exe"
    if not exe.is_file() or not (install / "_internal").is_dir():
        fail(f"the installer did not put robops.exe and _internal in {install}")
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
    if not (legacy / "_internal").is_dir():
        fail("self-update of a single-file robops did not create _internal")
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

    ask({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    print(run(env, exe, "self-update", "--force"))
    if not leftovers(install / "_internal"):
        fail("expected the replaced files of the running robops to be set aside as .old")
    reply = ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "save_playtest_report",
        "arguments": {"title": "Smoke", "summary": "ok", "players": [{"name": "Sim_1", "outcome": "completed", "feedback": "ok"}]}}})
    if reply.get("result", {}).get("isError") or "Report saved" not in json.dumps(reply):
        fail(f"the running robops did not work after the update: {reply}")
    server.stdin.close()                                          # type: ignore[union-attr]
    server.wait(timeout=30)
    ok("updated while robops was running; the running robops kept working")
    run(env, exe, "--version")
    if leftovers(install):
        fail(f"old files not deleted at the next launch: {leftovers(install)}")
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
