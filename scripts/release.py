# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The rules a container release file's values must meet.

Each rule is the lane's, restated. Where the lane's jq accepted a value
that cannot work downstream, the rule here is stricter, and says so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from scripts.gha import ActionError, visible
from scripts.numbers import Node, jq_tostring, literal_text, resolve

# Docker's tag grammar: a word character, then up to 127 more of
# [A-Za-z0-9._-]. fullmatch throughout: Python's '$' also matches
# before a trailing newline.
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", re.ASCII)
# One '/'-separated component of a repository name, in Docker's
# reference grammar. The lane's [a-z0-9]([a-z0-9._-]*[a-z0-9])? also
# took runs such as 'a..b' or 'a_-b', which Docker refuses.
NAME_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*", re.ASCII)
# host[:port], then an optional path of lowercase components.
REGISTRY = re.compile(
    r"[A-Za-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*)*", re.ASCII
)
# Docker's limit on a repository path below the registry host.
PATH_MAX = 255

CONTAINERS_INVALID = (
    "expected a non-empty array of {name, version} entries with registry-safe values"
)


@dataclass(frozen=True)
class _Number:
    """A JSON number, kept as the literal yq wrote."""

    literal: str


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def _load(text: str) -> Any:
    return json.loads(
        text,
        parse_int=_Number,
        parse_float=_Number,
        parse_constant=_reject_constant,
    )


# json.loads, with _load's hooks, yields exactly these types.
_KINDS: dict[type, str] = {
    dict: "a mapping",
    list: "a list",
    str: "a string",
    bool: "a boolean",
    _Number: "a number",
}


def _describe(value: Any) -> str:
    return _KINDS.get(type(value), "null")


def _name_problem(name: str) -> str | None:
    # jq splits "" into no components at all, so the lane accepted an
    # empty name; Python splits it into one empty component, refused.
    for component in name.split("/"):
        if not NAME_COMPONENT.fullmatch(component):
            return (
                f"name '{visible(name)}' is not a repository path of lowercase "
                f"'/'-separated components ([a-z0-9] runs joined by one '.', "
                f"one '_', '__' or dashes)"
            )
    return None


def _version(
    value: Any, node: Callable[[], Node | None], mode: str, where: str
) -> tuple[str | None, str | None]:
    """The version as a tag, or why it is not one.

    ``node`` fetches yq's description of the value, which the literal
    reading of a number and the other modes need.
    """
    if isinstance(value, str):
        found = node() if mode != "literal" else None
        text = resolve(found, mode, value, where) if found else value
    elif isinstance(value, _Number):
        found = node()
        if found is not None and mode != "literal":
            # yq tagged it a number, so resolve never falls back to the
            # literal reading here.
            text = resolve(found, mode, "", where)
        else:
            try:
                text = (
                    literal_text(found, value.literal)
                    if found is not None
                    else jq_tostring(value.literal)
                )
            except ArithmeticError:
                # Decimal bounds the exponent: 0e999999999999999999 overflows.
                return None, (
                    f"version {visible(value.literal)} is a number too large "
                    "to write as a tag"
                )
    else:
        return None, f"version is {_describe(value)}, not a string or number"
    # The lane's jq test() let one trailing newline through, which no
    # registry accepts in a tag; fullmatch refuses it.
    if not TAG.fullmatch(text):
        return None, f"version '{visible(text)}' is not a valid Docker tag"
    return text, None


