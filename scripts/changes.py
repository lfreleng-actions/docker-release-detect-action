# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The files under releases/ that a commit, or a change, touched.

Without a base this is the merge lane's question: what did the checked
out commit change against its first parent? With a base it is a verify
run's: what does HEAD change since it forked from the base?

The lane asked git diff-tree directly, which answers a shallow clone's
boundary commit, whose parent the checkout lacks, exactly as it answers
a root commit: with nothing. A depth-1 checkout therefore never found a
release. Both cases are told apart here before diff-tree runs.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from scripts.gha import ActionError

# The lane's message for a failed diff-tree, kept as the prefix of ours.
DIFF_TREE_FAILED = (
    "git diff-tree failed; cannot detect release files (insufficient checkout history?)"
)


@dataclass(frozen=True)
class Changes:
    """What the comparison found."""

    files: list[str]
    head: str
    # Describes the comparison in messages: "merged commit", or the base.
    scope: str
    # Set when HEAD is a true root commit, which has nothing to compare.
    root: bool = False


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, check=False)


def _stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    return lines[0] if lines else f"exit status {proc.returncode}"


def _stdout(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def _require_top_level() -> None:
    proc = _git("rev-parse", "--show-prefix")
    if proc.returncode != 0:
        raise ActionError(f"Not a git work tree: {_stderr(proc)}")
    prefix = _stdout(proc)
    if prefix:
        raise ActionError(
            f"path must be the top level of the repository, where "
            f"releases/ lives; it points at the subdirectory '{prefix}'"
        )


def _resolve(revision: str, what: str) -> str:
    proc = _git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    if proc.returncode != 0:
        raise ActionError(f"{what} does not name a commit in the checkout")
    return _stdout(proc)


def _first_parent(head: str) -> str | None:
    """HEAD's first parent as its commit object records it, or None.

    Read from the raw object, not through rev-list: a shallow clone
    grafts its boundary commits parentless, so rev-list cannot tell a
    boundary from a true root, nor can the shallow file, which lists a
    root cloned at depth 1 as well.
    """
    proc = _git("cat-file", "commit", head)
    if proc.returncode != 0:
        raise ActionError(f"{DIFF_TREE_FAILED}: {_stderr(proc)}")
    header = proc.stdout.split(b"\n\n", 1)[0].decode("utf-8", "replace")
    for line in header.splitlines():
        if line.startswith("parent "):
            return line.split()[1]
    return None


def _present(commit: str) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def in_commit(commit: str, path: str) -> bool:
    """Whether ``path`` names an entry in ``commit``'s tree.

    rev-parse reads trees only. cat-file -e would need the blob, which
    a blob-filtered partial clone lacks and would fetch, or fail on.
    """
    proc = _git("rev-parse", "-q", "--verify", f"{commit}:{path}")
    return proc.returncode == 0


def _diff_tree(*revisions: str) -> list[str]:
    # -z: unquoted, NUL-separated names. The lane read the quoted form,
    # in which a name holding a byte above 0x7f, a quote or a control
    # character arrives in C quotes, names no file, and is skipped.
    proc = _git(
        "diff-tree",
        "--no-commit-id",
        "-r",
        "-z",
        "--name-only",
        *revisions,
        "--",
        "releases/",
    )
    if proc.returncode != 0:
        raise ActionError(f"{DIFF_TREE_FAILED}: {_stderr(proc)}")
    return [_decode(name) for name in proc.stdout.split(b"\0") if name]


def _decode(name: bytes) -> str:
    # Outputs and the step summary are UTF-8; a name git holds but
    # UTF-8 cannot carry could never be published.
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        shown = name.decode("utf-8", "backslashreplace")
        raise ActionError(
            f"{shown} is not a valid UTF-8 file name, so it cannot be "
            "published as an output; rename it"
        ) from None


def _merge_base(base: str, head: str) -> str:
    proc = _git("merge-base", base, head)
    if proc.returncode != 0 or not _stdout(proc):
        raise ActionError(
            f"base {base} and HEAD {head} share no commit in the checkout, "
            "so the change cannot be isolated; fetch more history "
            "(actions/checkout with fetch-depth: 0)"
        )
    return _stdout(proc)


def find_changes(base: str) -> Changes:
    """List the files under releases/ that HEAD changes.

    Runs in the repository's top level. Without ``base``, HEAD is
    compared with its first parent, as the merge lane meant to. With one,
    HEAD is compared with its merge base with ``base``.
    """
    _require_top_level()
    head = _resolve("HEAD", "HEAD")
    if base:
        base_sha = _resolve(base, f"base '{base}'")
        fork = _merge_base(base_sha, head)
        return Changes(_diff_tree(fork, head), head, f"changes since {base}")
    scope = "merged commit"
    parent = _first_parent(head)
    if parent is None:
        return Changes([], head, scope, root=True)
    if not _present(parent):
        raise ActionError(
            f"HEAD {head} has a parent the checkout lacks, as at the boundary "
            "of a shallow clone, so the files it changed cannot be "
            "determined and a release would be silently missed. Check out "
            "with fetch-depth: 2 or more (actions/checkout, "
            "checkout-gerrit-change-action), or run git fetch --deepen=1"
        )
    # Diff the recorded parent explicitly: a shallow HEAD stays grafted
    # parentless even once its parent is fetched, and diff-tree of HEAD
    # alone would then list nothing.
    return Changes(_diff_tree(parent, head), head, scope)
