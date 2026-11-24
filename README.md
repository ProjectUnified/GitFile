# GitFile

One patch per file, for projects that track an upstream commit.

`gitfile` replaces a sequential patch stack (Quilt, `patches/series`, apply
order, `.pc/` state) with a flat tree of per-file patches:

```
src/main/java/com/example/Foo.java
patches/src/main/java/com/example/Foo.java.patch
```

Each patch is a plain unified diff that touches exactly one file and is applied
with `git apply -p1`. There is no order, so two changes to the same file are the
same patch and can never conflict with each other. The model is the same one
Paper uses for Minecraft sources.

No Python dependencies; `git` is the only requirement.

## Install

```sh
pipx install /path/to/GitFile     # provides the `gitfile` command
# or run it straight from the checkout:
python3 /path/to/GitFile/gitfile.py --help
./gitfile --help
```

## Config

`gitfile.json` lives at the project root (override with `-c/--config`).

| Key | Default | Meaning |
|---|---|---|
| `upstream` | — | Git URL cloned for `sync` |
| `ref` / `refFile` | — | Pinned commit/ref, or a file containing it (`upstream.ref`) |
| `patches` | `patches` | Per-file patch root |
| `baselineDir` | `.gitfile/baseline` | Pristine split, kept for regeneration and merges |
| `upstreamDir` | `.gitfile/upstream` | Upstream checkout |
| `paths` | from `sources`/`source`/`modules` | Trees to scan for patches (must be repo-relative) |
| `sources` | — | Default materializer mappings: `[[from, to], ...]`; entries may be directories or files, e.g. `[["src", "src"], ["pom.xml", "pom.xml"]]` |
| `source` | — | Shorthand for one identity mapping (`"src"` -> `src` to `src`) |
| `modules` | — | Shorthand for `<module>/src` -> `<module>/src` per module |
| `delete` | `[]` | Globs deleted after the default copy (e.g. `module-info.java`) |
| `overlays` | — | Directory of fork-owned files copied over every materialization |
| `materialize` | — | Custom shell command instead of `modules` (package splits, generated files) |
| `exportFile` | `gitfile.patch` | Default `export` output |
| `exportRewrite` | `[]` | `[[fromPrefix, toPrefix], ...]` applied to exported paths |

The `materialize` hook runs with `cwd` at the project root and these variables:

```
GITFILE_UPSTREAM  path to the pinned upstream checkout
GITFILE_DEST      tree to populate (baseline dir, or the project root for work)
GITFILE_ROOT      project root
GITFILE_MODE      "baseline" or "work"
```

## Commands

```sh
gitfile sync      # clone upstream, build baseline + work tree, apply patches
gitfile update    # regenerate patches from work tree vs baseline
gitfile apply     # apply patches without fetching upstream (--check to validate)
gitfile status    # list patches, applied state, and unresolved conflicts
gitfile export    # write all per-file patches as one combined patch
```

Typical loop:

```sh
gitfile sync
# edit files under the module source trees
gitfile update
```

`sync` is also the reset hatch: it discards unrecorded edits in the generated
trees and replays the patches.

Add the state directories to `.gitignore`:

```gitignore
.gitfile/
# or the configured .vanilla/ and .upstream/
```

## Upstream ref changes

When `upstream.ref` points at a new commit, `sync` keeps the previous baseline
and rebases each conflicting patch with a three-way merge:

- **clean merge** — both upstream's change and the patch's change are kept; the
  patch is rewritten against the new baseline automatically.
- **real conflict** — the working file gets merge markers (`<<<<<<<`, `=======`,
  `>>>>>>>`) or a `.rej` file. `sync` exits non-zero and lists the files.
  `update` refuses to record anything until the markers and `.rej` files are
  gone.
- **upstream deleted the file we only deleted** — the patch is dropped as
  obsolete (`already-deleted`).
- **upstream deleted a file we modified** — a conflict; resolve it by hand.

Resolve conflicts, then:

```sh
gitfile update
```

## Export for an upstream PR

`export` concatenates every per-file patch into a single unified diff that can
be applied to a fresh upstream checkout:

```sh
gitfile export                      # -> gitfile.patch
gitfile export -o my-fork.patch
gitfile export --stdout > my-fork.patch
```

By default it verifies the combined patch with `git apply --check` against the
pinned upstream checkout and fails if the paths do not line up.

The paths in a fork do not always match upstream. Two common shapes:

- **Modules align** (FastStats): `core/src/...` upstream and in the fork. No
  configuration needed.
- **Fork reorganized upstream** (cron-utils: one upstream module split into
  four): use `exportRewrite` to map fork paths back to upstream paths:

```json
"exportRewrite": [
  ["cron-utils-core/src/", "src/"],
  ["cron-utils-descriptor/src/", "src/"],
  ["cron-utils-converter/src/", "src/"],
  ["cron-utils-validator/src/", "src/"]
]
```

Then:

```sh
gitfile export --no-check      # if there is no upstream checkout to verify against
git -C /path/to/upstream apply -p1 /path/to/fork.patch
```

## Single-module projects and build files

When upstream keeps its code at the root (`src/main/java/...`), use an
identity mapping instead of `modules`. Entries can be single files as well, so
files the upstream owns (`pom.xml`, `build.gradle`, ...) can be patched
alongside the sources:

```json
{
  "upstream": "https://github.com/owner/project.git",
  "refFile": "upstream.ref",
  "sources": [["src", "src"], ["pom.xml", "pom.xml"]]
}
```

`paths` is derived from the `to` side, so this is enough to sync, update, and
export. The resulting patches are `patches/src/...` and `patches/pom.xml.patch`.
`"source": "src"` is shorthand for a single identity mapping; use `sources`
when a project copies several upstream trees.

## Tests

```sh
python3 tests/test_gitfile.py
```

The suite builds throwaway local upstream repos and covers sync, update,
rebase on an upstream change, conflict marking, deletion handling, new files,
and combined export.
