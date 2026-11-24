#!/usr/bin/env python3
"""gitfile - one patch per file for projects that track an upstream commit.

Patches live under a patch root, mirroring the working tree:

    src/main/java/com/example/Foo.java
    patches/src/main/java/com/example/Foo.java.patch

Each patch is a plain unified diff that touches exactly one file. There is no
series and no apply order, so two changes to the same file are the same patch
and can never conflict with each other.

Commands:
    sync    fetch upstream, build a pristine baseline + working tree, apply patches
    update  regenerate patches from baseline vs working tree
    revert  restore files from the baseline and drop their patches
    apply   apply existing patches (no upstream fetch)
    status  list patches and their applied state
    export  write all per-file patches as one combined patch

When `sync` detects that the upstream ref changed, it keeps the previous
pristine baseline, three-way merges conflicting patches against it, and marks
true conflicts in the working files. Resolve those, then run `update`.
"""

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CONFIG = "gitfile.json"
MARKER = ".gitfile-applied"
CONFLICT_STARTS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")
HUNK_RE = re.compile(r"^@@ ", re.MULTILINE)


class GitfileError(Exception):
    pass


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------

def run(cmd, cwd=None, env=None, check=False, shell=False, capture=True):
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        shell=shell,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitfileError(
            "command failed (%d): %s\n%s"
            % (proc.returncode, cmd if shell else " ".join(map(str, cmd)), detail)
        )
    return proc


def git(*args, cwd=None, check=False):
    return run(["git", *map(str, args)], cwd=cwd, check=check)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_config(path, root_override=None):
    cfg_path = Path(path)
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    cfg_path = cfg_path.resolve()
    if not cfg_path.is_file():
        raise GitfileError("config not found: %s" % cfg_path)
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as exc:
        raise GitfileError("invalid JSON in %s: %s" % (cfg_path, exc))
    if not isinstance(cfg, dict):
        raise GitfileError("%s must contain a JSON object" % cfg_path)
    root = Path(root_override).resolve() if root_override else cfg_path.parent
    return cfg, root


def state_paths(cfg, root):
    patches = root / cfg.get("patches", "patches")
    baseline = root / cfg.get("baselineDir", ".gitfile/baseline")
    upstream = root / cfg.get("upstreamDir", ".gitfile/upstream")
    for path in (patches, baseline, upstream):
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            raise GitfileError("state directories must live inside the project root: %s" % path)
    return patches, baseline, upstream


def copy_specs(cfg):
    """Return [(from, to)] pairs for the default materializer.

    `sources` is the general form and handles single-module projects where
    upstream keeps its code at the root:

        "sources": [["src", "src"], ["pom.xml", "pom.xml"]]

    Entries may be directories or single files, so build files that upstream
    owns can be patched alongside the sources.
    """
    if cfg.get("sources") is not None:
        specs = []
        for item in cfg["sources"]:
            if isinstance(item, dict):
                if "from" not in item or "to" not in item:
                    raise GitfileError("each 'sources' entry needs 'from' and 'to'")
                specs.append((item["from"], item["to"]))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                specs.append((item[0], item[1]))
            elif isinstance(item, str):
                specs.append((item, item))
            else:
                raise GitfileError("invalid 'sources' entry: %r" % (item,))
        return specs
    if cfg.get("source"):
        return [(cfg["source"], cfg["source"])]
    modules = cfg.get("modules")
    if modules:
        return [
            (str(m).rstrip("/") + "/src", str(m).rstrip("/") + "/src")
            for m in modules
        ]
    return []


def source_paths(cfg):
    paths = cfg.get("paths")
    if not paths:
        paths = [to for _, to in copy_specs(cfg)]
    if not paths:
        raise GitfileError("config needs 'paths', 'sources', 'source', or 'modules'")
    result = []
    for item in paths:
        p = Path(item)
        if p.is_absolute() or ".." in p.parts:
            raise GitfileError("path must be relative and stay inside root: %s" % item)
        result.append(p)
    return result


