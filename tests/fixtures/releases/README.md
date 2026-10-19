<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Container release file fixtures

A labelled corpus of container release files, each run through two
validators:

- **Schema**: [verify-release-schema-action] at
  `d8166617b2566adb151a9594849440ebfb9a9183` (v1.1.0), which runs
  `lftools-uv schema verify` (0.5.7 here) against its bundled
  `schemas/release-container-schema.yaml`.
- **Action**: this action, with the registry inputs shown.

[`expected.json`](expected.json) holds the same verdicts in machine
readable form, with the directory each fixture's release file sits
in. `tests/test_fixtures.py` asserts every action verdict offline,
and the `schema` job in `.github/workflows/testing.yaml` asserts
every schema verdict with the real validator.

Every fixture is original to this repository, under its licence. A
fixture that models a real release file reproduces the features listed
below and none of the file's content: names, projects, commits and
build paths are neutral, except the registry hosts where they are the
point. The citation names the real file so a reader can find the
shape it stands for.

## Verdicts

ONAP fixtures run with `snapshot_registry: nexus3.onap.org:10001` and
`release_registry: nexus3.onap.org:10002`, O-RAN-SC fixtures with
`nexus3.o-ran-sc.org:10004` and `nexus3.o-ran-sc.org:10002`, and the
rest with `nexus3.example.org:10001` and `nexus3.example.org:10002`.

<!-- markdownlint-disable MD013 -->

| Fixture                                                      | Features                                                                                    | Schema | Action                                               |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------- | ------ | ---------------------------------------------------- |
| [`onap-dmaap-datarouter.yaml`](onap-dmaap-datarouter.yaml)   | Names with a `/` sub-path; unquoted versions; `log-dir` with a hyphen; a 39-character `ref` | accept | accept                                               |
| [`onap-pnf-simulator.yaml`](onap-pnf-simulator.yaml)         | Registry overrides on the caller's host, another port; dotted names                         | accept | accept                                               |
| [`onap-java11.yaml`](onap-java11.yaml)                       | Mutable source tag `latest`; four-space list indent; `project` with `/`                     | accept | accept                                               |
| [`onap-cps-dmi-plugin.yaml`](onap-cps-dmi-plugin.yaml)       | `tag_release: true`; every scalar unquoted                                                  | accept | accept                                               |
| [`onap-policy-docker.yaml`](onap-policy-docker.yaml)         | Both registries overridden to `docker.io`                                                   | accept | reject with the ONAP inputs; accept with `docker.io` |
| [`oran-nonrtric-capif.yaml`](oran-nonrtric-capif.yaml)       | Leading `---`; `tag_release: false`; both overrides; unquoted dotted versions               | accept | accept                                               |
| [`oran-ric-app-ml.yaml`](oran-ric-app-ml.yaml)               | A comment line before `---`                                                                 | accept | accept                                               |
| [`oran-it-test-empty-ref.yaml`](oran-it-test-empty-ref.yaml) | `ref:` with no value                                                                        | reject | accept                                               |
| [`missing-project.yaml`](missing-project.yaml)               | No `project`                                                                                | reject | accept                                               |
| [`missing-ref.yaml`](missing-ref.yaml)                       | No `ref`                                                                                    | reject | accept                                               |
| [`unquoted-number-tag.yaml`](unquoted-number-tag.yaml)       | `container_release_tag: 1.10`, unquoted                                                     | reject | accept                                               |
| [`container-bad-name.yaml`](container-bad-name.yaml)         | A `containers` entry named `Bad Name!`                                                      | accept | reject                                               |
| [`container-extra-key.yaml`](container-extra-key.yaml)       | A `containers` entry with a key beyond `name` and `version`                                 | accept | accept                                               |
| [`container-bare-number.yaml`](container-bare-number.yaml)   | A bare `42` as a `containers` entry                                                         | accept | reject                                               |
| [`registry-other-host.yaml`](registry-other-host.yaml)       | Both overrides on a host other than the inputs'                                             | accept | reject                                               |
| [`tag-release-string.yaml`](tag-release-string.yaml)         | `tag_release: 'true'`, a string                                                             | reject | accept                                               |
| [`missing-release-tag.yaml`](missing-release-tag.yaml)       | No `container_release_tag`                                                                  | reject | reject                                               |

