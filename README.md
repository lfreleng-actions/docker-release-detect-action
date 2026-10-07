<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐳 Docker Release Detect Action

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/docker-release-detect-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/docker-release-detect-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/docker-release-detect-action)
<!-- prettier-ignore-end -->

Decides whether a commit releases containers. It looks for the
container release file the commit adds or edits under `releases/`,
validates it, and outputs the release tag, the staged images to
promote, and any registry overrides.

## docker-release-detect-action

A release in the Linux Foundation's self-release convention is a merge
that adds a file such as `releases/1.2.3-container.yaml`:

```yaml
distribution_type: container
project: example
container_release_tag: 1.2.3
ref: 0123456789abcdef0123456789abcdef01234567
container_push_registry: nexus3.example.org:10003
containers:
  - name: policy-api
    version: 1.2.3-STAGING-20260101T000000Z
  - name: onap/policy-pap
    version: 4.0.1
```

The [docker-workflows] merge lane carried this detection as 146 lines
of inline shell in its `check-release` job ([docker-workflows#36]).
This action replaces that step one for one: the five outputs keep
their names, and the defaults reproduce the step, as the test suite
proves against the original body; see [Compatibility](#compatibility).

Beyond the lane, it fails with an error on a checkout too shallow to tell
what the commit changed, and validates a change before it merges,
as the Jenkins release verify jobs do.

The action reads files and git history alone. It contacts no
registry: checking that the staged images exist belongs to
[docker-promote-action], which performs the promotion.

## Usage Example

<!-- markdownlint-disable MD046 -->

### Merge: gate the promotion

```yaml
jobs:
  check-release:
    runs-on: ubuntu-latest
    outputs:
      has_release: ${{ steps.detect.outputs.has_release }}
      version: ${{ steps.detect.outputs.version }}
      containers_json: ${{ steps.detect.outputs.containers_json }}
      pull_registry: ${{ steps.detect.outputs.pull_registry }}
      push_registry: ${{ steps.detect.outputs.push_registry }}
    steps:
      # Depth 2: the merged commit and its parents.
      - uses: actions/checkout@<sha>  # vX.Y.Z
        with:
          fetch-depth: 2
          persist-credentials: false
      - id: detect
        uses: lfreleng-actions/docker-release-detect-action@<sha>  # vX.Y.Z
        with:
          snapshot_registry: nexus3.example.org:10001
          release_registry: nexus3.example.org:10002
```

### Verify: reject a malformed release file before it merges

On `pull_request`, actions/checkout checks out the merge commit GitHub
prepares, whose first parent is the base branch, so the default
comparison covers every commit of the pull request. A Gerrit change
works the same way, checked out with
`checkout-gerrit-change-action` and `fetch-depth: 2`.

```yaml
on: pull_request
jobs:
  verify-release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>  # vX.Y.Z
        with:
          fetch-depth: 2
          persist-credentials: false
      - uses: lfreleng-actions/docker-release-detect-action@<sha>  # vX.Y.Z
        with:
          snapshot_registry: nexus3.example.org:10001
          release_registry: nexus3.example.org:10002
```

When the checkout is the change's own head instead, name the base:
the action then compares `HEAD` with its merge base with that revision.

```yaml
- uses: actions/checkout@<sha>  # vX.Y.Z
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- uses: lfreleng-actions/docker-release-detect-action@<sha>  # vX.Y.Z
  with:
    base: ${{ github.event.pull_request.base.sha }}
```

<!-- markdownlint-enable MD046 -->

## Inputs

<!-- markdownlint-disable MD013 -->

| Name                | Required | Default   | Description                                                                           |
| ------------------- | -------- | --------- | ------------------------------------------------------------------------------------- |
| `snapshot_registry` | False    | `''`      | Registry to promote from; a `container_pull_registry` override must keep its host     |
| `release_registry`  | False    | `''`      | Registry to promote to; a `container_push_registry` override must keep its host       |
| `base`              | False    | `''`      | Compare `HEAD` with its merge base with this revision, not with its first parent      |
| `path`              | False    | `.`       | The repository's top level, holding `releases/`                                       |
| `summary`           | False    | `true`    | Write a detection report to the step summary                                          |
| `numeric_versions`  | False    | `literal` | Reading of unquoted number versions and tags: `literal`, `yaml` or `refuse` (below)   |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Name                | Description                                                                      |
| ------------------- | -------------------------------------------------------------------------------- |
| `has_release`       | `true` when the commit adds or edits a container release file, otherwise `false` |
| `version`           | The release file's `container_release_tag`                                       |
| `containers_json`   | Compact JSON list of `{name, version}`, versions as strings                      |
| `pull_registry`     | The `container_pull_registry` override, or empty                                 |
| `push_registry`     | The `container_push_registry` override, or empty                                 |
| `release_file`      | Path of the container release file, or empty                                     |
| `container_count`   | Number of containers to promote; `0` without a release                           |
| `distribution_type` | `container` with a release, otherwise empty                                      |

<!-- markdownlint-enable MD013 -->

The first five are the merge lane's outputs, empty apart from
`has_release=false` when the commit releases nothing. A caller falls
back to its own registries when an override is empty, as the lane's
promotion job does with
`needs.check-release.outputs.pull_registry || inputs.snapshot_registry`.
Any validation failure fails the step and publishes no outputs.

## Implementation Details

### What the commit changed

The action lists the files under `releases/`, at any depth, that the
commit touched: `git diff-tree -r -z` between HEAD's first parent,
read from HEAD's commit object, and `HEAD`; see
[`scripts/changes.py`](scripts/changes.py).

A merge commit compares with the branch it merged into, so
a pull request merge counts the files the pull request brought. The
lane ran `git diff-tree -m --first-parent HEAD`, but `diff-tree` takes
`--first-parent` as a history-walk option and ignores it, so `-m`
diffed the merge against every parent. A release file the target
branch already held differs from the merged branch, so any later merge
detected it again. The lane read the names without `-z`, which made
git C-quote a name holding a byte above 0x7f, a quote or a control
character; the quoted name matched no file, and the lane skipped the
release without a word. A name that is not valid UTF-8 fails the
step: outputs and the step summary are UTF-8, so the action could not
publish it.

Naming the parent explicitly also covers a shallow `HEAD` whose parent
a later fetch brought in: git keeps grafting such a commit parentless, and
`diff-tree HEAD` alone would list nothing.

A file the commit deleted no longer exists in the checkout, so the
action skips it: removing a release file releases nothing. A file
still in the commit but absent from the checkout, as a sparse checkout
leaves it, fails the step instead, where the lane took it for a
deletion and missed the release. The check reads the commit's trees
and never the file's blob, so it holds in a blob-filtered partial
clone too. With `base`
set, the comparison runs from the merge base of `base` and `HEAD`, so
commits that reached the base branch after the change forked do not
count against it.

### History depth

The comparison needs the commit's parent. `actions/checkout` fetches
one commit by default, and a shallow clone grafts its boundary commit
parentless, so `diff-tree` treats `HEAD` as a root commit and lists
nothing. The lane encoded `fetch-depth: 2` as a comment: at depth 1 it
found no release on any merge, with no error.

The action reads `HEAD`'s commit object, which still records its
parents. When the first parent is absent from the checkout, the step
fails with the fix: `fetch-depth: 2` or more. Neither
`git rev-parse --is-shallow-repository` nor git's list of shallow
boundaries can settle this, because a clone at depth 1 lists a genuine
root commit as a boundary too.

A commit with no parent at all, a repository's first, has nothing to
compare against. The action reports it with a notice and treats it as
no release, where the lane gave no sign at all. Promoting from it
would be a hazard: a repository imported as one squashed commit would
release whatever release file its history last held.

### Release file rules

The action reads every file under `releases/` that the commit adds or
changes, and keeps those whose `distribution_type` is `container`;
others, such as `maven`, belong to other pipelines and draw a notice.
No container release file means no release. More than one fails the
step, since the promotion would otherwise pick one arbitrarily.

<!-- markdownlint-disable MD013 -->

| Field                                                | Rule                                                                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| The file                                             | One YAML document, a regular file in `HEAD`, with no symlink at it or above it                                                                    |
| `container_release_tag`                              | Required; Docker's tag grammar, `^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$`                                                                             |
| `containers`                                         | Required non-empty list of `{name, version}` mappings; other keys dropped from the output                                                         |
| `containers[].name`                                  | Unique; each `/`-separated component matches Docker's `^[a-z0-9]+(?:(?:[._]\|__\|-+)[a-z0-9]+)*$`; with any registry path, at most 255 characters |
| `containers[].version`                               | A string or a number, output as a string per `numeric_versions`; the tag grammar                                                                  |
| `container_pull_registry`, `container_push_registry` | Optional; `host[:port]` with an optional path of lowercase components, on the host of the matching input                                          |

<!-- markdownlint-enable MD013 -->

### Numeric versions

A `container_release_tag` or `containers[].version` that YAML types
as a number, written unquoted (`version: 1.10`) or tagged `!!int` or
`!!float` (`!!float "2"`), has no single string form, so the
`numeric_versions` input picks one. Quoted strings without such a tag
(`"1.10"`) are always used as written, in every mode.

- **`literal`** (default): the text as written, so `1.10` stays
  `1.10` and `2.0` stays `2.0`. This is the lane's result on
  GitHub-hosted runners (yq 4.53.3 or later, jq 1.7), now independent
  of the runner's yq: releases before 4.53.3 print `1.10` in JSON as
  `1.1`, so the action takes each number's text from yq's `tag`
  operator and a retag to `!!str`, which every supported release
  answers alike. As in the lane, a version, unlike the tag, which the
  lane read with `yq -r`, passes through jq's `tostring` (`1e3`
  becomes `1E+3`), and spellings yq itself rewrites (`010`, `0x1F`,
  `1_000`, `+1`, `.5`, integers past 64 bits) keep yq's conversion,
  which can vary by yq release.
- **`yaml`**: the number as Jenkins renders it. global-jjb's release
  job reads the file with the Python `yq` (a jq wrapper over PyYAML,
  installed unpinned), which resolves plain scalars with YAML 1.2
  core rules and prints the value through jq.
- **`refuse`**: fail, naming the file and field, for any value that
  yq or the Python `yq` reads as a number, asking the author to quote
  it.

<!-- markdownlint-disable MD013 -->

| Version written | `literal`               | `yaml`          |
| --------------- | ----------------------- | --------------- |
| `7`, `-1`       | `7`, `-1`               | `7`, `-1`       |
| `1.10`, `2.0`   | `1.10`, `2.0`           | `1.1`, `2.0`    |
| `1e3`, `0.10e1` | `1E+3` (refused), `1.0` | `1000.0`, `1.0` |
| `010`, `0x1F`   | `10`, `31`              | `10`, `31`      |
| `1_000`         | `1000`                  | `1_000`         |
| `"1.10"`        | `1.10`                  | `1.10`          |

<!-- markdownlint-enable MD013 -->

The `yaml` column is what the Python `yq` 4.4.0 printed. It follows
YAML 1.2 rather than PyYAML's YAML 1.1, so `010` is ten, not eight,
and `1_000`, `0b101` and the sexagesimal `1:30` stay text; the action
matches that. A number Jenkins would print as `inf` or `nan` fails.

### Registry overrides

The promotion job logs in to the pull and push registries with the
configured registry's credential. A merged release file naming another
host could send that credential anywhere, so an override may change
the port or the repository path, and never the host. Both sides lose
their path first and their port second before the comparison, so
`nexus3.example.org/onap` and `nexus3.example.org:10003` both stay on
`nexus3.example.org:10001`. The comparison is case-sensitive, as in
the lane. An empty input allows no override at all, and an override
against an input that is not itself `host[:port][/path]`, such as
`https://nexus3.example.org`, fails rather than trust a host read
from it.

### Reading YAML

Python's standard library has no YAML parser, so the action had three
choices:

- **A minimal parser for the constrained schema**, as
  build-metadata-action uses for top-level scalars. It cannot follow
  anchors, flow collections, block scalars or YAML's typing, and every
  gap would be a release read differently from the lane.
- **PyYAML or another library.** It needs an install step, and it reads
  YAML 1.1, where the lane's parser reads YAML 1.2: `on`, `yes` and
  `1:20` change type.
- **The yq the lane used**, mikefarah yq v4, which GitHub-hosted
  runners ship.

The action uses yq, with the lane's own expressions, such as
`yq -r '.container_release_tag // ""'`, and takes each scalar as bash's
`$(...)` captured it. Typing, anchors, merge keys and scalar styles
resolve as they did; the suite runs both against the same
yq. The action checks before anything else that the `yq` on `PATH` is
mikefarah yq v4.25.3 or a later v4, refusing the Python `yq` (a jq
wrapper) and older v4 releases, whose command line lacks `-r` and
needs the `eval` subcommand. It fails with that reason rather than at
the first release merge. Validation
then runs in Python, so the action needs no jq.

### Related actions and Jenkins

- **[verify-release-schema-action]** validates a release file against
  global-jjb's JSON Schema with `lftools`. This action mirrors the
  fields the promotion consumes rather than calling it: it fetches
  `lftools-uv` from PyPI and the schema from GitHub at run time, which
  a merge lane under an egress block cannot do, and it reads YAML
  through PyYAML. The two check different properties. The schema requires
  `project` and `ref`, which the promotion never reads, and requires
  strings where the lane accepts YAML numbers; it does not inspect
  `containers` entries at all, where this action applies the name and
  tag grammars. A verify lane after full Jenkins parity can run both.
- **[build-metadata-action]** reports the release files present in the
  tree, so a workflow knows a repository is release-ready. This action
  answers a different question, which release file a given commit
  adds, the one a merge can act on.
- **Jenkins**: global-jjb's `release-job.sh` takes the release file a
  merge adds with `git diff-tree` against `HEAD^1`, and its verify jobs
  check that file against the schema before the change merges. The
  verify usage above covers the same ground without registry access.

## Compatibility

`tests/legacy/detect-release-merge.sh` holds the lane's step body,
extracted verbatim from docker-workflows at
`ac1f91064b609f9be68ec5c93e1a47ba1ada8741`. `tests/test_equivalence.py`
builds real git histories and runs that body and the action in each,
with the same git, yq and jq, comparing exit status, the five outputs,
and annotations: the same levels, with the lane's message as the
start of the action's, which may add the specific reason.

The scenarios cover which files count (no release file, other
distribution types, deleted, renamed and nested files, merge commits
from either side), every `container_release_tag` and `containers`
rule, number versions, and the registry overrides. Number scenarios
assume jq 1.7 and yq 4.53.3 or later, and skip with older tools.

The action departs from the lane on purpose where the lane missed a
release, released something unusable, or failed without saying why.
`DeliberateDifferenceTest` asserts each case side by side:

<!-- markdownlint-disable MD013 -->

| Case                                       | Lane                                    | Action                         |
| ------------------------------------------ | --------------------------------------- | ------------------------------ |
| Depth-1 checkout                           | Finds no release                        | Fails, naming `fetch-depth: 2` |
| Root commit                                | No release                              | No release, with a notice      |
| Non-ASCII file name                        | Skips the release                       | Detects it                     |
| Non-UTF-8 file name                        | Skips the release                       | Fails, naming the file         |
| Two or more YAML documents                 | Skips the release, or fails on the list | Fails, naming the cause        |
| Invalid YAML, a list document, `.inf`      | Fails with no annotation                | Fails with an annotation       |
| Empty container name                       | Outputs it                              | Fails                          |
| Name Docker refuses, such as `a..b`        | Outputs it                              | Fails                          |
| Repository path over 255 characters        | Outputs it                              | Fails                          |
| Merge into a branch holding a release file | Detects that file again                 | Detects merged-in files alone  |
| Sparse checkout without `releases/`        | Takes the file for a deletion           | Fails, naming the fix          |
| Name or version ending in a newline        | Outputs it                              | Fails                          |
| Symlinked release file                     | Follows it                              | Fails                          |
| `releases/` replaced by a symlink          | Reads files through it                  | Takes them as deleted          |
| No `yq` on the runner, no release          | Passes                                  | Fails                          |

<!-- markdownlint-enable MD013 -->

The lane's jq accepted the empty name and the trailing newlines: it
splits `""` into no components at all, and its `$` also matches before
a final newline. Its name pattern also took any run of `.`, `_` and
`-` inside a component, where Docker allows one `.`, one `_`, `__` or
a run of dashes. None of these values works as an image reference.

To swap the lane's step, replace its `run:` body and `env:` block:

```yaml
- name: 'Detect container release file in merged commit'
  id: detect
  uses: lfreleng-actions/docker-release-detect-action@<sha>  # vX.Y.Z
  with:
    snapshot_registry: ${{ inputs.snapshot_registry }}
    release_registry: ${{ inputs.release_registry }}
```

The job's `outputs:` block and its checkouts stay as they are.

## Notes

- Needs `python3` 3.10 or later and mikefarah `yq` v4.25.3 or a later
  v4 on the runner, as GitHub-hosted runners provide, and installs nothing.
- The action reads the checkout and never runs repository content.
  Python's isolated mode keeps the checked-out tree off the import
  path, so a repository cannot substitute its own modules for the
  action's. It refuses release files that are, or lie under, a
  symlink, which on an untrusted pull request could point outside
  the checkout.

### Development

```bash
python3 -m unittest discover -s tests -t .
```

The suite needs `git` and `yq`; the differential tests also need
`bash` and `jq`, 1.7 or later for the number cases. The CI workflow
adds real `actions/checkout` histories at depths 1 and 2, and fixture
repositories for a release merge and a verify run.

[build-metadata-action]: https://github.com/lfreleng-actions/build-metadata-action
[docker-promote-action]: https://github.com/lfreleng-actions/docker-promote-action
[docker-workflows]: https://github.com/lfreleng-actions/docker-workflows
[docker-workflows#36]: https://github.com/lfreleng-actions/docker-workflows/issues/36
[verify-release-schema-action]: https://github.com/lfreleng-actions/verify-release-schema-action
[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/docker-release-detect-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/docker-release-detect-action/main.svg