def resolve_ref(cfg, root):
    if cfg.get("refFile"):
        ref_file = root / cfg["refFile"]
        if not ref_file.is_file():
            raise GitfileError("refFile not found: %s" % ref_file)
        ref = ref_file.read_text().strip()
    elif cfg.get("ref"):
        value = str(cfg["ref"])
        candidate = root / value
        ref = candidate.read_text().strip() if candidate.is_file() else value
    else:
        raise GitfileError("config needs 'ref' or 'refFile'")
    if not ref:
        raise GitfileError("upstream ref is empty")
    return ref


def list_patches(patches_dir):
    if not patches_dir.is_dir():
        return []
    return sorted(p for p in patches_dir.rglob("*.patch") if p.is_file())


def rel_from_patch(patch, patches_dir):
    rel = patch.relative_to(patches_dir).as_posix()
    return rel[:-len(".patch")]


def patch_kind(text):
    add = re.search(r"^--- /dev/null$", text, re.MULTILINE) is not None
    delete = re.search(r"^\+\+\+ /dev/null$", text, re.MULTILINE) is not None
    if add and delete:
        return "empty"
    if add:
        return "add"
    if delete:
        return "delete"
    return "modify"


def validate_patch(patch):
    text = patch.read_text()
    ndiff = len(re.findall(r"^--- ", text, re.MULTILINE))
    nplus = len(re.findall(r"^\+\+\+ ", text, re.MULTILINE))
    if ndiff != 1 or nplus != 1:
        raise GitfileError(
            "%s must target exactly one file (found %d '---', %d '+++')"
            % (patch, ndiff, nplus)
        )
    if not HUNK_RE.search(text):
        raise GitfileError("%s has no hunks" % patch)
    return text


# --------------------------------------------------------------------------
# upstream + materialization
# --------------------------------------------------------------------------

def ensure_upstream(cfg, root, upstream_dir, ref):
    url = cfg.get("upstream")
    if not url:
        raise GitfileError("config needs an 'upstream' URL for sync")
    if upstream_dir.exists():
        have = git("-C", upstream_dir, "rev-parse", "HEAD", cwd=root)
        want = git("-C", upstream_dir, "rev-parse", "--verify", ref + "^{commit}", cwd=root)
        if (
            have.returncode == 0
            and want.returncode == 0
            and have.stdout.strip() == want.stdout.strip()
        ):
            return
        shutil.rmtree(upstream_dir)
    upstream_dir.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--no-checkout", url, upstream_dir, cwd=root, check=True)
    checkout = git("-C", upstream_dir, "checkout", ref, cwd=root)
    if checkout.returncode != 0:
        raise GitfileError(
            "could not check out %s in %s:\n%s"
            % (ref, upstream_dir, (checkout.stderr or "").strip())
        )


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def copy_tree(src, dst):
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def materialize(cfg, root, upstream_dir, dest, mode):
    """Populate <dest> from the upstream checkout.

    A project can provide a `materialize` shell command for a mapping the
    generic copy cannot express (package splits, generated files, ...). It
    receives GITFILE_UPSTREAM, GITFILE_DEST, GITFILE_ROOT and GITFILE_MODE.
    Otherwise the default copies every `sources` entry (directories or single
    files), deletes `delete` globs, then overlays the optional `overlays`
    directory.
    """
    hook = cfg.get("materialize")
    if hook:
        env = os.environ.copy()
        env.update(
            GITFILE_UPSTREAM=str(upstream_dir),
            GITFILE_DEST=str(dest),
            GITFILE_ROOT=str(root),
            GITFILE_MODE=mode,
        )
        proc = run(hook, cwd=root, env=env, shell=True, check=False, capture=False)
        if proc.returncode != 0:
            raise GitfileError("materialize command failed (%d): %s" % (proc.returncode, hook))
        return

    specs = copy_specs(cfg)
    if not specs:
        raise GitfileError("config needs 'materialize', or 'sources'/'source'/'modules' for the default copier")
    for from_rel, to_rel in specs:
        src = upstream_dir / from_rel
        if not src.exists():
            raise GitfileError("upstream has no such source: %s" % from_rel)
        out = dest / to_rel
        remove_path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, out)
        else:
            shutil.copy2(src, out)

    for pattern in cfg.get("delete", []):
        for match in dest.glob(pattern):
            if match.is_dir():
                shutil.rmtree(match)
            elif match.exists():
                match.unlink()

    overlays = cfg.get("overlays")
    if overlays:
        overlay_dir = root / overlays
        if overlay_dir.is_dir():
            copy_tree(overlay_dir, dest)


