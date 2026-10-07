# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Behaviour beyond the lane: history depth, verify runs, inputs, yq."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest

from scripts.gha import ActionError, annotate, markdown_cell, visible
from scripts.numbers import jq_tostring
from scripts.release import (
    check_override,
    check_path_lengths,
    check_tag,
    parse_containers,
    registry_host,
)
from tests.support import (
    MAVEN,
    VALID,
    Repo,
    Run,
    SandboxTestCase,
    container_file,
    run_action,
)

VALID_JSON = (
    '[{"name":"policy-api","version":"1.2.3-STAGING-20260101T000000Z"},'
    '{"name":"onap/policy-pap","version":"4.0.1"}]'
)


def errors(run: Run) -> list[str]:
    return [line for line in run.annotations if line.startswith("::error::")]


@unittest.skipUnless(shutil.which("yq"), "the action needs yq")
class HistoryTest(SandboxTestCase):
    """Checkout depth: shallow boundaries fail, true roots are reported."""

    def test_depth_two_finds_the_release(self) -> None:
        repo = self.sandbox.repo()
        repo.commit({"releases/x-container.yaml": VALID})
        run = run_action(repo.clone("clone", depth=2))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "true")
        self.assertEqual(run.outputs["containers_json"], VALID_JSON)

    def test_depth_one_fails_without_a_release_too(self) -> None:
        # Whether or not the commit holds a release, a run that cannot
        # tell must not report that there is none.
        repo = self.sandbox.repo()
        repo.commit({"src/app.py": "print()\n"})
        run = run_action(repo.clone("clone", depth=1))
        self.assertEqual(run.status, 1)
        self.assertIn("boundary of a shallow clone", errors(run)[0])
        self.assertEqual(run.outputs, {})

    def test_partial_clone_without_releases_fails(self) -> None:
        # A blob-filtered clone holds the trees, not the release file's
        # blob. With the remote out of reach, the sparse release must
        # still read as present, not as deleted.
        repo = self.sandbox.repo()
        repo.git("config", "uploadpack.allowFilter", "true")
        repo.commit({"releases/x-container.yaml": VALID, "src/a.txt": "a\n"})
        clone = Repo(self.sandbox, self.sandbox.root / "partial")
        clone.git(
            "clone",
            "-q",
            "--no-checkout",
            "--filter=blob:none",
            repo.path.as_uri(),
            str(clone.path),
        )
        clone.git("sparse-checkout", "set", "src")
        clone.git("checkout", "-q", "main")
        clone.git("remote", "set-url", "origin", (self.sandbox.root / "gone").as_uri())
        run = run_action(clone)
        self.assertEqual(run.status, 1, run.stdout)
        self.assertIn("but absent from the checkout", errors(run)[0])

    def test_depth_one_clone_of_a_true_root(self) -> None:
        # The shallow file lists a root cloned at depth 1 too; its
        # commit object, with no parent line, shows it is a real root.
        repo = Repo(self.sandbox, self.sandbox.root / "single")
        repo.git("init", "-q", "-b", "main")
        repo.commit({"releases/x-container.yaml": VALID})
        clone = repo.clone("clone", depth=1)
        self.assertEqual(clone.git("rev-parse", "--is-shallow-repository"), "true")
        run = run_action(clone)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "false")
        self.assertIn("is a root commit", run.annotations[0])

    def test_depth_two_merge_commit(self) -> None:
        repo = self.sandbox.repo()
        repo.git("checkout", "-q", "-b", "feature")
        repo.commit({"releases/x-container.yaml": VALID})
        repo.git("checkout", "-q", "main")
        repo.commit({"src/other.txt": "x\n"})
        repo.merge("feature")
        run = run_action(repo.clone("clone", depth=2))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["release_file"], "releases/x-container.yaml")

    def test_depth_one_merge_commit_fails(self) -> None:
        repo = self.sandbox.repo()
        repo.git("checkout", "-q", "-b", "feature")
        repo.commit({"releases/x-container.yaml": VALID})
        repo.git("checkout", "-q", "main")
        repo.merge("feature")
        run = run_action(repo.clone("clone", depth=1))
        self.assertEqual(run.status, 1)
        self.assertIn("fetch-depth: 2", errors(run)[0])

    def test_shallow_head_with_its_parent_present(self) -> None:
        # The parent arrived by another ref, but git still grafts HEAD
        # parentless while the shallow file lists it.
        repo = self.sandbox.repo()
        repo.commit({"releases/x-container.yaml": VALID})
        clone = repo.clone("clone", depth=2)
        head = clone.git("rev-parse", "HEAD")
        with (clone.path / ".git" / "shallow").open("a") as shallow:
            shallow.write(head + "\n")
        self.assertEqual(clone.git("rev-list", "--count", "HEAD"), "1")
        run = run_action(clone)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "true")

    def test_unborn_head(self) -> None:
        repo = Repo(self.sandbox, self.sandbox.root / "unborn")
        repo.git("init", "-q", "-b", "main")
        run = run_action(repo)
        self.assertEqual(run.status, 1)
        self.assertIn("HEAD does not name a commit", errors(run)[0])


