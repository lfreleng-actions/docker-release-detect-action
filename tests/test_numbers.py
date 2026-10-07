# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The numeric_versions input: literal, yaml and refuse readings."""

from __future__ import annotations

import json
import shutil
import unittest

from scripts.gha import ActionError
from scripts.numbers import (
    Node,
    jenkins_number,
    jenkins_text,
    literal_text,
    resolve,
)
from scripts.release import parse_containers
from tests.support import Run, SandboxTestCase, container_file, run_action

# The flow-sequence entries of one release file, and what each mode
# outputs for them. The yaml column is what the Python yq 4.4.0 that
# global-jjb installs printed, through jq, for the same file.
NUMBERS = """\
  - {name: a, version: 1.10}
  - {name: b, version: "1.10"}
  - {name: c, version: 2.0}
  - {name: d, version: 7}
  - {name: e, version: 1_000}
  - {name: f, version: *anchor}
  - {name: g, version: !!float "2"}
  - {name: h, version: '010'}
"""
LITERAL = ["1.10", "1.10", "2.0", "7", "1000", "1.10", "2", "010"]
YAML = ["1.1", "1.10", "2.0", "7", "1_000", "1.1", "2.0", "010"]


def numbers_file(tag: str = "2.0") -> str:
    return "anchored: &anchor 1.10\n" + container_file(tag, NUMBERS)


def versions(run: Run) -> list[str]:
    return [entry["version"] for entry in json.loads(run.outputs["containers_json"])]


class JenkinsRenderingTest(unittest.TestCase):
    """Plain scalars as the Python yq (YAML 1.2 resolvers) and jq print them."""

    def test_plain_scalars(self) -> None:
        # Each expectation is what 'yq -c' (kislyuk/yq 4.4.0, jq 1.8)
        # printed for the plain scalar; None where it printed a string.
        cases = {
            "7": "7",
            "1.0": "1.0",
            "1.10": "1.1",
            "2.0": "2.0",
            "010": "10",
            "08": "8",
            "0x1F": "31",
            "0o17": "15",
            "1e3": "1000.0",
            "1.0e3": "1000.0",
            "0.10e1": "1.0",
            "-1": "-1",
            "+1": "1",
            ".5": "0.5",
            "1.": "1.0",
            "-0": "0",
            "-0.0": "-0.0",
            "0.1e-10": "1E-11",
            "1.0e16": "1E+16",
            "123456789.123456789": "123456789.12345679",
            "100000000000000000001": "100000000000000000001",
            "0b101": None,
            "1_000": None,
            "1:30": None,
            "+0x1F": None,
            "1.2.3": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                number = jenkins_number(Node("!!str", "", text), "f")
                rendered = None if number is None else jenkins_text(number)
                self.assertEqual(rendered, expected)

    def test_quoted_and_tagged(self) -> None:
        self.assertIsNone(jenkins_number(Node("!!str", "double", "1.10"), "f"))
        self.assertIsNone(jenkins_number(Node("!!str", "tagged", "1.10"), "f"))
        self.assertEqual(jenkins_number(Node("!!float", "tagged", "7"), "f"), 7.0)
        self.assertEqual(jenkins_number(Node("!!int", "tagged", "010"), "f"), 10)
        with self.assertRaises(ActionError):
            jenkins_number(Node("!!int", "tagged", "1.5"), "f")

    def test_overlong_integers_fail_as_jenkins_does(self) -> None:
        # Jenkins' Python yq stops at Python's 4300-digit int() limit,
        # reading a long decimal or printing a long hex number.
        for text in ("1" * 5000, "0x" + "f" * 4000):
            with self.subTest(digits=len(text)), self.assertRaises(ActionError):
                resolve(Node("!!int", "", text), "yaml", text, "f")

    def test_tagged_numbers_use_the_constructors(self) -> None:
        # What 'yq -c' (kislyuk/yq 4.4.0) printed for each '!!tag "text"';
        # None where it failed to read the file.
        cases = {
            ("!!int", "+0x1F"): "31",
            ("!!int", "-0x1F"): "-31",
            ("!!int", "1_000"): "1000",
            ("!!int", "+1_0"): "10",
            ("!!int", "0x_1F"): "31",
            ("!!int", "0o1_7"): "15",
            ("!!int", "08"): "8",
            ("!!int", "0b101"): None,
            ("!!int", "1:30"): None,
            ("!!int", "0o8"): None,
            ("!!int", "1.5"): None,
            ("!!float", "1_000.5"): "1000.5",
            ("!!float", "1:30"): "90.0",
            ("!!float", "1_0"): "10.0",
            ("!!float", "1e3_0"): "1E+30",
            ("!!float", "-.5_0"): "-0.5",
            ("!!float", "+1."): "1.0",
            ("!!float", "1.10"): "1.1",
            ("!!float", "0x1F"): None,
            ("!!float", "abc"): None,
        }
        for (tag, text), expected in cases.items():
            with self.subTest(tag=tag, text=text):
                node = Node(tag, "double", text)
                if expected is None:
                    with self.assertRaises(ActionError):
                        jenkins_number(node, "f")
                else:
                    number = jenkins_number(node, "f")
                    assert number is not None
                    self.assertEqual(jenkins_text(number), expected)


class LiteralTest(unittest.TestCase):
    """literal takes the source text, not the yq release's JSON number."""

    def test_overlong_integer_text_keeps_yqs_conversion(self) -> None:
        # Past Python's 4300-digit int() limit, never parsed as an int.
        node = Node("!!int", "", "1" * 5000)
        self.assertEqual(literal_text(node, "1e5000"), "1E+5000")

    def test_old_yq_json_is_replaced_by_the_text(self) -> None:
        # yq before 4.53.3 writes 1.10 as 1.1 and 2.0 as 2.
        cases = {("1.10", "1.1"): "1.10", ("2.0", "2"): "2.0", ("1e3", "1000"): "1E+3"}
        for (text, old_json), expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    literal_text(Node("!!float", "", text), old_json), expected
                )

    def test_spellings_yq_converts_keep_its_conversion(self) -> None:
        # As in the lane on yq 4.53.6: 010 is 10, and an integer past
        # int64 becomes a float.
        cases = {
            ("010", "10"): "10",
            ("0x1F", "31"): "31",
            ("100000000000000000001", "100000000000000000000.0"): (
                "100000000000000000000.0"
            ),
        }
        for (text, json_number), expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    literal_text(Node("!!int", "", text), json_number), expected
                )

    def test_parse_containers_uses_the_nodes(self) -> None:
        old_yq = '[{"name":"a","version":1.1},{"name":"b","version":2}]'
        nodes = [["!!float", "", "1.10"], ["!!float", "", "2.0"]]
        parsed = parse_containers(old_yq, "r", "literal", lambda: nodes)
        self.assertEqual([c["version"] for c in parsed], ["1.10", "2.0"])


