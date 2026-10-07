# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub Actions runner I/O: inputs, outputs, annotations and summary."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class ActionError(Exception):
    """A failure reported as an error annotation before a non-zero exit."""


def _escape_data(value: str) -> str:
    # Workflow commands end at a newline, so a multi-line message would
    # otherwise leak its tail into the log as plain text. Other controls,
    # such as a terminal escape in a file name, are shown visibly.
    value = "".join(char if char in "\r\n" else _visible(char) for char in value)
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotate(level: str, message: str) -> None:
    """Emit a workflow command annotation (error, warning or notice)."""
    print(f"::{level}::{_escape_data(message)}", flush=True)


def log(message: str) -> None:
    """Print a plain log line that cannot carry a workflow command.

    The runner executes '::command::' at the start of any line, so a
    repository-controlled value holding a line break could smuggle one
    in. Line breaks and other control characters are therefore shown as
    visible escapes.
    """
    print(visible(message), flush=True)


def visible(text: str) -> str:
    """``text`` with line breaks and control characters as escapes."""
    return "".join(_visible(char) for char in text)


def _visible(char: str) -> str:
    if char == "\n":
        return "\\n"
    if char == "\r":
        return "\\r"
    if char < " " or "\x7f" <= char <= "\x9f" or char in "\u2028\u2029":
        return f"\\x{ord(char):02x}" if ord(char) < 0x100 else f"\\u{ord(char):04x}"
    return char


# Characters that open inline Markdown: code, emphasis, strikethrough,
# links and images, and the '|' that would end a table cell.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~\[\]!|])")


def markdown_cell(text: str) -> str:
    """Text safe inside one Markdown table cell of the step summary.

    Repository-controlled values must render as literal text: '|' would
    end the cell, a line break the table, '<' could open HTML and
    '[' a link or remote image. Each is escaped, as are the other
    inline markers and control characters.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return visible(_MARKDOWN_SPECIAL.sub(r"\\\1", escaped))


def env(name: str, default: str = "") -> str:
    """Read an environment variable, treating unset as the default."""
    return os.environ.get(name, default)


def env_bool(name: str, label: str, default: bool) -> bool:
    """Parse a boolean input, rejecting anything ambiguous."""
    raw = env(name).strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ActionError(f"{label} must be 'true' or 'false', got '{raw}'")


def set_outputs(values: Mapping[str, str]) -> None:
    """Append step outputs using the multi-line heredoc form.

    Each value gets a random delimiter, regenerated until absent from
    the value, so no value can inject further keys into GITHUB_OUTPUT.
    """
    path = env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            delimiter = f"ghadelim_{secrets.token_hex(16)}"
            while delimiter in value:
                delimiter = f"ghadelim_{secrets.token_hex(16)}"
            handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def append_summary(text: str) -> None:
    """Append Markdown to the job step summary, when one is available."""
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)