@unittest.skipUnless(shutil.which("yq"), "the action needs yq")
class VerifyTest(SandboxTestCase):
    """base: validating what a change adds, before it merges."""

    def branch(self) -> Repo:
        """A two-commit change: the release file, then an unrelated fix."""
        repo = self.sandbox.repo()
        repo.git("checkout", "-q", "-b", "change")
        repo.commit({"releases/x-container.yaml": VALID})
        repo.commit({"src/fix.txt": "fix\n"})
        return repo

    def test_base_sees_every_commit_of_the_change(self) -> None:
        repo = self.branch()
        without = run_action(repo)
        self.assertEqual(without.outputs["has_release"], "false")
        self.assertIn("No container release file in merged commit", without.stdout)
        run = run_action(repo, base="main")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "true")
        self.assertEqual(run.outputs["containers_json"], VALID_JSON)

    def test_base_uses_the_merge_base(self) -> None:
        # A release file merged to main after the change forked belongs
        # to main, not to the change.
        repo = self.sandbox.repo()
        repo.git("checkout", "-q", "-b", "change")
        repo.commit({"src/fix.txt": "fix\n"})
        repo.git("checkout", "-q", "main")
        repo.commit({"releases/x-container.yaml": VALID})
        repo.git("checkout", "-q", "change")
        run = run_action(repo, base="main")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "false")
        self.assertIn("No container release file in changes since main", run.stdout)

    def test_base_is_literal_in_the_summary(self) -> None:
        # '<' and '>' are valid in a ref name; the summary must not
        # render the caller's base as HTML.
        repo = self.sandbox.repo()
        repo.git("branch", "<details>")
        repo.git("checkout", "-q", "-b", "change")
        repo.commit({"src/fix.txt": "fix\n"})
        run = run_action(repo, base="<details>")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertIn("changes since &lt;details&gt;", run.summary)
        self.assertNotIn("<details>", run.summary)

    def test_base_validates_the_change(self) -> None:
        repo = self.sandbox.repo()
        repo.git("checkout", "-q", "-b", "change")
        repo.commit({"releases/x-container.yaml": container_file(tag='"-bad"')})
        repo.commit({"src/fix.txt": "fix\n"})
        run = run_action(repo, base="main")
        self.assertEqual(run.status, 1)
        self.assertIn("Invalid or missing container_release_tag", errors(run)[0])

    def test_base_by_commit(self) -> None:
        repo = self.branch()
        base = repo.git("rev-parse", "main")
        run = run_action(repo, base=base)
        self.assertEqual(run.outputs["has_release"], "true")

    def test_unknown_base(self) -> None:
        run = run_action(self.branch(), base="origin/nowhere")
        self.assertEqual(run.status, 1)
        self.assertIn("base 'origin/nowhere' does not name a commit", errors(run)[0])

    def test_base_option_is_refused(self) -> None:
        run = run_action(self.branch(), base="--output=/tmp/x")
        self.assertEqual(run.status, 1)
        self.assertIn("base must be a single git revision", errors(run)[0])

    def test_unrelated_base(self) -> None:
        repo = self.branch()
        repo.git("checkout", "-q", "--orphan", "other")
        repo.commit({"other.txt": "x\n"})
        repo.git("checkout", "-q", "change")
        run = run_action(repo, base="other")
        self.assertEqual(run.status, 1)
        self.assertIn("share no commit", errors(run)[0])

    def test_base_in_a_shallow_clone_without_the_fork_point(self) -> None:
        repo = self.branch()
        clone = repo.clone("clone", depth=1)
        clone.git("fetch", "-q", "--depth=1", "origin", "main:main")
        run = run_action(clone, base="main")
        self.assertEqual(run.status, 1)
        self.assertIn("fetch-depth: 0", errors(run)[0])


