#!/usr/bin/env python3
"""
Run the tracker server and keep it up to date with GitHub.

    python updater.py [any app.py options, e.g. --port 8000]

What it does:
  * starts app.py and restarts it if it ever crashes,
  * every few minutes runs `git fetch`; when main has new commits it
      - waits until GitHub's CI checks for that commit have passed,
      - fast-forwards your copy (`git pull --ff-only`) and restarts the server.

It never touches the data/ folder (git ignores it), and it refuses to update if
you've edited tracked files locally. Nothing on GitHub needs access to your PC:
your PC only reads from GitHub.

Environment variables:
  UPDATE_INTERVAL   seconds between checks          (default 300)
  UPDATE_BRANCH     branch to follow                (default main)
  UPDATE_REQUIRE_CI 1 = wait for passing CI, 0 = don't  (default 1)
  GITHUB_TOKEN      only needed if the repo is private (to read CI results)
  (TRACKER_* variables are passed through to app.py.)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INTERVAL = int(os.environ.get("UPDATE_INTERVAL", "300"))
BRANCH = os.environ.get("UPDATE_BRANCH", "main")
REQUIRE_CI = os.environ.get("UPDATE_REQUIRE_CI", "1") != "0"
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def github_repo() -> str | None:
    """'owner/repo' from the origin URL, or None if origin isn't GitHub."""
    url = git("remote", "get-url", "origin", check=False)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def ci_state(repo: str, sha: str) -> str:
    """'success', 'pending' or 'failure' for the GitHub checks on a commit."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "bookkeeping-tracker-updater"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=20) as r:
        runs = json.load(r).get("check_runs", [])
    if not runs:
        return "pending"                       # CI hasn't started yet
    if any(run["status"] != "completed" for run in runs):
        return "pending"
    ok = all(run["conclusion"] in ("success", "skipped", "neutral") for run in runs)
    return "success" if ok else "failure"


class Server:
    def __init__(self, args: list[str]):
        self.args = args
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        log(f"starting server (commit {git('rev-parse', '--short', 'HEAD', check=False) or '?'})")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), *self.args], cwd=ROOT)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None


def check_for_update(repo: str | None, rejected: set[str]) -> bool:
    """Pull and return True if a new, CI-approved commit was installed."""
    git("fetch", "--quiet", "origin", BRANCH)
    local, remote = git("rev-parse", "HEAD"), git("rev-parse", f"origin/{BRANCH}")
    if local == remote or remote in rejected:
        return False
    if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{BRANCH}"], cwd=ROOT).returncode != 0:
        log("local copy has commits that aren't on GitHub; not updating (run `git status` to see why)")
        rejected.add(remote)
        return False
    if git("status", "--porcelain", "--untracked-files=no"):
        log("tracked files were edited locally; not updating (run `git status` to see why)")
        return False
    if REQUIRE_CI and repo:
        try:
            state = ci_state(repo, remote)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"couldn't read CI results ({e}); will try again later")
            return False
        if state == "pending":
            log(f"update {remote[:7]} found; waiting for CI to finish")
            return False
        if state == "failure":
            log(f"update {remote[:7]} failed CI; skipping it")
            rejected.add(remote)
            return False
    git("pull", "--ff-only", "--quiet", "origin", BRANCH)
    log(f"updated {local[:7]} -> {remote[:7]}: {git('log', '-1', '--format=%s')}")
    return True


def main() -> None:
    server = Server(sys.argv[1:])
    repo = github_repo()
    if not (ROOT / ".git").exists():
        sys.exit("updater.py must run inside a git clone of the repository.")
    if REQUIRE_CI and not repo:
        log("origin isn't a GitHub repo, so CI results can't be checked; updates will install without waiting for CI")

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)

    rejected: set[str] = set()
    failures = 0
    server.start()
    next_check = time.monotonic() + min(10, INTERVAL)
    log(f"checking GitHub ({repo or 'origin'}, {BRANCH}) every {INTERVAL // 60 or 1} min; Ctrl+C to stop")
    try:
        while not stop:
            time.sleep(1)
            if not server.alive():
                failures += 1
                wait = min(60, 2 ** failures)
                log(f"server stopped unexpectedly; restarting in {wait}s")
                time.sleep(wait)
                server.start()
            elif failures and time.monotonic() > next_check:
                failures = 0
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + INTERVAL
                try:
                    if check_for_update(repo, rejected):
                        server.stop()
                        server.start()
                except RuntimeError as e:
                    log(f"update check failed: {e}")
    finally:
        server.stop()
        log("stopped")


if __name__ == "__main__":
    main()
