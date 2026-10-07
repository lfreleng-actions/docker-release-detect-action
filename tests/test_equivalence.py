# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Differential tests: the action against the merge lane's inline step.

tests/legacy holds the check-release step body verbatim. Each scenario
builds a real git history, then runs that body and the action in it,
with the real git, yq and jq, and compares:

* exit status
* the five outputs the lane publishes
* annotations, in order: the same level, and the lane's message as the
  prefix of the action's (which may add the specific reason)

DeliberateDifferenceTest then pins, side by side, each case where the
action departs from the lane on purpose, with the lane's behaviour
asserted too, so that the reason for each departure stays visible.
"""

from __future__ import annotations

import os
import re
import shutil
import unittest
from collections.abc import Callable
from dataclasses import dataclass

from tests.support import (
    MAVEN,
    RELEASE,
    SNAPSHOT,
    VALID,
    Repo,
    Run,
    SandboxTestCase,
    container_file,
    jq_version,
    run_action,
    run_legacy,
    yq_version,
)

LANE_OUTPUTS = (
    "has_release",
    "version",
    "containers_json",
    "pull_registry",
    "push_registry",
)


@dataclass(frozen=True)
class Scenario:
    """A history to build, and the registry inputs to run it with."""

    build: Callable[[Repo], object]
    snapshot: str = SNAPSHOT
    release: str = RELEASE
    # jq 1.6 prints numbers as doubles (1.10 as 1.1); the action follows
    # jq 1.7, which ubuntu-24.04 runners ship. yq before 4.53.3 makes
    # the lane print 1.10 as 1.1 too, where the action's default
    # numeric_versions (literal) keeps the text as written.
    needs_jq17: bool = False


def adds(files: dict[str, str | None]) -> Callable[[Repo], object]:
    """A commit on top of the initial one, changing ``files``."""
    return lambda repo: repo.commit(files)


def then(
    first: dict[str, str | None], second: dict[str, str | None]
) -> Callable[[Repo], object]:
    """Two commits; the second is the one under test."""

    def build(repo: Repo) -> None:
        repo.commit(first)
        repo.commit(second)

    return build


def merge_brings_release(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"releases/1.2.3-container.yaml": VALID})
    repo.git("checkout", "-q", "main")
    repo.commit({"src/other.txt": "other\n"})
    repo.merge("feature")


def merge_release_on_first_parent(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"src/feature.txt": "feature\n"})
    repo.git("checkout", "-q", "main")
    repo.commit({"releases/1.2.3-container.yaml": VALID})
    repo.merge("feature")


def release(tag: str = "1.2.3", **kwargs: str) -> Callable[[Repo], object]:
    """Adds one container release file built by container_file."""
    return adds({"releases/x-container.yaml": container_file(tag, **kwargs)})


def entries(*lines: str) -> str:
    """A containers block from its lines, indented under the key."""
    return "".join(f"  {line}\n" for line in lines)


def version(value: str) -> Callable[[Repo], object]:
    """One container whose version is the YAML scalar ``value``."""
    return release(containers=entries("- name: app", f"  version: {value}"))


def overrides(
    pull: str = "", push: str = "", snapshot: str = SNAPSHOT, release_: str = RELEASE
) -> Scenario:
    """A valid release file carrying registry overrides."""
    extra = ""
    if pull:
        extra += f"container_pull_registry: {pull}\n"
    if push:
        extra += f"container_push_registry: {push}\n"
    return Scenario(release(extra=extra), snapshot, release_)


SCENARIOS: dict[str, Scenario] = {
    # Which files count
    "no release file": Scenario(adds({"src/app.py": "print()\n"})),
    "empty commit": Scenario(adds({})),
    "container release file": Scenario(adds({"releases/1.2.3-container.yaml": VALID})),
    "maven release file": Scenario(adds({"releases/1.2.3-maven.yaml": MAVEN})),
    "maven and container files": Scenario(
        adds({"releases/a-maven.yaml": MAVEN, "releases/b-container.yaml": VALID})
    ),
    "no distribution_type": Scenario(adds({"releases/x.yaml": "project: x\n"})),
    "distribution_type Container": Scenario(
        adds({"releases/x.yaml": VALID.replace("type: container", "type: Container")})
    ),
    "distribution_type false": Scenario(
        adds({"releases/x.yaml": "distribution_type: false\n"})
    ),
    "distribution_type quoted": Scenario(
        adds({"releases/x.yaml": VALID.replace("type: container", 'type: "container"')})
    ),
    "README under releases": Scenario(
        adds({"releases/README.md": "# Releases\n\nOne file per release.\n"})
    ),
    "empty release file": Scenario(adds({"releases/empty.yaml": ""})),
    "nested release file": Scenario(adds({"releases/sub/x-container.yaml": VALID})),
    "release file outside releases": Scenario(adds({"other/x-container.yaml": VALID})),
    "two container files": Scenario(
        adds({"releases/a-container.yaml": VALID, "releases/b-container.yaml": VALID})
    ),
    "modifies a release file": Scenario(
        then(
            {"releases/x-container.yaml": VALID},
            {"releases/x-container.yaml": VALID.replace("tag: 1.2.3", "tag: 1.2.4")},
        )
    ),
    "deletes a release file": Scenario(
        then({"releases/x-container.yaml": VALID}, {"releases/x-container.yaml": None})
    ),
    "renames a release file": Scenario(
        then(
            {"releases/x-container.yaml": VALID},
            {"releases/x-container.yaml": None, "releases/y-container.yaml": VALID},
        )
    ),
    "merge brings a release file": Scenario(merge_brings_release),
    "anchors and merge keys": Scenario(
        adds(
            {
                "releases/x.yaml": (
                    "base: &base {version: 2.0.0}\n"
                    "kind: &kind container\n"
                    "distribution_type: *kind\n"
                    "container_release_tag: 2.0.0\n"
                    "containers:\n"
                    "  - {<<: *base, name: app}\n"
                )
            }
        )
    ),
    "CRLF line endings": Scenario(
        adds({"releases/x-container.yaml": VALID.replace("\n", "\r\n")})
    ),
    # container_release_tag
    "tag missing": Scenario(release(tag="null")),
    "tag empty": Scenario(release(tag='""')),
    "tag leading dash": Scenario(release(tag='"-1.2.3"')),
    "tag with plus": Scenario(release(tag="1.2.3+build")),
    "tag with space": Scenario(release(tag='"1.2 3"')),
    "tag underscore first": Scenario(release(tag="_1.2.3")),
    "tag 128 characters": Scenario(release(tag="v" + "1" * 127)),
    "tag 129 characters": Scenario(release(tag="v" + "1" * 128)),
    "tag number 1.10": Scenario(release(tag="1.10")),
    "tag hex number": Scenario(release(tag="0x1F")),
    "tag true": Scenario(release(tag="true")),
    "tag block scalar": Scenario(release(tag="|\n  1.2.3")),
    "tag list": Scenario(release(tag="[1, 2]")),
    # containers
    "containers missing": Scenario(
        adds(
            {
                "releases/x.yaml": "distribution_type: container\ncontainer_release_tag: 1\n"
            }
        )
    ),
    "containers empty": Scenario(release(containers="  []\n")),
    "containers null": Scenario(release(containers="  null\n")),
    "containers mapping": Scenario(release(containers="  app: 1.0.0\n")),
    "containers string": Scenario(release(containers="  app\n")),
    "entry is a string": Scenario(release(containers=entries("- app"))),
    "entry is null": Scenario(release(containers=entries("- null"))),
    "name missing": Scenario(release(containers=entries("- version: 1.0.0"))),
    "name number": Scenario(release(containers=entries("- {name: 1, version: 1.0.0}"))),
    "name upper case": Scenario(
        release(containers=entries("- {name: App, version: 1}"))
    ),
    "name nested path": Scenario(
        release(containers=entries("- {name: onap/policy/api, version: 1.0.0}"))
    ),
    "name double slash": Scenario(
        release(containers=entries("- {name: onap//api, version: 1.0.0}"))
    ),
    "name leading slash": Scenario(
        release(containers=entries("- {name: /api, version: 1}"))
    ),
    "name leading dash": Scenario(
        release(containers=entries("- {name: -api, version: 1}"))
    ),
    "name trailing dot": Scenario(
        release(containers=entries("- {name: api., version: 1}"))
    ),
    "name separators": Scenario(
        release(containers=entries("- {name: a.b_c-d__e, version: 1.0.0}"))
    ),
    "duplicate names": Scenario(
        release(
            containers=entries(
                "- {name: app, version: 1.0.0}", "- {name: app, version: 2.0.0}"
            )
        )
    ),
    "version missing": Scenario(release(containers=entries("- name: app"))),
    "version null": Scenario(version("null")),
    "version boolean": Scenario(version("true")),
    "version list": Scenario(version("[1]")),
    "version with space": Scenario(version('"1.0 beta"')),
    "version leading dot": Scenario(version('".1"')),
    "version 129 characters": Scenario(version("v" + "1" * 128)),
    "version date": Scenario(version("2026-10-07")),
    "extra keys dropped": Scenario(
        release(containers=entries("- {name: app, version: 1.0.0, digest: sha256:00}"))
    ),
    "version integer": Scenario(version("7"), needs_jq17=True),
    "version 1.0": Scenario(version("1.0"), needs_jq17=True),
    "version 1.10": Scenario(version("1.10"), needs_jq17=True),
    "version octal-looking 010": Scenario(version("010"), needs_jq17=True),
    "version hex": Scenario(version("0x1F"), needs_jq17=True),
    "version underscores": Scenario(version("1_000"), needs_jq17=True),
    "version exponent": Scenario(version("1e3"), needs_jq17=True),
    "version exponent to plain": Scenario(version("0.10e1"), needs_jq17=True),
    "version negative": Scenario(version("-1"), needs_jq17=True),
    "version huge integer": Scenario(version("100000000000000000001"), needs_jq17=True),
    # Registry overrides
    "overrides on the configured host": overrides(
        "nexus3.example.org:10003", "nexus3.example.org:10004"
    ),
    "override with a path, no port": overrides("nexus3.example.org/onap/staging"),
    "override with port and path": overrides(push="nexus3.example.org:443/onap"),
    "override on another host": overrides("evil.example.com:10001"),
    "override on a subdomain": overrides(push="nexus3.example.org.evil.com"),
    "override with a scheme": overrides("https://nexus3.example.org"),
    "override with userinfo": overrides("user@nexus3.example.org"),
    "override with upper-case path": overrides("nexus3.example.org/ONAP"),
    "override with upper-case host": overrides("NEXUS3.example.org"),
    "override with trailing slash": overrides("nexus3.example.org/"),
    "override, input empty": overrides("nexus3.example.org:10003", snapshot=""),
    "override, input carries a path": overrides(
        "nexus3.example.org:10003", snapshot="nexus3.example.org/onap"
    ),
    "override path separators": overrides(push="nexus3.example.org/a.b/c__d/e---f"),
    "no override, inputs empty": Scenario(release(), snapshot="", release=""),
}

# Scenarios where the lane prints a message the action replaces with a
# more specific one; levels are still compared.
LEVEL_ONLY: frozenset[str] = frozenset()


def _slug(name: str) -> str:
    return re.sub(r"\W+", "_", name.lower()).strip("_")


@unittest.skipUnless(
    shutil.which("yq") and shutil.which("jq"), "the lane body needs yq and jq"
)
class EquivalenceTest(SandboxTestCase):
    """The action reproduces the lane on every scenario."""

    def compare(self, name: str, scenario: Scenario) -> None:
        if scenario.needs_jq17 and jq_version() < (1, 7):
            self.skipTest("number formatting follows jq 1.7")
        if scenario.needs_jq17 and yq_version() < (4, 53, 3):
            self.skipTest("number formatting follows yq 4.53.3")
        repo = self.sandbox.repo()
        scenario.build(repo)
        lane = run_legacy(repo, scenario.snapshot, scenario.release)
        action = run_action(
            repo,
            snapshot_registry=scenario.snapshot,
            release_registry=scenario.release,
        )
        self.assertEqual(lane.status == 0, action.status == 0, _both(lane, action))
        self.assertEqual(
            lane.outputs,
            {key: action.outputs[key] for key in lane.outputs},
            _both(lane, action),
        )
        if action.status == 0:
            self.assertEqual(sorted(lane.outputs), sorted(LANE_OUTPUTS))
        self.assertEqual(
            len(lane.annotations), len(action.annotations), _both(lane, action)
        )
        for ours, theirs in zip(action.annotations, lane.annotations, strict=True):
            level = theirs.split("::", 2)[1]
            self.assertTrue(ours.startswith(f"::{level}::"), _both(lane, action))
            if name not in LEVEL_ONLY:
                self.assertTrue(ours.startswith(theirs), _both(lane, action))


def _both(lane: Run, action: Run) -> str:
    return f"\n--- lane ---\n{lane.stdout}\n--- action ---\n{action.stdout}"


def _make(name: str, scenario: Scenario) -> Callable[[EquivalenceTest], None]:
    def test(self: EquivalenceTest) -> None:
        self.compare(name, scenario)

    test.__doc__ = name
    return test


for _name, _scenario in SCENARIOS.items():
    setattr(EquivalenceTest, f"test_{_slug(_name)}", _make(_name, _scenario))


@unittest.skipUnless(
    shutil.which("yq") and shutil.which("jq"), "the lane body needs yq and jq"
)
class DeliberateDifferenceTest(SandboxTestCase):
    """Where the action departs from the lane, and why, side by side."""

    def release_commit(self, files: dict[str, str | None] | None = None) -> Repo:
        repo = self.sandbox.repo()
        repo.commit(files or {"releases/x-container.yaml": VALID})
        return repo

    def test_shallow_clone_fails_loudly(self) -> None:
        # A depth-1 checkout of the release merge: the lane's diff-tree
        # sees a root commit and emits nothing, so the release is lost.
        clone = self.release_commit().clone("shallow", depth=1)
        lane = run_legacy(clone)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "false")
        action = run_action(clone)
        self.assertEqual(action.status, 1)
        self.assertIn("boundary of a shallow clone", action.annotations[0])
        self.assertIn("fetch-depth: 2", action.annotations[0])
        self.assertEqual(action.outputs, {})

    def test_root_commit_is_reported(self) -> None:
        repo = Repo(self.sandbox, self.sandbox.root / "root")
        repo.git("init", "-q", "-b", "main")
        repo.commit({"releases/x-container.yaml": VALID})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual((lane.status, action.status), (0, 0))
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertEqual(action.outputs["has_release"], "false")
        self.assertEqual(lane.annotations, [])
        self.assertEqual(len(action.annotations), 1)
        self.assertTrue(action.annotations[0].startswith("::notice::"))
        self.assertIn("root commit", action.annotations[0])

    def test_invalid_yaml_is_annotated(self) -> None:
        # The lane's set -e ends the step on yq's failure, unannotated.
        repo = self.release_commit({"releases/x.yaml": "a: [1, 2\n"})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertNotEqual(lane.status, 0)
        self.assertEqual(lane.annotations, [])
        self.assertEqual(action.status, 1)
        self.assertTrue(
            action.annotations[0].startswith(
                "::error::Cannot read releases/x.yaml as YAML: "
            )
        )

    def test_top_level_list_is_annotated(self) -> None:
        repo = self.release_commit({"releases/x.yaml": "- container\n"})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertNotEqual(lane.status, 0)
        self.assertEqual(lane.annotations, [])
        self.assertEqual(action.status, 1)
        self.assertIn("Cannot read releases/x.yaml as YAML", action.annotations[0])

    def test_unrepresentable_version_is_annotated(self) -> None:
        # yq cannot write .inf as JSON; the lane stops unannotated.
        repo = self.release_commit(
            {
                "releases/x.yaml": container_file(
                    containers="  - {name: app, version: .inf}\n"
                )
            }
        )
        lane, action = run_legacy(repo), run_action(repo)
        self.assertNotEqual(lane.status, 0)
        self.assertEqual(lane.annotations, [])
        self.assertEqual(action.status, 1)
        self.assertIn("Cannot read releases/x.yaml as YAML", action.annotations[0])

    def test_several_documents_all_container(self) -> None:
        # The lane reads distribution_type as two values joined, which
        # is not 'container', and skips the release with a notice.
        two = VALID + "---\n" + VALID
        repo = self.release_commit({"releases/x-container.yaml": two})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertTrue(lane.annotations[0].startswith("::notice::Ignoring"))
        self.assertEqual(action.status, 1)
        self.assertIn("holds 2 YAML documents", action.annotations[0])

    def test_trailing_document_marker(self) -> None:
        # A trailing '---' opens an empty second document. The lane
        # fails on the containers check, blaming the list; the action
        # names the cause.
        repo = self.release_commit({"releases/x-container.yaml": VALID + "---\n"})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 1)
        self.assertIn("Invalid containers list", lane.annotations[0])
        self.assertEqual(action.status, 1)
        self.assertIn("holds 2 YAML documents", action.annotations[0])

    def test_non_ascii_file_name_is_found(self) -> None:
        # diff-tree C-quotes such names unless -z; the quoted name is
        # no file, so the lane skipped the release silently.
        repo = self.release_commit({"releases/café-container.yaml": VALID})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual((lane.status, action.status), (0, 0))
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertEqual(action.outputs["has_release"], "true")
        self.assertEqual(action.outputs["release_file"], "releases/café-container.yaml")

    def test_non_utf8_file_name_fails(self) -> None:
        # Outputs are UTF-8, so the name could never be published. Built
        # with plumbing and left unchecked out: some file systems refuse
        # such a name, and the lane skips it either way.
        repo = self.sandbox.repo()
        repo.write({"blob.yaml": VALID})
        blob = repo.git("hash-object", "-w", "blob.yaml")
        name = os.fsdecode(b"releases/\xff-container.yaml")
        repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        repo.git("commit", "-q", "-m", "Release")
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual((lane.status, action.status), (0, 1))
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertIn(
            "releases/\\xff-container.yaml is not a valid UTF-8", action.annotations[0]
        )
        self.assertEqual(action.outputs, {})

    def test_empty_name_is_refused(self) -> None:
        # jq splits "" into no components, so all() holds vacuously.
        repo = self.release_commit(
            {
                "releases/x.yaml": container_file(
                    containers='  - {name: "", version: 1}\n'
                )
            }
        )
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["containers_json"], '[{"name":"","version":"1"}]')
        self.assertEqual(action.status, 1)
        self.assertIn(
            "entry 1: name '' is not a repository path", action.annotations[0]
        )

    def test_merge_does_not_rerelease_the_target_branch(self) -> None:
        # diff-tree takes --first-parent as a walk option and ignores it,
        # so the lane's -m diffed HEAD against every parent: a release
        # file main already held differs from the merged branch and was
        # detected again. The action diffs against the first parent.
        repo = self.sandbox.repo()
        merge_release_on_first_parent(repo)
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual((lane.status, action.status), (0, 0))
        self.assertEqual(lane.outputs["has_release"], "true")
        self.assertEqual(action.outputs["has_release"], "false")

    def test_sparse_checkout_without_releases_fails(self) -> None:
        # The release file is in the commit but not on disk; the lane's
        # [ -f ] took it for a deletion and missed the release.
        repo = self.release_commit()
        repo.git("sparse-checkout", "set", "src")
        self.assertFalse((repo.path / "releases").exists())
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual((lane.status, action.status), (0, 1))
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertIn("but absent from the checkout", action.annotations[0])

    def test_overlong_repository_path_is_refused(self) -> None:
        # Every component is valid, but a registry refuses a repository
        # path over 255 characters, so the promotion would fail.
        name = "a" * 256
        repo = self.release_commit(
            {
                "releases/x.yaml": container_file(
                    containers=f"  - {{name: {name}, version: 1}}\n"
                )
            }
        )
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "true")
        self.assertEqual(action.status, 1)
        self.assertIn("256 characters, over Docker's 255", action.annotations[0])

    def test_name_docker_refuses_is_refused(self) -> None:
        # The lane allowed any run of '.', '_' and '-' inside a
        # component; Docker's grammar allows '.', '_', '__' or dashes.
        for name in ("a..b", "a___b", "a_-b", "a.-b", "onap/a._b"):
            with self.subTest(name=name):
                repo = self.release_commit(
                    {
                        "releases/x.yaml": container_file(
                            containers=f"  - {{name: {name}, version: 1}}\n"
                        )
                    }
                )
                lane, action = run_legacy(repo), run_action(repo)
                self.assertEqual(lane.status, 0)
                self.assertEqual(lane.outputs["has_release"], "true")
                self.assertEqual(action.status, 1)
                self.assertIn(
                    f"name '{name}' is not a repository path", action.annotations[0]
                )

    def test_version_with_trailing_newline_is_refused(self) -> None:
        # jq's '$' also matches before a final newline, so a block
        # scalar version reached the output with its newline.
        block = "  - name: app\n    version: |\n      1.0.0\n"
        repo = self.release_commit(
            {"releases/x.yaml": container_file(containers=block)}
        )
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertIn('"version":"1.0.0\\n"', lane.outputs["containers_json"])
        self.assertEqual(action.status, 1)
        self.assertIn("version '1.0.0\\n' is not a valid Docker tag", action.stdout)

    def test_name_with_trailing_newline_is_refused(self) -> None:
        block = '  - {name: "app\\n", version: 1.0.0}\n'
        repo = self.release_commit(
            {"releases/x.yaml": container_file(containers=block)}
        )
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "true")
        self.assertEqual(action.status, 1)
        self.assertIn("is not a repository path", action.annotations[0])

    def test_symlinked_release_file_is_refused(self) -> None:
        repo = self.sandbox.repo()
        repo.write({"data/real.yaml": VALID})
        (repo.path / "releases").mkdir()
        (repo.path / "releases" / "x-container.yaml").symlink_to("../data/real.yaml")
        repo.commit({})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "true")
        self.assertEqual(action.status, 1)
        self.assertIn("is a symbolic link", action.annotations[0])

    def test_release_directory_replaced_by_a_symlink(self) -> None:
        # diff-tree lists the old releases/ files as deleted; through the
        # new symlink they resolve outside the checkout, where a file of
        # the same name must not be read as the release.
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        (outside / "x-container.yaml").write_text(VALID)
        repo = self.release_commit()
        repo.git("rm", "-q", "-r", "releases")
        (repo.path / "releases").symlink_to(outside)
        repo.commit({})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.outputs["has_release"], "true")
        self.assertEqual(action.status, 0, action.stdout)
        self.assertEqual(action.outputs["has_release"], "false")

    def test_self_referential_symlink_is_refused(self) -> None:
        # Its realpath is its own path, and it is no file to [ -f ].
        repo = self.sandbox.repo()
        (repo.path / "releases").mkdir()
        (repo.path / "releases" / "x-container.yaml").symlink_to("x-container.yaml")
        repo.commit({})
        lane, action = run_legacy(repo), run_action(repo)
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertEqual(action.status, 1, action.stdout)
        self.assertIn("is a symbolic link", action.annotations[0])

    def test_missing_yq_fails_even_without_a_release(self) -> None:
        # The lane only calls yq once a release file appears, so a
        # runner without it fails on the release merge itself.
        repo = self.sandbox.repo()
        repo.commit({"src/app.py": "print()\n"})
        bare = self.sandbox.without_yq()
        lane = run_legacy(repo, env_path=bare)
        action = run_action(repo, env_path=bare)
        self.assertEqual(lane.status, 0)
        self.assertEqual(lane.outputs["has_release"], "false")
        self.assertEqual(action.status, 1)
        self.assertIn("yq is not on PATH", action.annotations[0])


if __name__ == "__main__":
    unittest.main()