<!-- markdownlint-enable MD013 -->

## Why the verdicts differ

- **`ref`, `project`, `tag_release`**: the schema requires `project`
  and `ref` as strings and types `tag_release` as a boolean. The
  promotion reads none of them, so the action checks none of them.
  `ref:` with no value is YAML null, not a string.
- **Numbers**: `lftools-uv` reads YAML with PyYAML, which types an
  unquoted `1.10` as a float, and the schema wants a string. The
  action reads a number as its `numeric_versions` input says; by
  default, as written.
- **`containers` entries**: the schema puts `properties` and
  `additionalProperties: false` directly on the `containers` array.
  JSON Schema Draft 4 applies those keywords to objects alone, so the
  schema checks no entry at all. The action requires each entry to be
  a mapping, with a name in Docker's repository grammar and a version
  in its tag grammar. Both accept a key beyond `name` and `version`:
  the action drops it from its output.
- **Registry overrides**: the schema accepts any string. The action
  keeps each override on the host of the matching input, because the
  promotion logs in there with that registry's credential, so the same
  file can pass with one caller's inputs and fail with another's.

## Citations

Each fixture below models the shape of the named file; it copies
none of it.

<!-- markdownlint-disable MD013 -->

| Fixture                                                      | Project  | Gerrit repository                      | Path                                                              | Commit                                                                   |
| ------------------------------------------------------------ | -------- | -------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------ |
| [`onap-dmaap-datarouter.yaml`](onap-dmaap-datarouter.yaml)   | ONAP     | `dmaap/datarouter`                     | `releases/2.1.15-container.yaml`                                  | `585e0eb90e5a093dc2d49879c6a8a21a8a90f5e6`                               |
| [`onap-pnf-simulator.yaml`](onap-pnf-simulator.yaml)         | ONAP     | `integration/simulators/pnf-simulator` | `releases/1.0.6-container.yaml`                                   | `8a22fc38eb0d50c6d397cd16889e87d17b2d80f1`                               |
| [`onap-java11.yaml`](onap-java11.yaml)                       | ONAP     | `integration/docker/onap-java11`       | `releases/12.0.0-container.yaml`                                  | `fa65459fd726c0e97d2ce52e3296efa7f921a839`                               |
| [`onap-cps-dmi-plugin.yaml`](onap-cps-dmi-plugin.yaml)       | ONAP     | `cps/ncmp-dmi-plugin`                  | `releases/1.5.0-container.yaml`                                   | `3d61b4768845821064aeb7227e6058cc1d372d0e`                               |
| [`onap-policy-docker.yaml`](onap-policy-docker.yaml)         | ONAP     | `policy/docker`                        | `releases/2.0.0-container.yaml`                                   | `44283c65057e63f91adc4ab0137fa47cf5c8b709`                               |
| [`oran-nonrtric-capif.yaml`](oran-nonrtric-capif.yaml)       | O-RAN-SC | `nonrtric/plt/sme`                     | `.releases/container-release-capif.yaml`                          | `c0ef8c977c54f9541b41c4d7d3fdb019f035b17c` <!-- codespell:ignore sme --> |
| [`oran-ric-app-ml.yaml`](oran-ric-app-ml.yaml)               | O-RAN-SC | `ric-app/ml`                           | `.releases/docker.yaml`                                           | `ce9d4b284225c5d84a63729f96ef7fb5d5045ac7`                               |
| [`oran-it-test-empty-ref.yaml`](oran-it-test-empty-ref.yaml) | O-RAN-SC | `it/test`                              | `ric_benchmarking/Bouncer/releases/container-release-app_hw.yaml` | `c84cf647bef80dd1f3766890d4215e85725ae85a`                               |

<!-- markdownlint-enable MD013 -->

The other fixtures model no single file. Each stands for one
disagreement between the validators, or one case both reject.

The two O-RAN-SC fixtures cited at a `.releases/` path model files
kept in `.releases/`, and every other fixture sits in `releases/`.
`expected.json` records each fixture's directory, and
`tests/test_fixtures.py` commits the fixture there before running the
action, so the corpus exercises both directories the action reads.

[verify-release-schema-action]: https://github.com/lfreleng-actions/verify-release-schema-action