# --------------------------------------------------------------------------
# diff generation
# --------------------------------------------------------------------------

def _normalize_diff(raw, rel, kind):
    lines = raw.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("--- ")), None)
    if start is None or len(lines) < start + 2 or not lines[start + 1].startswith("+++ "):
        raise GitfileError("could not generate a unified diff for %s" % rel)
    body = lines[start:]
    if kind == "add":
        body[0] = "--- /dev/null\n"
        body[1] = "+++ b/%s\n" % rel
    elif kind == "delete":
        body[0] = "--- a/%s\n" % rel
        body[1] = "+++ /dev/null\n"
    else:
        body[0] = "--- a/%s\n" % rel
        body[1] = "+++ b/%s\n" % rel
    return "".join(body)


def _diff_two(root, first, second):
    proc = git(
        "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-renames", "--unified=3",
        "--", first, second, cwd=root,
    )
    if proc.returncode not in (0, 1):
        raise GitfileError("git diff failed for %s / %s:\n%s" % (first, second, proc.stderr))
    return proc.stdout


def make_patch(root, baseline, work, rel):
    base_file = baseline / rel
    work_file = work / rel
    if base_file.is_file() and work_file.is_file():
        if filecmp.cmp(str(base_file), str(work_file), shallow=False):
            return None
        raw = _diff_two(root, base_file.relative_to(root).as_posix(), work_file.relative_to(root).as_posix())
        return _normalize_diff(raw, rel, "modify")
    if work_file.is_file():
        with tempfile.NamedTemporaryFile() as empty:
            raw = _diff_two(root, empty.name, work_file.relative_to(root).as_posix())
        return _normalize_diff(raw, rel, "add")
    if base_file.is_file():
        with tempfile.NamedTemporaryFile() as empty:
            raw = _diff_two(root, base_file.relative_to(root).as_posix(), empty.name)
        return _normalize_diff(raw, rel, "delete")
    return None


def prune_empty_dirs(root):
    if not root.is_dir():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def regen(cfg, root, patches_dir, baseline):
    paths = source_paths(cfg)
    kept = set()
    for source in paths:
        base_tree = baseline / source
        work_tree = root / source
        rels = set()
        for tree in (base_tree, work_tree):
            if tree.is_file():
                rels.add(source.as_posix())
            elif tree.is_dir():
                for item in tree.rglob("*"):
                    if item.is_file():
                        rels.add((source / item.relative_to(tree)).as_posix())
        for rel in sorted(rels):
            text = make_patch(root, baseline, root, rel)
            if text is None:
                continue
            target = patches_dir / (rel + ".patch")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            kept.add(target.resolve())

    for patch in list_patches(patches_dir):
        if patch.resolve() not in kept:
            patch.unlink()
    prune_empty_dirs(patches_dir)
    return len(kept)


# --------------------------------------------------------------------------
# conflicts
# --------------------------------------------------------------------------

def has_conflicts(cfg, root):
    found = set()
    for source in source_paths(cfg):
        node = root / source
        if node.is_file():
            candidates = [node]
        elif node.is_dir():
            candidates = [p for p in node.rglob("*") if p.is_file()]
        else:
            continue
        for item in candidates:
            if item.suffix in (".rej", ".orig"):
                found.add(item.relative_to(root).as_posix())
                continue
            try:
                text = item.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for line in text.splitlines():
                if line.startswith(CONFLICT_STARTS):
                    found.add(item.relative_to(root).as_posix())
                    break
    return sorted(found)


def ensure_rej(target, patch_text, reason):
    rej = Path(str(target) + ".rej")
    if rej.exists():
        return
    rej.parent.mkdir(parents=True, exist_ok=True)
    rej.write_text("# gitfile: this patch did not apply cleanly (%s)\n\n" % reason + patch_text)