@unittest.skipUnless(shutil.which("yq"), "the action needs yq")
class InputsTest(SandboxTestCase):
    """Inputs, outputs and the step summary."""

    def release_repo(self, files: dict[str, str | None] | None = None) -> Repo:
        repo = self.sandbox.repo()
        repo.commit(files or {"releases/x-container.yaml": VALID})
        return repo

    def test_outputs_with_a_release(self) -> None:
        run = run_action(
            self.release_repo(
                {
                    "releases/x-container.yaml": VALID
                    + "container_push_registry: nexus3.example.org:10003/onap\n",
                    "releases/y-maven.yaml": MAVEN,
                }
            )
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            run.outputs,
            {
                "has_release": "true",
                "version": "1.2.3",
                "containers_json": VALID_JSON,
                "pull_registry": "",
                "push_registry": "nexus3.example.org:10003/onap",
                "release_file": "releases/x-container.yaml",
                "container_count": "2",
                "distribution_type": "container",
            },
        )

    def test_outputs_without_a_release(self) -> None:
        run = run_action(self.release_repo({"src/a.txt": "a\n"}))
        self.assertEqual(
            run.outputs,
            {
                "has_release": "false",
                "version": "",
                "containers_json": "",
                "pull_registry": "",
                "push_registry": "",
                "release_file": "",
                "container_count": "0",
                "distribution_type": "",
            },
        )

    def test_summary(self) -> None:
        run = run_action(
            self.release_repo(
                {"releases/x-container.yaml": VALID, "releases/y-maven.yaml": MAVEN}
            )
        )
        self.assertIn("## 🐳 Container release detection", run.summary)
        self.assertIn("| Release file | releases/x-container.yaml |", run.summary)
        self.assertIn("| onap/policy-pap | 4.0.1 |", run.summary)
        self.assertIn("| Pull registry | `snapshot_registry` input |", run.summary)
        self.assertIn("- releases/y-maven.yaml (maven)", run.summary)

    def test_summary_without_a_release(self) -> None:
        run = run_action(self.release_repo({"src/a.txt": "a\n"}))
        self.assertIn("No container release file in merged commit", run.summary)

    def test_summary_disabled(self) -> None:
        run = run_action(self.release_repo(), summary="false")
        self.assertEqual(run.status, 0)
        self.assertEqual(run.summary, "")

    def test_summary_invalid(self) -> None:
        run = run_action(self.release_repo(), summary="maybe")
        self.assertEqual(run.status, 1)
        self.assertIn("summary must be 'true' or 'false'", errors(run)[0])

    def test_path_input(self) -> None:
        repo = self.release_repo()
        run = run_action(repo, cwd=self.sandbox.root, path="repo")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "true")

    def test_path_in_a_subdirectory(self) -> None:
        repo = self.release_repo()
        run = run_action(repo, path="releases")
        self.assertEqual(run.status, 1)
        self.assertIn("top level of the repository", errors(run)[0])

    def test_path_missing(self) -> None:
        run = run_action(self.release_repo(), path="nowhere")
        self.assertEqual(run.status, 1)
        self.assertIn("path 'nowhere' is not a usable directory", errors(run)[0])

    def test_path_not_a_repository(self) -> None:
        (self.sandbox.root / "plain").mkdir()
        repo = Repo(self.sandbox, self.sandbox.root / "plain")
        run = run_action(repo)
        self.assertEqual(run.status, 1)
        self.assertIn("Not a git work tree", errors(run)[0])

    def test_notice_cannot_inject_commands(self) -> None:
        # A block mapping prints over several lines; each must stay in
        # the one notice rather than start a workflow command.
        hostile = "distribution_type:\n  a: 1\n  '::error::x': 2\n"
        run = run_action(self.release_repo({"releases/x.yaml": hostile}))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(len(run.annotations), 1)
        self.assertTrue(run.annotations[0].startswith("::notice::Ignoring"))
        self.assertIn("a: 1\\n", run.annotations[0])

    def test_error_cannot_inject_commands(self) -> None:
        tag = '"1.0\\n::warning::x"'
        run = run_action(self.release_repo({"releases/x.yaml": container_file(tag)}))
        self.assertEqual(run.status, 1)
        self.assertEqual(len(run.annotations), 1)
        self.assertIn("'1.0\\n::warning::x'", run.annotations[0])


