# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The labelled release file corpus: this action's verdict on each.

tests/fixtures/releases/expected.json records, for every fixture, the
container schema's verdict and this action's verdict with the registry
inputs it runs with. This suite asserts the action's verdicts offline;
the schema's are asserted in CI with the real validator.
"""

from __future__ import annotations

import json
import shutil
import unittest
from typing import Any

from tests.support import ROOT, SandboxTestCase, run_action

FIXTURES = ROOT / "tests" / "fixtures" / "releases"
VERDICTS = {"accept", "reject"}


def load_expected() -> list[dict[str, Any]]:
    """The fixtures listed in expected.json."""
    data = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    return data["fixtures"]


class CorpusTest(unittest.TestCase):
    """expected.json and the fixture README describe the whole corpus."""

    def test_every_fixture_is_labelled_once(self) -> None:
        listed = [entry["file"] for entry in load_expected()]
        self.assertEqual(len(listed), len(set(listed)))
        on_disk = sorted(path.name for path in FIXTURES.glob("*.yaml"))
        self.assertEqual(sorted(listed), on_disk)

    def test_disagreements_carry_a_reason(self) -> None:
        for entry in load_expected():
            with self.subTest(fixture=entry["file"]):
                self.assertIn(entry["schema"], VERDICTS)
                self.assertTrue(entry["action"])
                verdicts = {run["verdict"] for run in entry["action"]}
                self.assertLessEqual(verdicts, VERDICTS)
                if verdicts == {entry["schema"]}:
                    self.assertIsNone(entry["reason"])
                else:
                    self.assertTrue(entry["reason"])

    def test_readme_cites_every_fixture(self) -> None:
        readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
        for entry in load_expected():
            with self.subTest(fixture=entry["file"]):
                self.assertIn(f"[`{entry['file']}`]", readme)


@unittest.skipUnless(shutil.which("yq"), "the action needs yq")
class VerdictTest(SandboxTestCase):
    """Each fixture, committed as a release, meets its action verdict."""

    def test_action_verdicts(self) -> None:
        for entry in load_expected():
            content = (FIXTURES / entry["file"]).read_text(encoding="utf-8")
            repo = self.sandbox.repo(entry["file"].removesuffix(".yaml"))
            repo.commit({f"releases/{entry['file']}": content})
            for run_inputs in entry["action"]:
                with self.subTest(
                    fixture=entry["file"],
                    snapshot_registry=run_inputs["snapshot_registry"],
                    release_registry=run_inputs["release_registry"],
                ):
                    run = run_action(
                        repo,
                        snapshot_registry=run_inputs["snapshot_registry"],
                        release_registry=run_inputs["release_registry"],
                    )
                    if run_inputs["verdict"] == "accept":
                        self.assertEqual(run.status, 0, run.stdout)
                        self.assertEqual(run.outputs["has_release"], "true")
                    else:
                        # The error names the rule, so a fixture cannot
                        # pass by failing for an unrelated reason.
                        self.assertEqual(run.status, 1, run.stdout)
                        failures = [
                            line
                            for line in run.annotations
                            if line.startswith("::error::")
                        ]
                        self.assertEqual(len(failures), 1, run.stdout)
                        self.assertIn(run_inputs["error"], failures[0])


if __name__ == "__main__":
    unittest.main()