def try_three_way(patch, rel, old_baseline, baseline, target, root):
    """Merge an old-baseline patch onto the new baseline.

    Returns 0 for a clean merge, a positive conflict count when markers were
    written, or None when a three-way merge is not possible.
    """
    old = old_baseline / rel
    new = baseline / rel
    if not old.is_file() or not new.is_file():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        patched_old = tmp_root / rel
        patched_old.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, patched_old)
        applied = git("apply", "-p1", "--whitespace=nowarn", patch, cwd=tmp_root)
        if applied.returncode != 0:
            return None
        merged = git(
            "merge-file", "-p", "--diff3", patched_old, old, new, cwd=root,
        )
        if merged.returncode < 0 or "error:" in (merged.stderr or "").lower():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged.stdout)
        return merged.returncode


def apply_patches(cfg, root, patches_dir, baseline=None, old_baseline=None):
    """Apply every patch. Returns a list of (rel, state, detail)."""
    results = []
    for patch in list_patches(patches_dir):
        text = validate_patch(patch)
        rel = rel_from_patch(patch, patches_dir)
        target = root / rel
        proc = git("apply", "-p1", "--whitespace=nowarn", patch, cwd=root)
        if proc.returncode == 0:
            results.append((rel, "applied", ""))
            continue

        kind = patch_kind(text)
        # Upstream already removed this file: the deletion patch is obsolete.
        if kind == "delete" and baseline is not None and not (baseline / rel).exists():
            results.append((rel, "already-deleted", ""))
            continue

        merged = None
        if old_baseline is not None and kind == "modify":
            merged = try_three_way(patch, rel, old_baseline, baseline, target, root)
        if merged == 0:
            results.append((rel, "rebased", ""))
            continue
        if merged is not None and merged > 0:
            results.append((rel, "conflict", "%d conflicted region(s)" % merged))
            continue

        git("apply", "-p1", "--reject", "--whitespace=nowarn", patch, cwd=root)
        ensure_rej(target, text, "patch did not apply")
        results.append((rel, "conflict", "rejected hunks written to %s.rej" % rel))
    return results


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_marker(baseline):
    marker = baseline / MARKER
    return marker.read_text().strip() if marker.is_file() else None


def write_marker(baseline, sha):
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / MARKER).write_text(sha + "\n")


def cmd_sync(cfg, root, args):
    patches_dir, baseline, upstream_dir = state_paths(cfg, root)
    ref = resolve_ref(cfg, root)
    ensure_upstream(cfg, root, upstream_dir, ref)
    new_sha = git("-C", upstream_dir, "rev-parse", "HEAD", cwd=root, check=True).stdout.strip()
    old_sha = read_marker(baseline)
    changed = old_sha is not None and old_sha != new_sha

    prev = baseline.parent / (baseline.name + ".prev")
    if prev.exists():
        shutil.rmtree(prev)
    old_baseline = None
    try:
        if changed and baseline.is_dir():
            prev.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(baseline), str(prev))
            old_baseline = prev
            print("upstream moved %s -> %s; previous baseline kept for three-way merge" % (old_sha[:12], new_sha[:12]))

        materialize(cfg, root, upstream_dir, baseline, "baseline")
        materialize(cfg, root, upstream_dir, root, "work")
        results = apply_patches(cfg, root, patches_dir, baseline, old_baseline)
        write_marker(baseline, new_sha)
    finally:
        if prev.exists():
            shutil.rmtree(prev)

    conflicts = [r for r in results if r[1] == "conflict"]
    rebased = [r for r in results if r[1] in ("rebased", "already-deleted")]
    failed = [r for r in results if r[1] == "conflict"]

    print("synced %s: %d patch(es), %d applied, %d rebased/obsolete, %d conflict(s)"
          % (new_sha[:12], len(results), len(results) - len(rebased) - len(failed), len(rebased), len(failed)))
    for rel, state, detail in results:
        if state in ("rebased", "already-deleted", "conflict"):
            suffix = (" (%s)" % detail) if detail else ""
            print("  %-15s %s%s" % (state, rel, suffix))

    if conflicts:
        print("")
        print("Resolve the conflicts above (merge markers and/or .rej files),")
        print("then run: gitfile update")
        return 1

    if rebased and not args.no_regen:
        count = regen(cfg, root, patches_dir, baseline)
        print("rebased %d patch(es) against the new baseline; %d patch(es) now recorded"
              % (len(rebased), count))
    return 0