class YqTest(SandboxTestCase):
    """The yq on PATH must be mikefarah yq v4."""

    def run_with(self, version_script: str, name: str = "fake") -> Run:
        repo = self.sandbox.repo(f"repo-{name}")
        repo.commit({"src/a.txt": "a\n"})
        fake = self.sandbox.bin_dir(name, {"yq": version_script})
        return run_action(repo, env_path=f"{fake}:{self.sandbox.without_yq()}")

    def test_python_yq_is_refused(self) -> None:
        run = self.run_with("#!/bin/sh\necho 'yq 3.4.3'\n")
        self.assertEqual(run.status, 1)
        self.assertIn("is not mikefarah yq v4", errors(run)[0])
        self.assertIn("reported: yq 3.4.3", errors(run)[0])

    def test_mikefarah_v3_is_refused(self) -> None:
        run = self.run_with("#!/bin/sh\necho 'yq version 3.4.1'\n")
        self.assertIn("is not mikefarah yq v4", errors(run)[0])

    def test_failing_yq_is_refused(self) -> None:
        run = self.run_with("#!/bin/sh\nexit 3\n")
        self.assertIn("reported: exit status 3", errors(run)[0])

    def test_minimum_v4_is_accepted(self) -> None:
        # Releases before 4.18 printed the version with no 'v'.
        run = self.run_with(
            "#!/bin/sh\necho 'yq (https://github.com/mikefarah/yq/) version 4.25.3'\n"
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["has_release"], "false")

    def test_older_or_newer_major_is_refused(self) -> None:
        # 4.25.2 and older lack -r and need the eval subcommand: run
        # against real binaries, the suite fails on them.
        for version in ("4.9.6", "4.25.2", "v5.0.0"):
            with self.subTest(version=version):
                run = self.run_with(
                    "#!/bin/sh\necho 'yq (https://github.com/mikefarah/yq/) "
                    f"version {version}'\n",
                    name=version,
                )
                self.assertEqual(run.status, 1)
                self.assertIn("v4.25.3 or a later v4", errors(run)[0])

    def test_missing_yq(self) -> None:
        repo = self.sandbox.repo()
        run = run_action(repo, env_path=self.sandbox.without_yq())
        self.assertIn("yq is not on PATH", errors(run)[0])


