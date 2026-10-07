# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared helpers: scratch git repositories and both runners.

Every run uses the real git and yq on PATH, and the lane body the real
jq, as the runner does. Git reads no global or system configuration, so
a developer's settings (signing, core.quotePath) cannot leak in.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.yq import parse_version

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "legacy" / "detect-release-merge.sh"

SNAPSHOT = "nexus3.example.org:10001"
RELEASE = "nexus3.example.org:10002"

# A release file the lane accepts, as the LF container schema has it.
VALID = """\
distribution_type: container
project: example
container_release_tag: 1.2.3
ref: 0123456789abcdef0123456789abcdef01234567
containers:
  - name: policy-api
    version: 1.2.3-STAGING-20260101T000000Z
  - name: onap/policy-pap
    version: "4.0.1"
"""

MAVEN = """\
distribution_type: maven
project: example
version: 1.2.3
log_dir: example-maven-stage-master/1/
"""


def container_file(
    tag: str = "1.2.3",
    containers: str = "  - name: app\n    version: 1.0.0\n",
    extra: str = "",
) -> str:
    """A container release file with the given parts."""
    return (
        "distribution_type: container\n"
        f"container_release_tag: {tag}\n"
        f"containers:\n{containers}{extra}"
    )


@dataclass
class Run:
    """The observable result of one invocation."""

    status: int
    outputs: dict[str, str]
    annotations: list[str]
    stdout: str
    summary: str


def parse_outputs(text: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT in both the ``k=v`` and heredoc forms."""
    outputs: dict[str, str] = {}
    lines = iter(text.split("\n"))
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            for item in lines:
                if item == delimiter:
                    break
                body.append(item)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


class Sandbox:
    """A scratch directory with an isolated git configuration."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        config = root / "gitconfig"
        config.write_text("")
        self.env = {
            "HOME": str(root),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.org",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.org",
        }
        self.path = os.environ.get("PATH", "")
        self._runs = 0

    def bin_dir(self, name: str, tools: Mapping[str, str]) -> str:
        """A directory holding only ``tools`` (name -> script body)."""
        directory = self.root / name
        directory.mkdir()
        for tool, body in tools.items():
            script = directory / tool
            script.write_text(body)
            script.chmod(0o755)
        return str(directory)

    def without_yq(self) -> str:
        """A PATH holding git and bash alone, so no yq is found."""
        directory = self.root / "no-yq"
        if directory.exists():
            return str(directory)
        directory.mkdir()
        for tool in ("git", "bash"):
            found = shutil.which(tool)
            assert found, f"{tool} is required"
            (directory / tool).symlink_to(found)
        return str(directory)

    def repo(self, name: str = "repo") -> Repo:
        """A new repository with one ordinary commit on main."""
        repo = Repo(self, self.root / name)
        repo.git("init", "-q", "-b", "main")
        repo.commit({"README.md": "# Example\n"}, "Initial commit")
        return repo

    def invoke(
        self, command: list[str], cwd: pathlib.Path, env: Mapping[str, str]
    ) -> Run:
        """Run ``command`` as a step would, capturing its outputs."""
        self._runs += 1
        scratch = self.root / f"run-{self._runs}"
        scratch.mkdir()
        output, summary = scratch / "output", scratch / "summary"
        output.touch()
        summary.touch()
        full_env = {
            "PATH": self.path,
            **self.env,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            **env,
        }
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=full_env,
            capture_output=True,
            check=False,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        return Run(
            status=proc.returncode,
            outputs=parse_outputs(output.read_text(encoding="utf-8")),
            annotations=[line for line in stdout.splitlines() if line.startswith("::")],
            stdout=stdout + proc.stderr.decode("utf-8", "replace"),
            summary=summary.read_text(encoding="utf-8"),
        )


class Repo:
    """A git repository in the sandbox."""

    def __init__(self, sandbox: Sandbox, path: pathlib.Path) -> None:
        self.sandbox = sandbox
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def git(self, *args: str) -> str:
        """Run git here and return its stripped output."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={"PATH": self.sandbox.path, **self.sandbox.env},
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc.stdout.decode("utf-8", "replace").strip()

    def write(self, files: Mapping[str, str | None]) -> None:
        """Write files (relative path -> content); None deletes."""
        for relative, content in files.items():
            path = self.path / relative
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))

    def commit(self, files: Mapping[str, str | None], message: str = "Change") -> str:
        """Commit ``files`` (see write) and return the new HEAD."""
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch: str, message: str = "Merge") -> str:
        """Merge ``branch`` into the current branch with a merge commit."""
        self.git("merge", "-q", "--no-ff", "--no-edit", "-m", message, branch)
        return self.git("rev-parse", "HEAD")

    def clone(self, name: str, depth: int) -> Repo:
        """A shallow clone, as actions/checkout makes with fetch-depth."""
        clone = Repo(self.sandbox, self.sandbox.root / name)
        clone.git(
            "clone", "-q", f"--depth={depth}", self.path.as_uri(), str(clone.path)
        )
        return clone


def run_legacy(
    repo: Repo,
    snapshot: str = SNAPSHOT,
    release: str = RELEASE,
    env_path: str | None = None,
) -> Run:
    """Run the lane's vendored step body, as the runner does."""
    env = {"SNAPSHOT_REGISTRY": snapshot, "RELEASE_REGISTRY": release}
    if env_path is not None:
        env["PATH"] = env_path
    return repo.sandbox.invoke(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(LEGACY)],
        repo.path,
        env,
    )


def run_action(
    repo: Repo,
    *,
    cwd: pathlib.Path | None = None,
    env_path: str | None = None,
    **inputs: str,
) -> Run:
    """Run the action's entry point exactly as action.yaml does.

    ``inputs`` become INPUT_* variables; the registries default to the
    lane fixtures' values, and ``env_path`` replaces PATH.
    """
    env = {f"INPUT_{key.upper()}": value for key, value in inputs.items()}
    env.setdefault("INPUT_SNAPSHOT_REGISTRY", SNAPSHOT)
    env.setdefault("INPUT_RELEASE_REGISTRY", RELEASE)
    if env_path is not None:
        env["PATH"] = env_path
    return repo.sandbox.invoke(
        [sys.executable, "-I", str(ROOT / "entrypoint.py")],
        cwd or repo.path,
        env,
    )


def jq_version() -> tuple[int, ...]:
    """The jq on PATH as a version tuple; empty when absent."""
    if shutil.which("jq") is None:
        return ()
    proc = subprocess.run(["jq", "--version"], capture_output=True, text=True)
    text = proc.stdout.strip().removeprefix("jq-")
    try:
        return tuple(int(part) for part in text.split(".")[:2])
    except ValueError:
        return ()


def yq_version() -> tuple[int, ...]:
    """The mikefarah yq on PATH as a version tuple; empty when absent."""
    if shutil.which("yq") is None:
        return ()
    proc = subprocess.run(["yq", "--version"], capture_output=True, text=True)
    return parse_version(proc.stdout) or ()


class SandboxTestCase(unittest.TestCase):
    """A test case with a fresh sandbox per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="release-detect-")
        self.sandbox = Sandbox(pathlib.Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()