def cmd_update(cfg, root, args):
    patches_dir, baseline, _ = state_paths(cfg, root)
    if not (baseline / MARKER).is_file():
        raise GitfileError("baseline marker missing; run `gitfile sync` first")
    conflicts = has_conflicts(cfg, root)
    if conflicts and not args.force:
        lines = "\n".join("  " + c for c in conflicts[:20])
        raise GitfileError(
            "unresolved conflicts:\n%s\n"
            "Resolve them and remove the .rej files, or pass --force to record anyway." % lines
        )
    count = regen(cfg, root, patches_dir, baseline)
    print("regenerated %d per-file patch(es) under %s" % (count, patches_dir.relative_to(root)))
    return 0


def cmd_revert(cfg, root, args):
    """Restore files from the baseline, dropping their per-file patches."""
    patches_dir, baseline, _ = state_paths(cfg, root)
    if not (baseline / MARKER).is_file():
        raise GitfileError("baseline marker missing; run `gitfile sync` first")

    allowed = source_paths(cfg)
    rels = set()
    for raw in args.paths:
        rel = Path(raw)
        if rel.is_absolute() or ".." in rel.parts:
            raise GitfileError("path must be relative and stay inside root: %s" % raw)
        rel_str = rel.as_posix().rstrip("/")
        if not any(
            rel_str == p.as_posix() or rel_str.startswith(p.as_posix() + "/")
            for p in allowed
        ):
            raise GitfileError("path is not under a configured source path: %s" % raw)
        for tree in (root, baseline):
            node = tree / rel_str
            if node.is_file():
                rels.add(rel_str)
            elif node.is_dir():
                for item in node.rglob("*"):
                    if item.is_file():
                        rels.add((Path(rel_str) / item.relative_to(node)).as_posix())

    if not rels:
        raise GitfileError("nothing to revert under: %s" % ", ".join(args.paths))

    restored = deleted = dropped = 0
    for rel in sorted(rels):
        work_file = root / rel
        base_file = baseline / rel
        if base_file.is_file():
            unchanged = work_file.is_file() and filecmp.cmp(str(base_file), str(work_file), shallow=False)
            if not unchanged:
                work_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base_file, work_file)
                restored += 1
        elif work_file.exists():
            remove_path(work_file)
            deleted += 1
        for suffix in (".rej", ".orig"):
            artifact = Path(str(work_file) + suffix)
            if artifact.exists():
                artifact.unlink()
        patch = patches_dir / (rel + ".patch")
        if patch.exists():
            patch.unlink()
            dropped += 1

    prune_empty_dirs(patches_dir)
    print(
        "reverted %d file(s) (%d restored, %d deleted); removed %d patch(es)"
        % (len(rels), restored, deleted, dropped)
    )
    return 0


def cmd_apply(cfg, root, args):
    patches_dir, baseline, _ = state_paths(cfg, root)
    if args.check:
        failures = []
        for patch in list_patches(patches_dir):
            validate_patch(patch)
            proc = git("apply", "--check", "-p1", "--whitespace=nowarn", patch, cwd=root)
            if proc.returncode != 0:
                failures.append((rel_from_patch(patch, patches_dir), (proc.stderr or "").strip()))
        if failures:
            for rel, detail in failures:
                print("FAIL %s\n%s" % (rel, detail))
            return 1
        print("all %d patch(es) apply cleanly" % len(list_patches(patches_dir)))
        return 0
    results = apply_patches(cfg, root, patches_dir, baseline if baseline.is_dir() else None)
    conflicts = [r for r in results if r[1] == "conflict"]
    print("applied %d patch(es), %d conflict(s)" % (len(results), len(conflicts)))
    for rel, state, detail in results:
        if state == "conflict":
            print("  conflict %s" % rel)
    return 1 if conflicts else 0


def rewrite_patch_paths(text, rules):
    """Rewrite the a/ and b/ sides of a patch so it targets upstream paths."""
    if not rules:
        return text

    def remap(path):
        for rule in rules:
            if len(rule) != 2:
                raise GitfileError("exportRewrite entries must be [from, to] pairs")
            src, dst = rule
            if path.startswith(src):
                return dst + path[len(src):]
        return path

    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("--- ") and not line.startswith("--- /dev/null"):
            path = line[4:].rstrip("\n")
            if path.startswith("a/"):
                path = path[2:]
            out.append("--- a/%s\n" % remap(path))
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].rstrip("\n")
            if path.startswith("b/"):
                path = path[2:]
            out.append("+++ b/%s\n" % remap(path))
        else:
            out.append(line)
    return "".join(out)