class RulesTest(unittest.TestCase):
    """The validation rules, without yq."""

    def test_repository_path_length(self) -> None:
        # The limit counts the path below the host: an input or override
        # path, a '/', then the container name.
        cases = [
            ("nexus3.example.org:10001", 255, None),
            ("nexus3.example.org:10001", 256, 256),
            ("nexus3.example.org:10001/onap", 250, None),
            ("nexus3.example.org:10001/onap", 251, 256),
        ]
        for registry, length, over in cases:
            with self.subTest(registry=registry, length=length):
                containers = [{"name": "a" * length, "version": "1"}]
                if over is None:
                    check_path_lengths((registry,), containers, "r.yaml")
                    continue
                with self.assertRaises(ActionError) as caught:
                    check_path_lengths((registry,), containers, "r.yaml")
                self.assertIn(f"is {over} characters", str(caught.exception))

    def test_markdown_cell_is_literal(self) -> None:
        # An ignored distribution_type is arbitrary repository text.
        cases = {
            "![x](https://example.invalid/i.png)": (
                "\\!\\[x\\](https://example.invalid/i.png)"
            ),
            "[label](https://example.invalid)": "\\[label\\](https://example.invalid)",
            "*a* _b_ ~c~": "\\*a\\* \\_b\\_ \\~c\\~",
            "a|b `c` d\\e": "a\\|b \\`c\\` d\\\\e",
            "<img src=x> & \n": "&lt;img src=x&gt; &amp; \\n",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(markdown_cell(text), expected)

    def test_jq_tostring(self) -> None:
        # Each expectation is what jq 1.7.1 printed for yq's JSON.
        cases = {
            "1": "1",
            "1.0": "1.0",
            "1.10": "1.10",
            "1e3": "1E+3",
            "1.5e+3": "1.5E+3",
            "0.10e1": "1.0",
            "0.1e-10": "1E-11",
            "100E-2": "1.00",
            "-0": "-0",
            "0e10": "0E+10",
            "100000000000000000000.0": "100000000000000000000.0",
            "1.0000000000000000001": "1.0000000000000000001",
        }
        for literal, expected in cases.items():
            with self.subTest(literal=literal):
                self.assertEqual(jq_tostring(literal), expected)

    def test_annotation_shows_controls(self) -> None:
        # CR and LF stay %-encoded so a multi-line message stays one
        # command; other controls, such as ESC or C1 CSI, become visible.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            annotate("error", "releases/\x1b[31mbad\x9b.yaml 5%\r\nnext")
        self.assertEqual(
            out.getvalue(),
            "::error::releases/\\x1b[31mbad\\x9b.yaml 5%25%0D%0Anext\n",
        )

    def test_visible_escapes_controls(self) -> None:
        cases = {
            "a\nb\rc": "a\\nb\\rc",
            "\x00\x1b\x7f": "\\x00\\x1b\\x7f",
            "\x80\x85\x9b\x9f": "\\x80\\x85\\x9b\\x9f",
            "\u2028\u2029": "\\u2028\\u2029",
            "\xa0é1.0": "\xa0é1.0",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(visible(text), expected)

    def test_parse_containers_refuses_an_unrenderable_number(self) -> None:
        # Decimal's exponent is bounded; jq's tostring has no answer.
        text = '[{"name":"a","version":0e999999999999999999999999}]'
        with self.assertRaises(ActionError) as caught:
            parse_containers(text, "r")
        self.assertIn("entry 1 (a): version", str(caught.exception))

    def test_registry_host(self) -> None:
        cases = {
            "nexus3.example.org": "nexus3.example.org",
            "nexus3.example.org:10001": "nexus3.example.org",
            "nexus3.example.org/onap": "nexus3.example.org",
            "nexus3.example.org:443/a/b": "nexus3.example.org",
            "": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(registry_host(value), expected)

    def test_override_rules(self) -> None:
        allowed = "nexus3.example.org:10001"
        check_override("", allowed, "f", "snapshot_registry", "r")
        check_override("nexus3.example.org:10003/onap", allowed, "f", "i", "r")
        for bad in ("evil.org", "nexus3.example.org.evil.org", "a b", "x/Y"):
            with self.subTest(value=bad), self.assertRaises(ActionError):
                check_override(bad, allowed, "f", "i", "r")

    def test_override_hint_names_the_empty_input(self) -> None:
        with self.assertRaises(ActionError) as caught:
            check_override("nexus3.example.org", "", "f", "release_registry", "r")
        self.assertIn("the release_registry input is empty", str(caught.exception))

    def test_override_needs_a_well_formed_input(self) -> None:
        # With a scheme, the input's host would read as 'https', which a
        # release file's 'https:443/x' override would then match.
        for allowed in ("https://nexus3.example.org", "nexus3.example.org/"):
            with self.subTest(allowed=allowed):
                with self.assertRaises(ActionError) as caught:
                    check_override(
                        "https:443/x", allowed, "f", "snapshot_registry", "r"
                    )
                self.assertIn("The snapshot_registry input", str(caught.exception))

    def test_tag_rules(self) -> None:
        for good in ("1", "v1.2.3", "_x", "A" * 128, "1.0-rc.1_b"):
            check_tag(good, "r")
        for bad in ("", "-1", ".1", "1+b", "A" * 129, "1.0\n", "é"):
            with self.subTest(tag=bad), self.assertRaises(ActionError):
                check_tag(bad, "r")

    def test_parse_containers_normalises(self) -> None:
        text = '[{"version":1.10,"name":"a","x":1},{"name":"b/c","version":"2"}]'
        self.assertEqual(
            json.dumps(parse_containers(text, "r"), separators=(",", ":")),
            '[{"name":"a","version":"1.10"},{"name":"b/c","version":"2"}]',
        )

    def test_parse_containers_refuses(self) -> None:
        for text in ("", "nope", "{}", "[]", '[{"name":"a","version":NaN}]', "[1]"):
            with self.subTest(text=text), self.assertRaises(ActionError):
                parse_containers(text, "r")


if __name__ == "__main__":
    unittest.main()