def parse_containers(
    text: str,
    release_file: str,
    mode: str = "literal",
    nodes: Callable[[], list[Any]] = list,
) -> list[dict[str, str]]:
    """Validate yq's JSON for ``containers``; return {name, version} pairs.

    Versions come back as strings, numbers read as ``mode`` reads them
    from ``nodes``, yq's [tag, style, text] for each entry's version,
    and every other key is dropped, as the lane's jq did.
    """
    fetched: list[list[Any]] = []

    def node_at(index: int) -> Node | None:
        if not fetched:
            fetched.append(nodes())
        found = fetched[0]
        return Node.from_yq(found[index]) if index < len(found) else None

    prefix = f"Invalid containers list in {release_file}: {CONTAINERS_INVALID}"
    try:
        containers = _load(text)
    except ValueError as error:
        raise ActionError(f"{prefix}; yq returned no usable JSON") from error
    if not isinstance(containers, list):
        raise ActionError(f"{prefix}; containers is {_describe(containers)}")
    if not containers:
        raise ActionError(f"{prefix}; containers is empty")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(containers, start=1):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            raise ActionError(f"{prefix}; {where} is {_describe(entry)}")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ActionError(
                f"{prefix}; {where}: name is {_describe(name)}, not a string"
            )
        problem = _name_problem(name)
        if problem:
            raise ActionError(f"{prefix}; {where}: {problem}")
        version, problem = _version(
            entry.get("version"),
            partial(node_at, index - 1),
            mode,
            f"The version of containers {where} ({visible(name)}) in {release_file}",
        )
        if problem or version is None:
            raise ActionError(f"{prefix}; {where} ({visible(name)}): {problem}")
        if name in seen:
            raise ActionError(
                f"{prefix}; {where}: name '{visible(name)}' appears more than once, "
                "so two sources would promote to one destination"
            )
        seen.add(name)
        result.append({"name": name, "version": version})
    return result


def check_tag(tag: str, release_file: str) -> None:
    """The release tag must be a Docker tag."""
    if not TAG.fullmatch(tag):
        found = (
            f"'{visible(tag)}' is not a valid Docker tag" if tag else "it is not set"
        )
        raise ActionError(
            f"Invalid or missing container_release_tag in {release_file}: {found}"
        )


def registry_host(value: str) -> str:
    """The host of host[:port][/path], as the lane cut it.

    The path goes first, so a value with a path and no port does not
    compare host and path together.
    """
    return value.split("/", 1)[0].split(":", 1)[0]


def check_path_lengths(
    registries: tuple[str, ...], containers: list[dict[str, str]], release_file: str
) -> None:
    """Each image's repository path, below the host, fits Docker's limit.

    The promotion copies ``registry/name:tag``; a registry refuses a
    repository path over 255 characters, so crane copy would fail.
    """
    for registry in registries:
        prefix = registry.split("/", 1)[1] + "/" if "/" in registry else ""
        for container in containers:
            path = prefix + container["name"]
            if len(path) > PATH_MAX:
                raise ActionError(
                    f"Invalid containers list in {release_file}: the repository "
                    f"path '{visible(path)}' under {visible(registry)} is "
                    f"{len(path)} characters, over Docker's {PATH_MAX}"
                )


def check_override(
    value: str, allowed: str, field: str, input_name: str, release_file: str
) -> None:
    """A registry override may change the port or path, never the host.

    The promotion job logs in to these endpoints with the configured
    registry's credential, so a merged release file naming another host
    could send that credential anywhere.
    """
    if not value:
        return
    expected = "host[:port] with an optional repository path of lowercase components"
    if not REGISTRY.fullmatch(value):
        raise ActionError(
            f"Invalid {field} in {release_file}: {visible(value)} (expected {expected})"
        )
    # The input anchors the trust: with a scheme, 'https://h' would
    # yield the host 'https', which an override 'https:443' matches.
    if allowed and not REGISTRY.fullmatch(allowed):
        raise ActionError(
            f"The {input_name} input ({visible(allowed)}) is not {expected}, "
            f"so {field} in {release_file} cannot be checked against it"
        )
    host, allowed_host = registry_host(value), registry_host(allowed)
    if host != allowed_host:
        hint = (
            f"; the {input_name} input is empty, so no override is allowed"
            if not allowed
            else ""
        )
        raise ActionError(
            f"{field} in {release_file} ({visible(value)}) must stay on the "
            f"configured registry host ({allowed_host}); the promotion "
            f"credential only ever authenticates there{hint}"
        )
