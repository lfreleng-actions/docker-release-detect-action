# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Detect a container release file in a commit and publish its contents.

The flow is the merge lane's check-release step: list the files under
releases/ that the commit touched, keep those still present, sort them
by distribution_type, and accept exactly one container release file,
whose tag, containers and registry overrides are then validated.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from scripts import yq
from scripts.changes import Changes, find_changes, in_commit
from scripts.gha import (
    ActionError,
    annotate,
    append_summary,
    env,
    env_bool,
    log,
    markdown_cell,
    set_outputs,
    visible,
)
from scripts.numbers import MODES, Node, resolve
from scripts.release import (
    check_override,
    check_path_lengths,
    check_tag,
    parse_containers,
)

# A revision git can take as one argument: no leading '-', which git
# would read as an option, and no whitespace or control characters.
_REVISION = re.compile(r"[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]*")


@dataclass(frozen=True)
class Settings:
    """The action's inputs."""

    path: str
    base: str
    snapshot_registry: str
    release_registry: str
    summary: bool
    numeric_versions: str = "literal"


@dataclass
class Result:
    """What the step publishes."""

    release_file: str = ""
    version: str = ""
    containers: list[dict[str, str]] = field(default_factory=list)
    pull_registry: str = ""
    push_registry: str = ""
    # Release files of other distribution types: (path, type).
    ignored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def containers_json(self) -> str:
        if not self.containers:
            return ""
        return json.dumps(self.containers, separators=(",", ":"))

    def outputs(self) -> dict[str, str]:
        has_release = bool(self.release_file)
        return {
            "has_release": "true" if has_release else "false",
            "version": self.version,
            "containers_json": self.containers_json,
            "pull_registry": self.pull_registry,
            "push_registry": self.push_registry,
            "release_file": self.release_file,
            "container_count": str(len(self.containers)),
            "distribution_type": "container" if has_release else "",
        }


def read_settings() -> Settings:
    """Read and check the INPUT_* variables action.yaml binds."""
    base = env("INPUT_BASE").strip()
    if base and not _REVISION.fullmatch(base):
        raise ActionError(
            f"base must be a single git revision (a branch, tag or commit), "
            f"got '{base}'"
        )
    numeric_versions = env("INPUT_NUMERIC_VERSIONS").strip() or "literal"
    if numeric_versions not in MODES:
        raise ActionError(
            f"numeric_versions must be one of {', '.join(MODES)}, "
            f"got '{visible(numeric_versions)}'"
        )
    return Settings(
        path=env("INPUT_PATH").strip() or ".",
        base=base,
        snapshot_registry=env("INPUT_SNAPSHOT_REGISTRY"),
        release_registry=env("INPUT_RELEASE_REGISTRY"),
        summary=env_bool("INPUT_SUMMARY", "summary", default=True),
        numeric_versions=numeric_versions,
    )


def _classify(changes: Changes, result: Result) -> list[str]:
    """The container release files among the changed ones."""
    found: list[str] = []
    top = os.path.realpath(".")
    for path in changes.files:
        # Removed by the commit: not a release trigger. Decided from
        # HEAD's tree, not the checkout, where a symlink replacing
        # releases/ could make a removed path resolve to another file.
        if not in_commit(changes.head, path):
            continue
        # A symlink, at the file or any directory above it, could point
        # outside the checkout; on an untrusted verify run that would
        # read an arbitrary file into the log. realpath alone misses a
        # link to itself, which resolves to its own path.
        if os.path.islink(path) or os.path.realpath(path) != os.path.join(
            top, os.path.normpath(path)
        ):
            raise ActionError(
                f"{path} is a symbolic link, or lies under one; a release file "
                "must be a regular file"
            )
        if not os.path.isfile(path):
            # In the commit but not on disk: a sparse checkout left it
            # out, and skipping it would miss the release.
            if not os.path.lexists(path):
                raise ActionError(
                    f"{path} is in commit {changes.head[:12]} but absent from "
                    "the checkout, as with a sparse checkout; include "
                    "releases/ (git sparse-checkout add releases)"
                )
            continue
        documents = yq.document_count(path)
        if documents != 1:
            raise ActionError(
                f"{path} holds {documents} YAML documents; a release file "
                "holds exactly one"
            )
        distribution_type = yq.scalar(path, "distribution_type")
        if distribution_type == "container":
            found.append(path)
            continue
        result.ignored.append((path, distribution_type))
        annotate(
            "notice",
            f"Ignoring non-container release file: {path} "
            f"(distribution_type: {visible(distribution_type) or 'unset'})",
        )
    return found