def cmd_export(cfg, root, args):
    patches_dir, _, upstream_dir = state_paths(cfg, root)
    patches = list_patches(patches_dir)
    if not patches:
        raise GitfileError("no patches to export under %s" % patches_dir)

    rules = cfg.get("exportRewrite", [])
    chunks = []
    for patch in patches:
        text = rewrite_patch_paths(validate_patch(patch), rules)
        if not text.endswith("\n"):
            text += "\n"
        chunks.append(text)
    combined = "".join(chunks)

    if args.stdout:
        sys.stdout.write(combined)
        return 0

    out = Path(args.output) if args.output else root / cfg.get("exportFile", "gitfile.patch")
    if not out.is_absolute():
        out = root / out
    try:
        out.relative_to(patches_dir)
        raise GitfileError("export file must not live inside the per-file patch root: %s" % out)
    except ValueError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(combined)
    print("exported %d per-file patch(es) to %s" % (len(patches), out))

    if not args.no_check and upstream_dir.is_dir():
        proc = git("apply", "--check", "-p1", "--whitespace=nowarn", out, cwd=upstream_dir)
        if proc.returncode != 0:
            raise GitfileError(
                "combined patch does not apply to the upstream checkout:\n%s\n"
                "Set 'exportRewrite' in the config if fork paths differ from upstream."
                % (proc.stderr or "").strip()
            )
        print("verified against upstream checkout at %s" % upstream_dir.relative_to(root))
    return 0


def cmd_status(cfg, root, args):
    patches_dir, baseline, upstream_dir = state_paths(cfg, root)
    patches = list_patches(patches_dir)
    sha = read_marker(baseline)
    print("root:     %s" % root)
    print("patches:  %d in %s" % (len(patches), patches_dir.relative_to(root)))
    print("baseline: %s (ref %s)" % (baseline.relative_to(root), sha or "missing"))
    for patch in patches:
        rel = rel_from_patch(patch, patches_dir)
        proc = git("apply", "--reverse", "--check", "-p1", patch, cwd=root)
        state = "applied" if proc.returncode == 0 else "not applied"
        print("  %-11s %s" % (state, rel))
    conflicts = has_conflicts(cfg, root)
    if conflicts:
        print("conflicts:")
        for item in conflicts:
            print("  " + item)
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="gitfile",
        description="One patch per file for projects that track an upstream commit.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: %s)" % DEFAULT_CONFIG)
    parser.add_argument("--root", help="project root (default: config file directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="fetch upstream, rebuild baseline + work tree, apply patches")
    sync.add_argument("--no-regen", action="store_true", help="do not rewrite patches after a clean rebase")
    sync.set_defaults(func=cmd_sync)

    update = sub.add_parser("update", help="regenerate per-file patches from the working tree")
    update.add_argument("--force", action="store_true", help="record even if conflict markers/.rej files remain")
    update.set_defaults(func=cmd_update)

    revert = sub.add_parser("revert", help="restore files from the baseline and drop their patches")
    revert.add_argument(
        "paths", nargs="+",
        metavar="PATH",
        help="repo-relative file or directory under a configured source path",
    )
    revert.set_defaults(func=cmd_revert)

    apply_cmd = sub.add_parser("apply", help="apply existing patches without fetching upstream")
    apply_cmd.add_argument("--check", action="store_true", help="only check that patches apply")
    apply_cmd.set_defaults(func=cmd_apply)

    status = sub.add_parser("status", help="list patches and their applied state")
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export", help="write all per-file patches as one combined patch")
    export.add_argument("-o", "--output", help="output file (default: config 'exportFile' or gitfile.patch)")
    export.add_argument("--stdout", action="store_true", help="write the combined patch to stdout")
    export.add_argument("--no-check", action="store_true", help="skip verifying against the upstream checkout")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg, root = load_config(args.config, args.root)
        return args.func(cfg, root, args)
    except GitfileError as exc:
        print("gitfile: error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
