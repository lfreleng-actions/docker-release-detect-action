#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The "Detect container release file in merged commit" step body from
# lfreleng-actions/docker-workflows merge.yaml (job check-release) at
# ac1f91064b609f9be68ec5c93e1a47ba1ada8741, lines 1588-1733, extracted
# by removing the ten-space YAML indent and trailing whitespace. The
# tests run it as the runner does, with bash -eo pipefail, in the
# repository's top level, with SNAPSHOT_REGISTRY and RELEASE_REGISTRY
# bound as the step's env: block binds them.
#
# The body below stays verbatim, so any lint exceptions live in this
# header.

# Detect a release file added/changed by the merged commit.
# -m --first-parent makes merge commits diff against their
# first parent (plain diff-tree emits nothing for merges,
# which would silently suppress release detection). A
# diff-tree failure (e.g. insufficient history) fails loudly
# rather than silently skipping an intended release.
if ! files=$(git diff-tree -m --first-parent \
  --no-commit-id -r HEAD --name-only -- "releases/"); then
  echo "::error::git diff-tree failed; cannot detect" \
    "release files (insufficient checkout history?)"
  exit 1
fi
no_release() {
  echo "$1"
  {
    echo "has_release=false"
    echo "version="
    echo "containers_json="
    echo "pull_registry="
    echo "push_registry="
  } >> "$GITHUB_OUTPUT"
  exit 0
}
declare -a container_files=()
while IFS= read -r file; do
  [ -n "${file}" ] || continue
  # Removed by the merged commit; not a release trigger
  [ -f "${file}" ] || continue
  dist_type=$(yq -r '.distribution_type // ""' "${file}")
  if [ "${dist_type}" = 'container' ]; then
    container_files+=("${file}")
  else
    echo "::notice::Ignoring non-container release file:" \
      "${file} (distribution_type: ${dist_type:-unset})"
  fi
done <<< "${files}"
if [ "${#container_files[@]}" -eq 0 ]; then
  no_release "No container release file in merged commit"
fi
# Enforce exactly one container release descriptor per
# merged commit; multiple would make promotion arbitrary
if [ "${#container_files[@]}" -gt 1 ]; then
  echo "::error::Multiple container release files in" \
    "merged commit:"
  printf -- '%s\n' "${container_files[@]}"
  exit 1
fi
release_file="${container_files[0]}"
echo "Container release file detected: ${release_file}"
version=$(yq -r '.container_release_tag // ""' \
  "${release_file}")
# Guard against malformed/hostile content before values
# reach $GITHUB_OUTPUT and downstream jobs: the value
# becomes a docker tag, so enforce the docker tag grammar
# (leading alphanumeric/underscore, 128 characters max)
if [[ ! "${version}" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]]
then
  echo "::error::Invalid or missing" \
    "container_release_tag in ${release_file}"
  exit 1
fi
containers=$(yq -o=json -I=0 '.containers // []' \
  "${release_file}")
# Names become registry repository paths (validate each
# slash-separated component, and require uniqueness —
# duplicate names would map to the same promotion
# destination and silently leave only the last source
# promoted) and versions become docker tags (apply the tag
# grammar), so crane receives well-formed references
if ! jq -e 'type == "array" and length > 0 and
    ([.[].name] | length == (unique | length)) and
    all(.[]; (.name | type == "string" and
      (split("/") |
       all(.[];
         test("^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")))) and
      ((.version | type) as $t |
       ($t == "string" or $t == "number")) and
      ((.version | tostring) |
      test("^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")))' \
    >/dev/null 2>&1 <<< "${containers}"; then
  echo "::error::Invalid containers list in" \
    "${release_file}: expected a non-empty array of" \
    "{name, version} entries with registry-safe values"
  exit 1
fi
# Normalise versions to strings (yq may emit numbers) and
# compact to a single line for $GITHUB_OUTPUT
containers=$(jq -c \
  '[.[] | {name: .name, version: (.version | tostring)}]' \
  <<< "${containers}")
# Optional per-file registry overrides (the LF self-release
# container schema carries these). The overrides may vary
# the port or repository path only: the promotion job logs
# in to these endpoints with the loaded registry
# credential, so an arbitrary host in a merged release file
# could exfiltrate it. Constrain each override to the host
# of the corresponding workflow input.
pull_registry=$(yq -r '.container_pull_registry // ""' \
  "${release_file}")
push_registry=$(yq -r '.container_push_registry // ""' \
  "${release_file}")
check_registry() {
  local override="$1" allowed="$2" field="$3"
  [ -n "${override}" ] || return 0
  local pattern='^[A-Za-z0-9.-]+(:[0-9]+)?'
  pattern+='(/[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*)*$'
  if [[ ! "${override}" =~ ${pattern} ]]
  then
    echo "::error::Invalid ${field} in ${release_file}:" \
      "${override} (expected host[:port] with an optional" \
      "repository path of lowercase components)"
    exit 1
  fi
  # Compare hosts, so a release file may still select a
  # different port or repository path on the configured
  # host — Nexus 3 and Artifactory both express the target
  # repository that way, and the credential authenticates
  # to the host either way. Strip the path before the port,
  # because a host-only value carrying a path has no colon
  # to cut at and would otherwise compare host and path
  # together.
  local override_host="${override%%/*}"
  override_host="${override_host%%:*}"
  local allowed_host="${allowed%%/*}"
  allowed_host="${allowed_host%%:*}"
  if [ "${override_host}" != "${allowed_host}" ]; then
    echo "::error::${field} in ${release_file}" \
      "(${override}) must stay on the configured" \
      "registry host (${allowed_host}); the promotion" \
      "credential only ever authenticates there"
    exit 1
  fi
}
check_registry "${pull_registry}" "${SNAPSHOT_REGISTRY}" \
  'container_pull_registry'
check_registry "${push_registry}" "${RELEASE_REGISTRY}" \
  'container_push_registry'
echo "Release tag: ${version}"
echo "Containers: ${containers}"
{
  echo "has_release=true"
  echo "version=${version}"
  echo "containers_json=${containers}"
  echo "pull_registry=${pull_registry}"
  echo "push_registry=${push_registry}"
} >> "$GITHUB_OUTPUT"