def detect(changes: Changes, settings: Settings) -> Result:
    """Find and validate the commit's container release file."""
    result = Result()
    if changes.root:
        annotate(
            "notice",
            f"HEAD {changes.head} is a root commit: with no parent to compare "
            "against, it is not treated as a release",
        )
    found = _classify(changes, result)
    if not found:
        log(f"No container release file in {changes.scope}")
        return result
    # Several would make the promotion arbitrary.
    if len(found) > 1:
        raise ActionError(
            f"Multiple container release files in {changes.scope}: " + ", ".join(found)
        )
    release_file = found[0]
    log(f"Container release file detected: {release_file}")
    mode = settings.numeric_versions
    version = yq.scalar(release_file, "container_release_tag")
    if mode != "literal":
        # yq -r already prints a number's text, so literal needs no node.
        node = Node.from_yq(yq.node(release_file, "container_release_tag"))
        if node is not None:
            where = f"container_release_tag in {release_file}"
            version = resolve(node, mode, version, where)
    check_tag(version, release_file)
    containers = parse_containers(
        yq.containers_json(release_file),
        release_file,
        mode,
        lambda: yq.version_nodes(release_file),
    )
    pull_registry = yq.scalar(release_file, "container_pull_registry")
    push_registry = yq.scalar(release_file, "container_push_registry")
    check_override(
        pull_registry,
        settings.snapshot_registry,
        "container_pull_registry",
        "snapshot_registry",
        release_file,
    )
    check_override(
        push_registry,
        settings.release_registry,
        "container_push_registry",
        "release_registry",
        release_file,
    )
    check_path_lengths(
        (
            pull_registry or settings.snapshot_registry,
            push_registry or settings.release_registry,
        ),
        containers,
        release_file,
    )
    result.release_file = release_file
    result.version = version
    result.containers = containers
    result.pull_registry = pull_registry
    result.push_registry = push_registry
    log(f"Release tag: {version}")
    log(f"Containers: {result.containers_json}")
    return result


def _registry_cell(override: str, fallback_input: str) -> str:
    if override:
        return markdown_cell(override)
    return f"`{fallback_input}` input"


def summary(changes: Changes, result: Result) -> str:
    """The step summary report."""
    lines = ["## 🐳 Container release detection", ""]
    commit = markdown_cell(changes.head[:12])
    if not result.release_file:
        lines.append(
            f"No container release file in {markdown_cell(changes.scope)} ({commit})."
        )
    else:
        lines += [
            "| Field | Value |",
            "| ----- | ----- |",
            f"| Release file | {markdown_cell(result.release_file)} |",
            f"| Commit | {commit} |",
            f"| Release tag | {markdown_cell(result.version)} |",
            "| Pull registry | "
            f"{_registry_cell(result.pull_registry, 'snapshot_registry')} |",
            "| Push registry | "
            f"{_registry_cell(result.push_registry, 'release_registry')} |",
            "",
            "| Container | Version |",
            "| --------- | ------- |",
        ]
        lines += [
            f"| {markdown_cell(c['name'])} | {markdown_cell(c['version'])} |"
            for c in result.containers
        ]
    if result.ignored:
        lines += ["", "Ignored release files of other distribution types:", ""]
        lines += [
            f"- {markdown_cell(path)} ({markdown_cell(kind or 'unset')})"
            for path, kind in result.ignored
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Run the action; return the process exit status."""
    try:
        settings = read_settings()
        try:
            os.chdir(settings.path)
        except OSError as error:
            raise ActionError(
                f"path '{settings.path}' is not a usable directory: {error.strerror}"
            ) from error
        yq.check_yq()
        changes = find_changes(settings.base)
        result = detect(changes, settings)
    except ActionError as error:
        annotate("error", str(error))
        return 1
    set_outputs(result.outputs())
    if settings.summary:
        append_summary(summary(changes, result))
    return 0
