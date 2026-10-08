#!/usr/bin/env python3
"""Merge upstream PhotoCraft, push main, and verify its completed Pages deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

UPSTREAM = "storytold/photocraft"
WORKFLOW = ".github/workflows/pages.yml"


class SyncError(RuntimeError):
    pass


def record_deployment(directory):
    directory = Path(directory)
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in (".html", ".js", ".wasm"))
    if not (directory / "index.html").is_file() or not any(p.suffix == ".wasm" for p in files):
        raise SyncError("The website build is missing index.html or WebAssembly.")
    marker = {
        "commit": os.environ["GITHUB_SHA"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "assets": [{"name": p.name, "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
    }
    (directory / "deployment.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def native(arguments, root):
    result = subprocess.run(arguments, cwd=root, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise SyncError((result.stderr or result.stdout).strip() or f"{arguments[0]} failed.")
    return result.stdout.strip()


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Photocraft-dev", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=30) as response:
        limit = 128 * 1024 * 1024
        data = response.read(limit + 1)
        if len(data) > limit:
            raise SyncError("A published asset exceeds the 128 MiB verification limit.")
        return data, response.headers.get_content_type()


class Sync:
    def __init__(self, root, repository, gh, timeout=1800, poll=15, runner=native,
                 http=download, clock=time.monotonic, sleep=time.sleep):
        self.root, self.repository, self.gh = Path(root), repository, gh
        self.timeout, self.poll = timeout, poll
        self.runner, self.http, self.clock, self.sleep = runner, http, clock, sleep

    def git(self, *arguments):
        return self.runner(["git", *arguments], self.root)

    def api(self, endpoint, *arguments):
        output = self.runner([self.gh, "api", endpoint, *arguments], self.root)
        return json.loads(output)

    def check_remote(self, remote, repository):
        url = self.git("config", "--get", f"remote.{remote}.url")
        allowed = {f"https://github.com/{repository}", f"https://github.com/{repository}.git",
                   f"git@github.com:{repository}", f"git@github.com:{repository}.git"}
        if url not in allowed:
            raise SyncError(f"Remote '{remote}' must point to github.com/{repository}.")

    def verify_site(self, site, commit, run_id):
        suffix = f"?revision={commit}&run={run_id}"
        raw, _ = self.http(site + "deployment.json" + suffix)
        if len(raw) > 1024 * 1024:
            raise SyncError("The deployment marker is too large.")
        marker = json.loads(raw)
        if (marker.get("commit") != commit or marker.get("repository") != self.repository
                or str(marker.get("run_id")) != str(run_id)):
            raise SyncError("The published site is still serving a different deployment.")
        assets = marker.get("assets", [])
        if not isinstance(assets, list) or not 1 <= len(assets) <= 100:
            raise SyncError("The deployment marker has an invalid asset list.")
        names = [asset.get("name", "") for asset in assets]
        if "index.html" not in names or not any(n.endswith(".wasm") for n in names) or not any(n.endswith(".js") for n in names):
            raise SyncError("The deployment marker is missing required website assets.")
        for asset in assets:
            name = asset.get("name", "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(html|js|wasm)", name):
                raise SyncError("The deployment marker has an invalid asset name.")
            data, mime = self.http(site + name + suffix)
            if len(data) != asset.get("bytes") or hashlib.sha256(data).hexdigest() != asset.get("sha256"):
                raise SyncError(f"Published asset does not match the build: {name}")
            if name.endswith(".wasm") and (mime != "application/wasm" or not data.startswith(b"\0asm")):
                raise SyncError(f"Invalid WASM response: {name}")
            if name.endswith(".js") and mime not in ("text/javascript", "application/javascript"):
                raise SyncError(f"Invalid JavaScript MIME type: {name}")

    def execute(self):
        if self.git("branch", "--show-current") != "main":
            raise SyncError("Switch to main before syncing.")
        if self.git("status", "--porcelain"):
            raise SyncError("The working tree is not clean. Commit or stash your changes first.")
        for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"):
            if (self.root / self.git("rev-parse", "--git-path", state)).exists():
                raise SyncError("Finish the current merge/rebase before syncing.")
        self.check_remote("origin", self.repository)
        self.check_remote("upstream", UPSTREAM)
        repo = self.api(f"repos/{self.repository}")
        if not repo.get("permissions", {}).get("push"):
            raise SyncError("GitHub CLI is not authenticated with write access to this repository.")
        print("Fetching personal repo and upstream main...", flush=True)
        self.git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        self.git("fetch", "--no-tags", "upstream", "refs/heads/main:refs/remotes/upstream/main")
        upstream = self.git("rev-parse", "refs/remotes/upstream/main")
        for reference in ("refs/remotes/origin/main", "refs/remotes/upstream/main"):
            try:
                print(self.git("merge", "--no-edit", reference), flush=True)
            except SyncError as error:
                raise SyncError(f"Merge stopped; nothing was pushed. Resolve/commit the merge or abort it before rerunning.\n{error}") from error
        commit = self.git("rev-parse", "HEAD")
        self.git("merge-base", "--is-ancestor", upstream, commit)
        if not (self.root / WORKFLOW).is_file():
            raise SyncError("The Pages workflow is missing after merging; nothing was pushed.")
        print(self.git("push", "origin", "HEAD:refs/heads/main") or "Push completed.", flush=True)
        print(f"Waiting for PhotoCraft Pages for commit {commit[:12]}...", flush=True)
        deadline = self.clock() + self.timeout
        started = self.clock()
        dispatched = False
        last_message = "Waiting for GitHub to create the workflow run."
        last_printed = None
        while self.clock() < deadline:
            runs = self.api(f"repos/{self.repository}/actions/runs", "--method", "GET",
                            "-f", f"head_sha={commit}", "-f", "branch=main", "-f", "per_page=100")
            matching = [r for r in runs.get("workflow_runs", [])
                        if r.get("path") == WORKFLOW and r.get("head_sha") == commit and r.get("head_branch") == "main"]
            if matching:
                run = max(matching, key=lambda r: r["id"])
                last_message = f"Pages: {run['status']} ? {run['html_url']}"
                if run["status"] == "completed":
                    if run.get("conclusion") != "success":
                        raise SyncError(f"Pages deployment {run.get('conclusion')}: {run['html_url']}")
                    try:
                        pages = self.api(f"repos/{self.repository}/pages")
                        site = pages["html_url"].rstrip("/") + "/"
                        self.verify_site(site, commit, run["id"])
                        print(f"Deployment complete and published assets verified: {site}", flush=True)
                        return {"commit": commit, "upstream": upstream, "site": site, "run": run["html_url"]}
                    except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                        last_message = f"Waiting for the published files: {error}"
            elif not dispatched and self.clock() - started >= 30:
                self.runner([self.gh, "workflow", "run", "pages.yml", "--repo", self.repository, "--ref", "main"], self.root)
                dispatched = True
                last_message = "Started Pages manually because no matching run was available."
            if last_message != last_printed:
                print(last_message, flush=True)
                last_printed = last_message
            self.sleep(min(self.poll, max(0, deadline - self.clock())))
        raise SyncError(f"Timed out waiting for a complete Pages deployment. {last_message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="hassan-nasser-arch/photocraft")
    parser.add_argument("--gh", help="Path to GitHub CLI; otherwise PATH or target/github-tools/bin/gh.exe")
    parser.add_argument("--timeout", type=int, default=1800, help="Deployment wait limit in seconds (default: 1800)")
    parser.add_argument("--poll", type=int, default=15, help="Poll interval in seconds (default: 15)")
    parser.add_argument("--record-deployment", metavar="DIRECTORY", help="CI: write the deployment marker for an already built website")
    args = parser.parse_args()
    try:
        if args.record_deployment:
            record_deployment(args.record_deployment)
            return 0
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.repo):
            raise SyncError("Invalid GitHub repository name.")
        if not 1 <= args.timeout <= 86400 or not 1 <= args.poll <= 60:
            raise SyncError("Timeout must be 1?86400 seconds; poll interval must be 1?60 seconds.")
        root = Path(__file__).resolve().parent.parent
        gh = args.gh or shutil.which("gh")
        if not gh:
            cached = root / "target/github-tools/bin/gh.exe"
            if cached.is_file():
                gh = str(cached)
        if not gh:
            raise SyncError("Install GitHub CLI and run 'gh auth login', or pass --gh PATH.")
        Sync(root, args.repo, gh, args.timeout, args.poll).execute()
        return 0
    except (SyncError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"PhotoCraft sync failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("PhotoCraft sync interrupted; check Git and Actions before rerunning.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
