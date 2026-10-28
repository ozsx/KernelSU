"""Kiểm thử integration bằng các Git bare remotes cục bộ, không dùng Internet."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "auto_sync.py"


def run(*a, cwd=None, check=True):
    p = subprocess.run(list(map(str, a)), cwd=cwd, text=True, capture_output=True)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, a))}\n{p.stdout}\n{p.stderr}")
    return p


def git(where, *args):
    return run("git", "-C", where, *args).stdout.strip()


class AutoSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.upstream = root / "upstream.git"
        self.seed = root / "seed"
        self.local = root / "local"
        run("git", "init", "--bare", "-b", "main", self.origin)
        run("git", "init", "--bare", "-b", "master", self.upstream)
        run("git", "init", "-b", "master", self.seed)
        self.configure(self.seed)
        (self.seed / "hello.txt").write_text("base\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "A")
        self.base = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "remote", "add", "upstream", str(self.upstream))
        git(self.seed, "push", "upstream", "master")
        git(self.seed, "push", "origin", "master:main")
        git(self.seed, "branch", "personal/base")
        git(self.seed, "push", "origin", "personal/base")
        git(self.seed, "switch", "-c", "dev/personal")
        (self.seed / "p1.txt").write_text("personal 1\n")
        git(self.seed, "add", ".")
        self.personal_commit("P1")
        (self.seed / "p2.txt").write_text("personal 2\n")
        git(self.seed, "add", ".")
        self.personal_commit("P2")
        git(self.seed, "push", "origin", "dev/personal")
        git(self.seed, "switch", "master")
        run("git", "clone", self.origin, self.local)
        self.configure(self.local)

    def configure(self, path):
        git(path, "config", "user.name", "Git Test Maintainer")
        git(path, "config", "user.email", "maintainer@example.org")

    def personal_commit(self, message):
        git(self.seed, "commit", "--author=Alice Example <alice@example.org>", "-m", message)

    def upstream_commit(self, name="upstream.txt", content="upstream\n", message="B"):
        git(self.seed, "switch", "master")
        (self.seed / name).write_text(content)
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", message)
        git(self.seed, "push", "upstream", "master")
        return git(self.seed, "rev-parse", "HEAD")

    def tag(self, name, target="HEAD"):
        git(self.seed, "tag", "-a", name, target, "-m", f"upstream {name}")
        git(self.seed, "push", "upstream", f"refs/tags/{name}")

    def execute(self, *args, ok=True):
        p = run(sys.executable, SCRIPT, "--repo", self.local,
                "--upstream-url", self.upstream,
                "--tagger-email", "123456+ozsx@users.noreply.github.com", *args, check=False)
        if ok and p.returncode:
            self.fail(p.stdout + "\n" + p.stderr)
        if not ok and not p.returncode:
            self.fail("Expected failure: " + p.stdout)
        return json.loads((self.local / "sync-reports" / "report.json").read_text())

    def branch(self, name="main"):
        p = git(self.local, "ls-remote", "origin", f"refs/heads/{name}")
        return p.split()[0] if p else None

    def tag_commit(self, tag):
        # Clone tag về một namespace tạm để peel annotated tag.
        git(self.local, "fetch", "--no-tags", "origin",
            f"+refs/tags/{tag}:refs/test-tags/{tag}")
        return git(self.local, "rev-parse", f"refs/test-tags/{tag}^{{commit}}")

    def test_push_main_and_tag_at_original_upstream_release(self):
        b = self.upstream_commit("b.txt", "b\n", "B")
        self.tag("v1.0")
        c = self.upstream_commit("c.txt", "c\n", "C")
        initial = self.execute()
        self.assertEqual(initial["status"], "dry-run")
        self.assertEqual(self.branch(), self.base)
        result = self.execute("--push")
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(len(result["tags_new"]), 1)
        main = self.branch()
        self.assertNotEqual(main, self.base)
        self.assertEqual(main, result["main_after"])
        self.assertEqual(main, self.branch("sync/staging"))
        self.assertEqual(self.branch(f"sync/backups/{self.base}"), self.base)
        # Release phải nằm trên B, không phải C.
        release = self.tag_commit("v1.0-personal")
        self.assertEqual(git(self.local, "rev-parse", f"{release}~2"), b)
        self.assertEqual(git(self.local, "rev-parse", f"{main}~2"), c)
        self.assertNotEqual(main, release)
        for oid in git(self.local, "rev-list", "--reverse", f"{c}..{main}").splitlines():
            self.assertEqual(git(self.local, "log", "-1", "--format=%an <%ae>", oid),
                             "Alice Example <alice@example.org>")
        again = self.execute("--push")
        self.assertEqual(again["status"], "skip")
        self.assertEqual(self.branch(), main)
        # Tag mới trên commit cũ cũng được nhận ra không cần sync-state.
        self.tag("v1.0.1", b)
        new_tag = self.execute("--push")
        self.assertEqual(new_tag["status"], "pushed")
        self.assertEqual(self.branch(), main)
        self.assertEqual(self.tag_commit("v1.0.1-personal"), release)


    def test_annotated_tag_uses_custom_tagger_without_changing_personal_author(self):
        # Dùng tagger tuỳ chọn là GitHub account, không giả làm author commit.
        self.tag("v-identity")
        result = self.execute("--push", "--tagger-name", "ozsx",
                              "--tagger-email", "123456+ozsx@users.noreply.github.com")
        self.assertEqual(result["status"], "pushed")
        git(self.local, "fetch", "--no-tags", "origin",
            "+refs/tags/v-identity-personal:refs/test-tags/v-identity-personal")
        tag_oid = git(self.local, "rev-parse", "refs/test-tags/v-identity-personal")
        self.assertEqual(git(self.local, "cat-file", "-t", tag_oid), "tag")
        tag_text = git(self.local, "cat-file", "-p", tag_oid)
        self.assertIn("tagger ozsx <123456+ozsx@users.noreply.github.com>", tag_text)
        commit_sha = self.tag_commit("v-identity-personal")
        self.assertEqual(git(self.local, "log", "-1", "--format=%an <%ae>", commit_sha),
                         "Alice Example <alice@example.org>")
        self.assertEqual(git(self.local, "log", "-1", "--format=%cn <%ce>", commit_sha),
                         "ozsx <123456+ozsx@users.noreply.github.com>")

    def test_no_identity_email_fails_instead_of_falling_back(self):
        # Không truyền email và xoá biến môi trường nếu có: phải báo lỗi rõ ràng.
        import os
        env = dict(os.environ)
        env.pop("SYNC_TAGGER_EMAIL", None)
        p = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.local),
                            "--upstream-url", str(self.upstream), "--push"],
                           capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SYNC_TAGGER_EMAIL", p.stderr)
        self.assertEqual(self.branch(), self.base)

    def test_existing_tag_is_not_rewritten_when_tagger_identity_changes(self):
        self.tag("v-stable")
        self.execute("--push")
        old_oid = git(self.local, "ls-remote", "--refs", "--tags", "origin",
                      "refs/tags/v-stable-personal").split()[0]
        self.execute("--push", "--tagger-name", "ozsx",
                     "--tagger-email", "123456+ozsx@users.noreply.github.com")
        new_oid = git(self.local, "ls-remote", "--refs", "--tags", "origin",
                      "refs/tags/v-stable-personal").split()[0]
        self.assertEqual(old_oid, new_oid)

    def test_tag_from_release_branch_not_on_master(self):
        # Master đã lên C, nhưng release tách ra từ A và tag nằm trên R.
        master = self.upstream_commit("master.txt", "new master\n", "Master B")
        git(self.seed, "switch", "-c", "release/legacy", self.base)
        (self.seed / "release.txt").write_text("release-only\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "R")
        release_root = git(self.seed, "rev-parse", "HEAD")
        self.tag("v-release")  # annotated tag; release branch không push lên upstream
        git(self.seed, "switch", "master")
        state = self.execute("--push")
        self.assertEqual(state["status"], "pushed")
        self.assertEqual(len(state["tags_new"]), 1)
        self.assertEqual(state["tags_new"][0]["upstream_commit"], release_root)
        self.assertEqual(state["tags_new"][0]["personal_count"], 2)
        main = self.branch()
        out_tag = self.tag_commit("v-release-personal")
        self.assertEqual(git(self.local, "rev-parse", f"{main}~2"), master)
        self.assertEqual(git(self.local, "rev-parse", f"{out_tag}~2"), release_root)
        self.assertEqual(git(self.local, "rev-list", "--first-parent", "--count", f"{release_root}..{out_tag}"), "2")
        self.assertEqual(git(self.local, "show", f"{out_tag}:release.txt"), "release-only")
        self.assertEqual(git(self.local, "log", "-1", "--format=%an <%ae>", out_tag),
                         "Alice Example <alice@example.org>")
        self.assertEqual(self.execute("--push")["status"], "skip")

    def test_tag_from_unrelated_orphan_commit(self):
        # Commit orphan không có chung lịch sử với main/personal/base.
        git(self.seed, "switch", "--orphan", "isolated-release")
        (self.seed / "orphan.txt").write_text("independent release\n")
        git(self.seed, "add", "orphan.txt")
        git(self.seed, "commit", "-m", "Orphan release")
        orphan = git(self.seed, "rev-parse", "HEAD")
        self.tag("v-orphan")
        git(self.seed, "switch", "master")
        state = self.execute("--push")
        self.assertEqual(state["status"], "pushed")
        result = self.tag_commit("v-orphan-personal")
        self.assertEqual(git(self.local, "rev-parse", f"{result}~2"), orphan)
        self.assertEqual(git(self.local, "rev-list", "--first-parent", "--count", f"{orphan}..{result}"), "2")
        self.assertEqual(self.branch("sync/staging"), self.branch())

    def test_lightweight_tag_on_separate_branch(self):
        # Tag lightweight cũng phải được peel đúng về commit nguồn.
        git(self.seed, "switch", "-c", "release/light", self.base)
        (self.seed / "light.txt").write_text("lightweight\n")
        git(self.seed, "add", "light.txt")
        git(self.seed, "commit", "-m", "Release")
        release_commit = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "tag", "v-light")
        git(self.seed, "push", "upstream", "refs/tags/v-light")
        git(self.seed, "switch", "master")
        state = self.execute("--push")
        self.assertEqual(state["tags_new"][0]["upstream_commit"], release_commit)
        self.assertEqual(git(self.local, "rev-parse", f"{self.tag_commit('v-light-personal')}~2"), release_commit)

    def test_tag_conflict_prevents_all_pushes(self):
        # main cherry-pick được, tag ở nhánh khác có xung đột -> không push gì.
        git(self.seed, "switch", "dev/personal")
        (self.seed / "hello.txt").write_text("patch owner\n")
        git(self.seed, "add", "hello.txt")
        self.personal_commit("Patch hello")
        git(self.seed, "push", "origin", "dev/personal")
        git(self.seed, "switch", "-c", "release/conflicted", self.base)
        (self.seed / "hello.txt").write_text("upstream divergent\n")
        git(self.seed, "add", "hello.txt")
        git(self.seed, "commit", "-m", "Release changes hello")
        self.tag("v-conflicted")
        git(self.seed, "switch", "master")
        error = self.execute("--push", "--strategy", "fail", ok=False)
        self.assertEqual(error["status"], "failed")
        self.assertIn("tag:v-conflicted", error["error"])
        self.assertEqual(self.branch(), self.base)
        self.assertIsNone(self.branch("sync/staging"))
        self.assertEqual(git(self.local, "ls-remote", "origin", "refs/tags/v-conflicted-personal"), "")

    def test_conflict_theirs_and_stable_rebuild(self):
        git(self.seed, "switch", "dev/personal")
        (self.seed / "hello.txt").write_text("personal\n")
        git(self.seed, "add", ".")
        self.personal_commit("P3 conflict")
        git(self.seed, "push", "origin", "dev/personal")
        self.upstream_commit("hello.txt", "upstream\n")
        state = self.execute("--push", "--strategy", "theirs")
        self.assertEqual(state["status"], "pushed")
        main = self.branch()
        self.assertEqual(git(self.local, "show", f"{main}:hello.txt"), "personal")
        self.assertEqual(self.execute("--push")["status"], "skip")
        self.assertEqual(self.branch(), main)

    def test_append_personal_commit_and_sync_without_upstream_change(self):
        self.upstream_commit("b.txt", "upstream B\n")
        first = self.execute("--push")
        first_sha = self.branch()
        self.assertEqual(first["status"], "pushed")
        git(self.seed, "switch", "dev/personal")
        (self.seed / "p3.txt").write_text("personal 3\n")
        git(self.seed, "add", "p3.txt")
        self.personal_commit("P3")
        git(self.seed, "push", "origin", "dev/personal")
        second = self.execute("--push")
        self.assertEqual(second["status"], "pushed")
        self.assertNotEqual(first_sha, self.branch())
        self.assertEqual(len(second["applied"]), 3)
        self.assertEqual(self.branch(f"sync/backups/{first_sha}"), first_sha)
        self.assertEqual(self.execute("--push")["status"], "skip")

    def test_conflict_fail_and_failed_check_keep_main(self):
        git(self.seed, "switch", "dev/personal")
        (self.seed / "hello.txt").write_text("personal\n")
        git(self.seed, "add", ".")
        self.personal_commit("conflicting")
        git(self.seed, "push", "origin", "dev/personal")
        self.upstream_commit("hello.txt", "upstream\n")
        self.tag("v2")
        err = self.execute("--push", "--strategy", "fail", ok=False)
        self.assertEqual(err["status"], "failed")
        self.assertIn("Conflict", err["error"])
        self.assertEqual(self.branch(), self.base)
        self.assertIsNone(self.branch("sync/staging"))
        self.assertFalse(git(self.local, "ls-remote", "origin", "refs/tags/v2-personal"))
        err = self.execute("--push", "--strategy", "theirs", "--check-cmd", "exit 3", ok=False)
        self.assertEqual(err["status"], "failed")
        self.assertEqual(self.branch(), self.base)
        self.assertFalse(git(self.local, "ls-remote", "origin", "refs/tags/v2-personal"))

    def rewrite_upstream_as_orphan(self, message="New upstream root"):
        """Giả lập upstream force-push, KHÔNG còn chung tổ tiên với personal/base."""
        git(self.seed, "switch", "--orphan", "rewrite-root")
        (self.seed / "hello.txt").write_text("rewritten upstream\n")
        (self.seed / "rewrite.txt").write_text("new history\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", message)
        rewritten = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "branch", "-f", "master", rewritten)
        git(self.seed, "push", "--force", "upstream", "master")
        return rewritten

    def test_rewrite_upstream_without_state(self):
        self.upstream_commit("b.txt", "old history\n")
        first = self.execute("--push")
        old_main = self.branch()
        self.assertEqual(first["status"], "pushed")
        self.assertIsNone(self.branch("sync/state"))
        rewritten = self.rewrite_upstream_as_orphan()
        self.tag("v3.0", rewritten)
        new = self.execute("--push")
        self.assertEqual(new["status"], "pushed")
        self.assertEqual(git(self.local, "rev-parse", f"{self.branch()}~2"), rewritten)
        self.assertEqual(git(self.local, "rev-parse", f"{self.tag_commit('v3.0-personal')}~2"), rewritten)
        self.assertEqual(self.branch(f"sync/backups/{old_main}"), old_main)
        self.assertIsNone(self.branch("sync/state"))
        self.assertEqual(self.execute("--push")["status"], "skip")

    def test_main_equals_upstream_stale_staging(self):
        rewritten = self.rewrite_upstream_as_orphan()
        git(self.seed, "push", "origin", f"{self.base}:refs/heads/sync/staging")
        git(self.seed, "push", "--force", "origin", f"{rewritten}:refs/heads/main")
        result = self.execute("--push")
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(self.branch(), result["main_after"])
        self.assertEqual(self.branch("sync/staging"), result["main_after"])
        self.assertIsNone(self.branch("sync/state"))

    def test_tag_new_older_than_personal_base(self):
        # Không lưu state, chỉ so tên tag upstream với tag đã có trên fork.
        self.upstream_commit("b.txt", "B\n")
        self.execute("--push")
        self.tag("v-old", self.base)
        result = self.execute("--push")
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(len(result["tags_new"]), 1)
        release = self.tag_commit("v-old-personal")
        self.assertEqual(git(self.local, "rev-parse", f"{release}~2"), self.base)

    def test_rewrite_unknown_main_is_backed_up(self):
        self.upstream_commit("b.txt", "B\n")
        self.execute("--push")
        git(self.seed, "fetch", "origin", "main")
        git(self.seed, "switch", "-c", "manual-edit", "FETCH_HEAD")
        (self.seed / "manual.txt").write_text("manual change\n")
        git(self.seed, "add", "manual.txt")
        git(self.seed, "commit", "-m", "Manual")
        git(self.seed, "push", "origin", "HEAD:main")
        old_main = self.branch()
        rewrite = self.rewrite_upstream_as_orphan()
        result = self.execute("--push")
        self.assertEqual(result["status"], "pushed")
        self.assertNotEqual(self.branch(), old_main)
        self.assertEqual(self.branch(f"sync/backups/{old_main}"), old_main)
        self.assertEqual(git(self.local, "rev-parse", f"{self.branch()}~2"), rewrite)

    def test_max_new_tags_optional_limit(self):
        self.tag("v0", self.base)
        self.tag("v1", self.base)
        self.assertEqual(self.execute("--push", "--max-new-tags", "1", ok=False)["status"], "failed")
        self.assertIsNone(self.branch("sync/staging"))
        self.assertEqual(len(self.execute("--push")["tags_new"]), 2)

    def test_no_stale_state_dependency(self):
        self.upstream_commit("b.txt", "B\n")
        self.execute("--push")
        # v3.1 để lại sync/state sai cũng không được khiến v4 bị kẹt.
        git(self.seed, "switch", "master")
        git(self.seed, "push", "origin", f"{self.base}:refs/heads/sync/state")
        self.upstream_commit("c.txt", "C\n", "C")
        result = self.execute("--push")
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(self.branch("sync/state"), self.base)  # không chạm state


if __name__ == "__main__":
    unittest.main()