@unittest.skipUnless(shutil.which("yq"), "the action needs yq")
class NumericVersionsTest(SandboxTestCase):
    """Each mode end to end, through the real yq."""

    def run_mode(self, content: str, mode: str | None) -> Run:
        self.runs = getattr(self, "runs", 0) + 1
        repo = self.sandbox.repo(f"repo{self.runs}")
        repo.commit({"releases/x-container.yaml": content})
        if mode is None:
            return run_action(repo)
        return run_action(repo, numeric_versions=mode)

    def test_literal_is_the_default(self) -> None:
        for mode in (None, "", "literal"):
            with self.subTest(mode=mode):
                run = self.run_mode(numbers_file(), mode)
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(run.outputs["version"], "2.0")
                self.assertEqual(versions(run), LITERAL)

    def test_yaml_renders_as_jenkins(self) -> None:
        run = self.run_mode(numbers_file(), "yaml")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(versions(run), YAML)

    def test_yaml_renders_the_tag_as_jenkins(self) -> None:
        cases = {
            "1.10": "1.1",
            '"1.10"': "1.10",
            "1_000": "1_000",
            "v1": "v1",
            '!!int "+0x1F"': "31",
            '!!float "1_000.5"': "1000.5",
        }
        for tag, expected in cases.items():
            with self.subTest(tag=tag):
                run = self.run_mode(container_file(tag), "yaml")
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(run.outputs["version"], expected)

    def test_yaml_refuses_an_integer_jenkins_cannot_read(self) -> None:
        run = self.run_mode(container_file("1" * 5000), "yaml")
        self.assertEqual(run.status, 1, run.stdout[:300])
        self.assertEqual(run.outputs, {})
        self.assertIn("which Jenkins cannot read as a number", run.annotations[0])

    def test_yaml_renders_tagged_versions_as_jenkins(self) -> None:
        content = container_file(
            "v1",
            '  - {name: a, version: !!int "1_000"}\n  - {name: b, version: !!int 07}\n',
        )
        run = self.run_mode(content, "yaml")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(versions(run), ["1000", "7"])

    def test_refuse_fails_on_a_number_tag(self) -> None:
        run = self.run_mode(container_file("2.0"), "refuse")
        self.assertEqual(run.status, 1)
        self.assertEqual(run.outputs, {})
        self.assertIn(
            "::error::container_release_tag in releases/x-container.yaml is the "
            "unquoted YAML number 2.0",
            run.annotations[0],
        )
        self.assertIn('write it as a quoted string, "2.0"', run.annotations[0])

    def test_refuse_fails_on_a_number_version(self) -> None:
        cases = {
            "1.10": ("the unquoted YAML number 1.10", 'a quoted string, "1.10"'),
            "7": ("the unquoted YAML number 7", 'a quoted string, "7"'),
            "*anchor": ("the unquoted YAML number 1.10", 'a quoted string, "1.10"'),
            "!!float '2'": (
                "tagged !!float, which makes 2 a YAML number",
                'drop the tag and write it as a quoted string, "2"',
            ),
        }
        for value, (problem, remedy) in cases.items():
            with self.subTest(value=value):
                content = "anchored: &anchor 1.10\n" + container_file(
                    "'2.0'", f"  - {{name: app, version: {value}}}\n"
                )
                run = self.run_mode(content, "refuse")
                self.assertEqual(run.status, 1, run.stdout)
                self.assertEqual(run.outputs, {})
                self.assertIn(
                    "The version of containers entry 1 (app) in "
                    f"releases/x-container.yaml is {problem}, which",
                    run.annotations[0],
                )
                self.assertIn(remedy, run.annotations[0])

    def test_refuse_accepts_quoted_values(self) -> None:
        content = container_file(
            '"2.0"', '  - {name: a, version: "1.10"}\n  - {name: b, version: v1}\n'
        )
        run = self.run_mode(content, "refuse")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["version"], "2.0")
        self.assertEqual(versions(run), ["1.10", "v1"])

    def test_invalid_mode_fails(self) -> None:
        for mode in ("strict", "Literal", "yaml,refuse"):
            with self.subTest(mode=mode):
                run = self.run_mode(numbers_file(), mode)
                self.assertEqual(run.status, 1)
                self.assertEqual(run.outputs, {})
                self.assertIn(
                    "numeric_versions must be one of literal, yaml, refuse",
                    run.annotations[0],
                )


if __name__ == "__main__":
    unittest.main()
