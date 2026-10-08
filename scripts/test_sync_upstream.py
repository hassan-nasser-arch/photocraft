"""Integration checks using real local Git repositories and simulated GitHub/HTTP."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sync_upstream import Sync, SyncError, WORKFLOW, native, record_deployment


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).resolve().parents[1] / "target/sync-script-tests").resolve()
        self.parent.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="case-", dir=self.parent)).resolve()
        self.seed = self.directory / "seed"
        self.origin = self.directory / "origin.git"
        self.upstream = self.directory / "upstream.git"
        self.fork = self.directory / "fork"
        self.seed.mkdir()
        self.git(self.seed, "init", "--initial-branch=main")
        self.identity(self.seed)
        (self.seed / "app.txt").write_text("base\n")
        (self.seed / WORKFLOW).parent.mkdir(parents=True)
        (self.seed / WORKFLOW).write_text("name: PhotoCraft Pages\n")
        self.commit(self.seed, "base")
        self.git(self.directory, "clone", "--bare", str(self.seed), str(self.origin))
        self.git(self.directory, "clone", "--bare", str(self.seed), str(self.upstream))
        self.git(self.directory, "clone", str(self.seed), str(self.fork))
        self.identity(self.fork)
        self.git(self.fork, "remote", "set-url", "origin", "git@github.com:hassan-nasser-arch/photocraft.git")
        self.git(self.fork, "remote", "add", "upstream", "https://github.com/storytold/photocraft.git")
        for bare, url in [(self.origin, "git@github.com:hassan-nasser-arch/photocraft.git"),
                          (self.upstream, "https://github.com/storytold/photocraft.git")]:
            self.git(self.fork, "config", f"url.{bare.as_uri()}.insteadOf", url)
        (self.fork / "personal.txt").write_text("keep this\n")
        self.commit(self.fork, "personal changes")
        (self.seed / "upstream.txt").write_text("latest upstream\n")
        self.commit(self.seed, "upstream update")
        self.git(self.seed, "push", str(self.upstream), "main")
        self.elapsed = 0
        self.run_conclusion = "success"
        self.marker_requests = 0
        self.stale_requests = 0
        self.broken_asset = False
        self.gh_calls = 0
        self.assets = {"index.html": (b"<title>PhotoCraft</title>\n", "text/html"),
                       "app.js": (b"// generated loader fixture\n", "application/javascript"),
                       "app.wasm": (b"\0asm\x01\0\0\0", "application/wasm")}

    def tearDown(self):
        resolved = self.directory.resolve()
        if resolved.parent != self.parent or not resolved.name.startswith("case-"):
            raise RuntimeError("Refusing to remove a path outside the test workspace")
        def remove_readonly(function, path, error):
            candidate = Path(path).resolve()
            if not candidate.is_relative_to(resolved):
                raise RuntimeError("Cleanup escaped the test workspace")
            os.chmod(candidate, stat.S_IWRITE)
            function(path)
        if sys.version_info >= (3, 12):
            shutil.rmtree(resolved, onexc=remove_readonly)
        else:
            shutil.rmtree(resolved, onerror=remove_readonly)

    def git(self, root, *arguments):
        return native(["git", *arguments], root)

    def identity(self, root):
        self.git(root, "config", "user.name", "PhotoCraft tests")
        self.git(root, "config", "user.email", "test@example.invalid")

    def commit(self, root, message):
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", message)

    def tip(self):
        return self.git(self.directory, "--git-dir", str(self.origin), "rev-parse", "main")

    def runner(self, arguments, root):
        if arguments[0] == "git":
            return native(arguments, root)
        self.gh_calls += 1
        endpoint = arguments[2]
        if endpoint.endswith("/actions/runs"):
            result = {"workflow_runs": [{"id": 42, "path": WORKFLOW, "head_sha": self.tip(),
                       "head_branch": "main", "status": "completed", "conclusion": self.run_conclusion,
                       "html_url": "https://github.com/hassan-nasser-arch/photocraft/actions/runs/42"}]}
        elif endpoint.endswith("/pages"):
            result = {"html_url": "https://example.invalid/photocraft/"}
        else:
            result = {"permissions": {"push": True}}
        return json.dumps(result)

    def http(self, url):
        if "/deployment.json?" in url:
            self.marker_requests += 1
            commit = "0" * 40 if self.marker_requests <= self.stale_requests else self.tip()
            marker = {"commit": commit, "repository": "hassan-nasser-arch/photocraft", "run_id": "42",
                      "assets": [{"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                 for name, (data, _) in self.assets.items()]}
            return json.dumps(marker).encode(), "application/json"
        name = url.split("?")[0].rsplit("/", 1)[1]
        data, mime = self.assets[name]
        if self.broken_asset and name == "app.js":
            data = b"incorrect loader"
        return data, mime

    def sleep(self, seconds):
        self.elapsed += seconds

    def sync(self):
        return Sync(self.fork, "hassan-nasser-arch/photocraft", "fake-gh", timeout=3, poll=1,
                    runner=self.runner, http=self.http, clock=lambda: self.elapsed, sleep=self.sleep)

    def test_merges_pushes_preserves_personal_changes_and_verifies_site(self):
        result = self.sync().execute()
        self.assertEqual(result["commit"], self.tip())
        self.assertEqual((self.fork / "personal.txt").read_text(), "keep this\n")
        self.assertTrue((self.fork / "upstream.txt").is_file())
        self.git(self.fork, "merge-base", "--is-ancestor", result["upstream"], result["commit"])

    def test_dirty_tree_stops_before_fetch_or_push(self):
        before = self.tip()
        (self.fork / "unfinished.txt").write_text("unsaved work")
        with self.assertRaisesRegex(SyncError, "not clean"):
            self.sync().execute()
        self.assertEqual(before, self.tip())
        self.assertEqual(self.gh_calls, 0)

    def test_merge_conflict_preserves_files_and_does_not_push(self):
        before = self.tip()
        (self.seed / "app.txt").write_text("upstream version\n")
        self.commit(self.seed, "upstream conflict")
        self.git(self.seed, "push", str(self.upstream), "main")
        (self.fork / "app.txt").write_text("personal version\n")
        self.commit(self.fork, "personal conflict")
        with self.assertRaisesRegex(SyncError, "nothing was pushed"):
            self.sync().execute()
        self.assertEqual(before, self.tip())
        self.assertTrue((self.fork / ".git/MERGE_HEAD").is_file())

    def test_failed_pages_run_is_reported_as_failure(self):
        self.run_conclusion = "failure"
        with self.assertRaisesRegex(SyncError, "Pages deployment failure"):
            self.sync().execute()
        self.assertEqual(self.marker_requests, 0)

    def test_stale_deployment_is_retried_until_exact_commit_is_live(self):
        self.stale_requests = 2
        self.sync().execute()
        self.assertEqual(self.marker_requests, 3)

    def test_incorrect_published_asset_never_reports_completion(self):
        self.broken_asset = True
        with self.assertRaisesRegex(SyncError, "Timed out.*does not match"):
            self.sync().execute()

    def test_no_updates_verifies_existing_deployment(self):
        first = self.sync().execute()
        second = self.sync().execute()
        self.assertEqual(first["commit"], second["commit"])

    def test_ci_marker_records_commit_run_and_actual_asset_hashes(self):
        website = self.directory / "website"
        website.mkdir()
        for name, (data, _) in self.assets.items():
            (website / name).write_bytes(data)
        with patch.dict(os.environ, GITHUB_SHA="a" * 40, GITHUB_REPOSITORY="hassan-nasser-arch/photocraft", GITHUB_RUN_ID="42"):
            record_deployment(website)
        marker = json.loads((website / "deployment.json").read_text())
        self.assertEqual(marker["commit"], "a" * 40)
        self.assertEqual(marker["run_id"], "42")
        self.assertEqual(len(marker["assets"]), 3)
        for asset in marker["assets"]:
            self.assertEqual(asset["sha256"], hashlib.sha256(self.assets[asset["name"]][0]).hexdigest())


if __name__ == "__main__":
    unittest.main()
