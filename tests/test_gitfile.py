#!/usr/bin/env python3
"""Integration tests for gitfile.py.

Run:  python3 tests/test_gitfile.py
No external services: the "upstream" is a throwaway local git repository.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "gitfile.py"
PY = sys.executable

FOO = """package demo;

public class Foo {
    public int value() {
        return 1;
    }
}
"""

POM = """<project>
  <version>1</version>
</project>
"""

BAR = """package demo;

public class Bar {
    public int value() {
        return 2;
    }
}
"""


def sh(cmd, cwd):
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise AssertionError("command failed: %s\n%s" % (" ".join(cmd), proc.stdout))
    return proc.stdout


def git(cwd, *args):
    return sh(["git", *args], cwd)


def gitfile(cwd, *args):
    return subprocess.run(
        [PY, str(APP), *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class GitfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitfile-test-"))
        self.upstream = self.tmp / "upstream"
        self.project = self.tmp / "project"

        self.upstream.mkdir()
        git(self.upstream, "init", "-q")
        git(self.upstream, "config", "user.email", "test@example.com")
        git(self.upstream, "config", "user.name", "Test")
        (self.upstream / "mod/src/main/java/demo").mkdir(parents=True)
        (self.upstream / "mod/src/main/java/demo/Foo.java").write_text(FOO)
        (self.upstream / "mod/src/main/java/demo/Bar.java").write_text(BAR)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "initial")
        self.ref = git(self.upstream, "rev-parse", "HEAD").strip()

        self.project.mkdir()
        (self.project / "upstream.ref").write_text(self.ref + "\n")
        (self.project / "gitfile.json").write_text(json.dumps({
            "upstream": str(self.upstream),
            "refFile": "upstream.ref",
            "patches": "patches",
            "baselineDir": ".gitfile/baseline",
            "upstreamDir": ".gitfile/upstream",
            "modules": ["mod"],
        }, indent=2))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def bump_upstream(self, relpath, text):
        path = self.upstream / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "upstream change")
        self.ref = git(self.upstream, "rev-parse", "HEAD").strip()
        (self.project / "upstream.ref").write_text(self.ref + "\n")

    def foo(self):
        return self.project / "mod/src/main/java/demo/Foo.java"

    def patch(self, rel):
        return self.project / "patches" / (rel + ".patch")

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    # -- tests -------------------------------------------------------------
    def test_sync_creates_work_tree(self):
        self.ok(gitfile(self.project, "sync"))
        self.assertEqual(self.foo().read_text(), FOO)
        self.assertTrue((self.project / ".gitfile/baseline/.gitfile-applied").is_file())

    def test_edit_then_update_writes_one_patch_per_file(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(self.project, "update"))
        patch_text = self.patch("mod/src/main/java/demo/Foo.java").read_text()
        self.assertIn("-        return 1;", patch_text)
        self.assertIn("+        return 42;", patch_text)
        self.assertFalse(self.patch("mod/src/main/java/demo/Bar.java").exists())

    def test_clean_upstream_change_keeps_patch_and_rebases(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("public class Foo {", "// our change\npublic class Foo {"))
        self.ok(gitfile(self.project, "update"))

        # Upstream change in a different file: patches still apply cleanly.
        self.bump_upstream("mod/src/main/java/demo/Bar.java", BAR.replace("return 2;", "return 3;"))
        out = self.ok(gitfile(self.project, "sync"))
        self.assertIn("0 conflict(s)", out)

        # Upstream change in a non-overlapping part of the same file:
        # three-way merge keeps both changes.
        self.foo()  # sanity
        new_foo = FOO.replace("return 1;", "return 7;")  # upstream edits the patched line's neighbour
        self.bump_upstream("mod/src/main/java/demo/Foo.java", new_foo)
        out = self.ok(gitfile(self.project, "sync"))
        self.assertIn("rebased", out)
        merged = self.foo().read_text()
        self.assertIn("// our change", merged)
        self.assertIn("return 7;", merged)
        self.assertNotIn("return 42;", merged)  # our patch never touched the value here

    def test_conflicting_upstream_change_marks_conflict_and_update_refuses(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(self.project, "update"))

        self.bump_upstream("mod/src/main/java/demo/Foo.java", FOO.replace("return 1;", "return 99;"))
        result = gitfile(self.project, "sync")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("conflict", result.stdout)
        text = self.foo().read_text()
        self.assertIn("<<<<<<<", text)
        self.assertIn("return 42;", text)
        self.assertIn("return 99;", text)

        blocked = gitfile(self.project, "update")
        self.assertEqual(blocked.returncode, 1, blocked.stdout)
        self.assertIn("unresolved conflicts", blocked.stdout)

        # Resolve: keep our value and drop the markers.
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(self.project, "update", "--force"))
        patch_text = self.patch("mod/src/main/java/demo/Foo.java").read_text()
        self.assertIn("+        return 42;", patch_text)
        self.assertNotIn("<<<<<<<", patch_text)

    def test_upstream_deletes_modified_file_is_a_conflict(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(self.project, "update"))

        (self.upstream / "mod/src/main/java/demo/Foo.java").unlink()
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "delete Foo")
        self.ref = git(self.upstream, "rev-parse", "HEAD").strip()
        (self.project / "upstream.ref").write_text(self.ref + "\n")

        result = gitfile(self.project, "sync")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("conflict", result.stdout)
        self.assertTrue((self.project / "mod/src/main/java/demo/Foo.java.rej").is_file())

    def test_our_deletion_becomes_obsolete_when_upstream_deletes_too(self):
        self.ok(gitfile(self.project, "sync"))
        (self.project / "mod/src/main/java/demo/Bar.java").unlink()
        self.ok(gitfile(self.project, "update"))
        self.assertTrue(self.patch("mod/src/main/java/demo/Bar.java").exists())

        (self.upstream / "mod/src/main/java/demo/Bar.java").unlink()
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "delete Bar upstream")
        self.ref = git(self.upstream, "rev-parse", "HEAD").strip()
        (self.project / "upstream.ref").write_text(self.ref + "\n")

        out = self.ok(gitfile(self.project, "sync"))
        self.assertIn("already-deleted", out)
        self.assertFalse(self.patch("mod/src/main/java/demo/Bar.java").exists())

    def test_new_files_and_deletions_round_trip(self):
        self.ok(gitfile(self.project, "sync"))
        (self.project / "mod/src/main/java/demo/New.java").write_text("package demo;\nclass New {}\n")
        (self.project / "mod/src/main/java/demo/Bar.java").unlink()
        self.ok(gitfile(self.project, "update"))

        self.assertIn("+package demo;", self.patch("mod/src/main/java/demo/New.java").read_text())
        del_patch = self.patch("mod/src/main/java/demo/Bar.java").read_text()
        self.assertIn("+ /dev/null", del_patch)

        # A fresh sync replays both.
        self.ok(gitfile(self.project, "sync"))
        self.assertTrue((self.project / "mod/src/main/java/demo/New.java").is_file())
        self.assertFalse((self.project / "mod/src/main/java/demo/Bar.java").exists())

    def test_custom_materialize_hook_receives_env(self):
        hook = self.project / "hook.sh"
        hook.write_text(
            '#!/bin/sh\n'
            'set -e\n'
            'rm -rf "$GITFILE_DEST/mod/src"\n'
            'mkdir -p "$GITFILE_DEST/mod/src"\n'
            'cp -a "$GITFILE_UPSTREAM/mod/src/." "$GITFILE_DEST/mod/src/"\n'
            'echo "$GITFILE_MODE" >> "$GITFILE_DEST/modes.txt"\n'
        )
        cfg = json.loads((self.project / "gitfile.json").read_text())
        cfg.pop("modules")
        cfg["paths"] = ["mod/src"]
        cfg["materialize"] = "sh hook.sh"
        (self.project / "gitfile.json").write_text(json.dumps(cfg, indent=2))

        self.ok(gitfile(self.project, "sync"))
        self.assertEqual(self.foo().read_text(), FOO)
        self.assertIn("work", (self.project / "modes.txt").read_text())

    def test_single_module_project_with_root_src(self):
        up = self.tmp / "single-upstream"
        proj = self.tmp / "single-project"
        up.mkdir()
        git(up, "init", "-q")
        git(up, "config", "user.email", "test@example.com")
        git(up, "config", "user.name", "Test")
        (up / "src/main/java/demo").mkdir(parents=True)
        (up / "src/main/java/demo/Foo.java").write_text(FOO)
        git(up, "add", "-A")
        git(up, "commit", "-qm", "initial")
        ref = git(up, "rev-parse", "HEAD").strip()

        proj.mkdir()
        (proj / "upstream.ref").write_text(ref + "\n")
        (proj / "gitfile.json").write_text(json.dumps({
            "upstream": str(up),
            "refFile": "upstream.ref",
            "sources": [["src", "src"]],
        }, indent=2))

        self.ok(gitfile(proj, "sync"))
        self.assertEqual((proj / "src/main/java/demo/Foo.java").read_text(), FOO)

        (proj / "src/main/java/demo/Foo.java").write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(proj, "update"))
        patch = proj / "patches/src/main/java/demo/Foo.java.patch"
        self.assertTrue(patch.is_file())
        self.assertIn("-- a/src/main/java/demo/Foo.java", patch.read_text())

        result = gitfile(proj, "export", "-o", "fork.patch")
        self.ok(result)
        self.assertIn("verified against upstream", result.stdout)

    def test_root_file_and_src_are_patched_together(self):
        up = self.tmp / "root-upstream"
        proj = self.tmp / "root-project"
        up.mkdir()
        git(up, "init", "-q")
        git(up, "config", "user.email", "test@example.com")
        git(up, "config", "user.name", "Test")
        (up / "src/main/java/demo").mkdir(parents=True)
        (up / "src/main/java/demo/Foo.java").write_text(FOO)
        (up / "pom.xml").write_text(POM)
        git(up, "add", "-A")
        git(up, "commit", "-qm", "initial")
        ref = git(up, "rev-parse", "HEAD").strip()

        proj.mkdir()
        (proj / "upstream.ref").write_text(ref + "\n")
        (proj / "gitfile.json").write_text(json.dumps({
            "upstream": str(up),
            "refFile": "upstream.ref",
            "sources": [["src", "src"], ["pom.xml", "pom.xml"]],
        }, indent=2))

        self.ok(gitfile(proj, "sync"))
        self.assertEqual((proj / "pom.xml").read_text(), POM)

        (proj / "pom.xml").write_text(POM.replace("<version>1</version>", "<version>2</version>"))
        (proj / "src/main/java/demo/Foo.java").write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(proj, "update"))

        pom_patch = proj / "patches/pom.xml.patch"
        self.assertTrue(pom_patch.is_file())
        self.assertIn("--- a/pom.xml", pom_patch.read_text())
        self.assertIn("+  <version>2</version>", pom_patch.read_text())
        self.assertTrue((proj / "patches/src/main/java/demo/Foo.java.patch").is_file())

        result = gitfile(proj, "export", "-o", "fork.patch")
        self.ok(result)
        self.assertIn("verified against upstream", result.stdout)
        self.assertIn("--- a/pom.xml", (proj / "fork.patch").read_text())

    def test_export_combined_patch_applies_to_upstream(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        (self.project / "mod/src/main/java/demo/Bar.java").unlink()
        self.ok(gitfile(self.project, "update"))

        result = gitfile(self.project, "export", "-o", "combined.patch")
        self.ok(result)
        self.assertIn("verified against upstream", result.stdout)
        combined = (self.project / "combined.patch").read_text()
        self.assertIn("--- a/mod/src/main/java/demo/Foo.java", combined)
        self.assertIn("+++ /dev/null", combined)  # the Bar deletion is included

        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "-q", str(self.upstream), str(checkout))
        git(checkout, "apply", "-p1", str(self.project / "combined.patch"))
        self.assertIn("return 42;", (checkout / "mod/src/main/java/demo/Foo.java").read_text())
        self.assertFalse((checkout / "mod/src/main/java/demo/Bar.java").exists())

    def test_export_rewrite_targets_a_different_upstream_layout(self):
        self.ok(gitfile(self.project, "sync"))
        self.foo().write_text(FOO.replace("return 1;", "return 42;"))
        self.ok(gitfile(self.project, "update"))

        cfg = json.loads((self.project / "gitfile.json").read_text())
        cfg["exportRewrite"] = [["mod/src/", "src/"]]
        (self.project / "gitfile.json").write_text(json.dumps(cfg, indent=2))

        result = gitfile(self.project, "export", "-o", "rewritten.patch", "--no-check")
        self.ok(result)
        combined = (self.project / "rewritten.patch").read_text()
        self.assertIn("--- a/src/main/java/demo/Foo.java", combined)
        self.assertNotIn("mod/src/", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
