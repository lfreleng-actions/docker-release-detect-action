# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read release files with mikefarah yq, through the lane's expressions.

The lane read every field with yq v4, and so does this module, with the
same expressions. YAML typing (is 1.10 a number? is yes a string?),
anchors, merge keys and scalar styles therefore resolve exactly as they
did; a second YAML parser could only disagree.

Scalars come back as bash's $(...) captured them: NUL bytes dropped and
trailing newlines stripped, so a block scalar reads as the lane read it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from scripts.gha import ActionError

YQ_URL = "https://github.com/mikefarah/yq"
# mikefarah yq prints 'yq (https://github.com/mikefarah/yq/) version
# v4.44.3'; releases before 4.18 omitted the 'v'.
_VERSION = re.compile(r"mikefarah/yq/?\)? version v?(\d+)\.(\d+)\.(\d+)")
# The first v4 with the root command (no 'eval') and the -r short flag
# the lane's commands use; the suite passes on it and fails on 4.25.2.
MINIMUM = (4, 25, 3)


def parse_version(first_line: str) -> tuple[int, ...] | None:
    """The version in the first line of 'yq --version', if mikefarah's."""
    match = _VERSION.search(first_line)
    return tuple(int(part) for part in match.groups()) if match else None


def check_yq() -> None:
    """Fail unless the yq on PATH is mikefarah yq v4.25.3 or newer v4.

    The Python yq (kislyuk/yq), a jq wrapper, takes the same name and
    reads these expressions differently, so it is refused by name.
    """
    if shutil.which("yq") is None:
        raise ActionError(
            f"yq is not on PATH. This action reads release files with "
            f"mikefarah yq v4 ({YQ_URL}), which GitHub-hosted runners "
            f"provide; install it on a self-hosted runner"
        )
    proc = subprocess.run(
        ["yq", "--version"], capture_output=True, text=True, check=False
    )
    found = (proc.stdout or proc.stderr).strip().splitlines()
    first = found[0] if found else f"exit status {proc.returncode}"
    version = parse_version(first) if proc.returncode == 0 else None
    if version is None or version[0] != 4 or version < MINIMUM:
        raise ActionError(
            f"The yq on PATH is not mikefarah yq v4.25.3 or a later v4 "
            f"({YQ_URL}), which this action needs to read release files; "
            f"'yq --version' reported: {first}"
        )


def _run(path: str, expression: str, *flags: str) -> str:
    proc = subprocess.run(
        ["yq", *flags, expression, path], capture_output=True, check=False
    )
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = lines[0] if lines else f"yq exit status {proc.returncode}"
        raise ActionError(f"Cannot read {path} as YAML: {reason}")
    return proc.stdout.decode("utf-8", "replace")


def scalar(path: str, field: str) -> str:
    """The lane's ``yq -r '.<field> // ""'``, as ``$(...)`` captured it."""
    raw = _run(path, f'.{field} // ""', "-r")
    return raw.replace("\0", "").rstrip("\n")


def document_count(path: str) -> int:
    """How many YAML documents the file holds; an empty file holds one."""
    raw = _run(path, "[.] | length", "eval-all", "-r")
    try:
        return int(raw.strip())
    except ValueError as error:
        raise ActionError(f"Cannot count the YAML documents in {path}") from error


def containers_json(path: str) -> str:
    """The lane's ``yq -o=json -I=0 '.containers // []'`` output."""
    return _run(path, ".containers // []", "-o=json", "-I=0").rstrip("\n")


# [tag, style, text] for a value. A number retagged !!str comes out as
# its source text in every v4 release, where its JSON form varies.
# explode() first, so that an alias reports the anchored value.
_NODE = (
    '[tag, style, (select(tag == "!!int" or tag == "!!float") |= (. tag = "!!str"))]'
)


def _json(path: str, expression: str) -> Any:
    raw = _run(path, expression, "-o=json", "-I=0")
    try:
        return json.loads(raw)
    except ValueError as error:
        raise ActionError(f"Cannot read {path} as YAML: yq returned no JSON") from error


def node(path: str, field: str) -> Any:
    """yq's [tag, style, text] for a top-level field."""
    return _json(path, f".{field} | explode(.) | {_NODE}")


def version_nodes(path: str) -> list[Any]:
    """yq's [tag, style, text] for each containers entry's version.

    An entry that is not a mapping yields a placeholder, so indices
    match the entries'. Call only once containers is known to be a list.
    """
    found = _json(
        path,
        "[(.containers // []) | explode(.) | .[] | "
        f'((select(tag == "!!map") | .version | {_NODE}) // null)]',
    )
    return found if isinstance(found, list) else []
