# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What an unquoted YAML number in a release file becomes.

The numeric_versions input picks one of three readings for
container_release_tag and every containers[].version:

* literal: the text as written. yq 4.53.3 and later keep a JSON-syntax
  number's literal in their JSON output, older v4 releases decode it
  through float64 (1.10 becomes 1.1), so the text comes from yq's tag
  operator and a retag to !!str instead, which every v4 release
  answers the same way.
* yaml: the value Jenkins promotes. global-jjb's release job reads the
  file with the Python yq (kislyuk/yq, installed unpinned), whose
  default YAML 1.2 resolvers are restated here, and prints the value
  through jq.
* refuse: any value either reader takes as a number fails the run.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from scripts.gha import ActionError, visible

MODES = ("literal", "yaml", "refuse")
NUMBER_TAGS = ("!!int", "!!float")

# A JSON number: the literals yq 4.53.3 and later write unchanged,
# except integers beyond int64, which they write as floats.
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?")
_INT64 = range(-(2**63), 2**63)
# The Python yq's YAML 1.2 resolvers (yq/loader.py, core_resolvers):
# no '_' separators, no 0b, no sexagesimal 1:30, and 010 is decimal.
_JENKINS_INT = re.compile(r"0o[0-7]+|[-+]?[0-9]+|0x[0-9a-fA-F]+")
_JENKINS_FLOAT = re.compile(
    r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
)
_JENKINS_NON_FINITE = re.compile(r"[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)")


def jq_tostring(literal: str) -> str:
    """What jq 1.7's ``tostring`` makes of a JSON number literal.

    jq 1.7 keeps a parsed literal as a decNumber and prints it with the
    General Decimal Arithmetic to-scientific-string conversion, which
    Python's Decimal implements to the same specification: 1.10 stays
    1.10 and 1e3 becomes 1E+3. jq 1.6 printed doubles instead (1.10
    became 1.1), so the lane's result depended on the runner image;
    this pins the behaviour of jq 1.7, which ubuntu-24.04 ships.
    """
    return str(Decimal(literal))


@dataclass(frozen=True)
class Node:
    """A value as yq describes it: tag, style, and text for a scalar."""

    tag: str
    style: str
    text: Any

    @classmethod
    def from_yq(cls, raw: Any) -> Node | None:
        """A Node from yq's [tag, style, text] triple, if it is one."""
        if isinstance(raw, list) and len(raw) == 3:
            tag, style, text = raw
            if isinstance(tag, str) and isinstance(style, str):
                return cls(tag, style, text)
        return None


def literal_text(node: Node, yq_literal: str) -> str:
    """A number version, as the lane rendered it with yq 4.53.3 or later.

    The node's own text replaces the JSON literal yq wrote wherever
    that release wrote the text unchanged; other spellings (010, 0x1F,
    1_000, +1, .5) keep yq's conversion, as in the lane.
    """
    text = node.text
    if isinstance(text, str):
        match = _JSON_NUMBER.fullmatch(text)
        # The length test keeps int() off texts past its digit limit.
        if match and (
            match.group(1)
            or match.group(2)
            or (len(text) <= 20 and int(text) in _INT64)
        ):
            return jq_tostring(text)
    return jq_tostring(yq_literal)


def _signed(text: str) -> tuple[int, str]:
    """Both constructors' first steps: drop '_', then take one sign."""
    value = text.replace("_", "")
    if not value:
        raise ValueError("empty")
    sign = -1 if value[0] == "-" else 1
    return sign, value[1:] if value[0] in "+-" else value


def _construct_int(text: str) -> int:
    """The Python yq's construct_yaml_1_2_int, restated."""
    sign, value = _signed(text)
    if value.startswith(("0o", "0x")):
        return sign * int(value, 0)
    return sign * int(value, 10)


def _construct_float(text: str) -> float:
    """PyYAML's SafeConstructor.construct_yaml_float, restated."""
    sign, value = _signed(text.lower())
    if value == ".inf":
        return sign * math.inf
    if value == ".nan":
        return math.nan
    if ":" in value:
        number = 0.0
        for power, digit in enumerate(reversed(value.split(":"))):
            number += float(digit) * 60**power
        return sign * number
    return sign * float(value)


def jenkins_number(node: Node, where: str) -> int | float | None:
    """The number the Python yq reads, or None where it reads text.

    A plain scalar takes its type from the resolvers, an explicitly
    tagged one from its tag; either way the type's constructor reads
    the text. A quoted or block scalar without a number tag is text.
    """
    text = node.text
    if not isinstance(text, str):
        return None
    if node.style == "":
        if _JENKINS_INT.fullmatch(text):
            construct: Callable[[str], int | float] = _construct_int
        elif _JENKINS_FLOAT.fullmatch(text) or _JENKINS_NON_FINITE.fullmatch(text):
            construct = _construct_float
        else:
            return None
    elif node.tag in NUMBER_TAGS:
        construct = _construct_int if node.tag == "!!int" else _construct_float
    else:
        return None
    # ValueError also covers Python's 4300-digit int() limit, at which
    # the Python yq stops too.
    try:
        return construct(text)
    except ValueError as error:
        kind = f"tagged {node.tag} but " if node.style else ""
        raise ActionError(
            f"{where} is {kind}'{visible(text)}', which Jenkins cannot read as "
            f"a number; {_remedy(node)}"
        ) from error


def jenkins_text(value: int | float) -> str:
    """How jq prints the Python yq's JSON for a number."""
    if isinstance(value, int):
        return str(value)
    return jq_tostring(repr(value))


def _remedy(node: Node) -> str:
    """How to make the value text for every reader."""
    text = visible(str(node.text))
    if node.style == "":
        return f'write it as a quoted string, "{text}"'
    return f'drop the tag and write it as a quoted string, "{text}"'


def resolve(node: Node, mode: str, lane: str, where: str) -> str:
    """The value under ``mode``; ``lane`` is the literal-mode result.

    ``where`` names the field and file for an error.
    """
    if mode == "literal":
        return lane
    if mode == "refuse":
        # A numeric tag is checked first: its text may not even parse.
        if node.tag in NUMBER_TAGS or jenkins_number(node, where) is not None:
            text = visible(str(node.text))
            what = (
                f"the unquoted YAML number {text}"
                if node.style == ""
                else f"tagged {node.tag}, which makes {text} a YAML number"
            )
            raise ActionError(
                f"{where} is {what}, which YAML readers render differently "
                f"(1.10 can become 1.1); {_remedy(node)}, or set "
                "numeric_versions to literal or yaml"
            )
        return lane
    number = jenkins_number(node, where)
    if number is None:
        # Jenkins reads text here, even where yq reads 0b101 or 1_000
        # as an integer.
        return node.text if node.tag in NUMBER_TAGS else lane
    if isinstance(number, float) and not math.isfinite(number):
        raise ActionError(
            f"{where} is {visible(str(node.text))}, not a finite number; "
            f"{_remedy(node)}"
        )
    try:
        return jenkins_text(number)
    except ValueError as error:
        # str() of an int past 4300 digits, such as a long 0x literal.
        raise ActionError(
            f"{where} is a number too long for Jenkins to print; {_remedy(node)}"
        ) from error
