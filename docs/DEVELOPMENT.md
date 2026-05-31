# Development

This document is the contributor/operator guide for developing hermes-cortex from
a source checkout and promoting a reviewed change into the Hermes runtime plugin.
The README keeps only the short path; this file carries the boring details. Alas,
the boring details are where most deployment bugs breed.

## Checkouts and state

Use separate locations for source, runtime code, and mutable Cortex state:

| Role | Typical path | Notes |
|---|---|---|
| Development source | `~/hermes-workspace/20_repos/hermes-cortex/` | Normal editing, tests, commits, pushes. |
| Hermes runtime plugin | `~/.hermes/plugins/cortex/` | Git-managed plugin checkout that Hermes loads. Keep it clean. |
| Cortex runtime state | `~/.hermes/cortex/` | Profile-local config, chunks, vector store, graph artifacts. |
| Vault | `~/hermes-workspace/vault/` | User knowledge base indexed by Cortex. |

Do not store `chunks.jsonl`, Chroma data, local config, or graph outputs in the
plugin checkout. Runtime updates should be `git pull --ff-only` clean; mutable
state belongs under `~/.hermes/cortex/`.

## Local setup

From the development checkout:

```bash
cd ~/hermes-workspace/20_repos/hermes-cortex
./install.sh --dev
source .venv/bin/activate
```

`install.sh --dev` creates or reuses `.venv` and installs the package editable
with the `dev` extra from `pyproject.toml` (`pytest`, `ruff`, `build`, `wheel`,
and friends). Use `--python PATH` or `HERMES_CORTEX_PYTHON` when testing a
specific interpreter.

## Routine checks

Run the same local gate that CI runs, but isolate `HERMES_HOME` so tests cannot
accidentally read the live `~/.hermes/state.db`:

```bash
ruff check .
TMP_HOME=$(mktemp -d)
HERMES_HOME="$TMP_HOME" pytest
rm -rf "$TMP_HOME"
python -m build --no-isolation
git diff --check
```

The GitHub Actions workflow in [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml)
runs the package install, Ruff, pytest, and `python -m build --no-isolation` on
Python 3.11 and 3.12 for pushes and pull requests targeting `main`. CI runners do
not carry a developer's live Hermes profile, so the workflow can use plain
`pytest`.

For a quick runtime-oriented smoke after a docs/skill-only change, the full test
suite may be overkill, but at minimum run:

```bash
hermes cortex --help
hermes cortex status
hermes cortex validate-frontmatter --strict
```

For retrieval or indexing changes, add:

```bash
hermes cortex search-eval --top-k 20 --allow-failures
hermes cortex lifecycle maintenance
hermes cortex graph broken
```

## Runtime deploy/update workflow

The supported Hermes runtime path is the standalone plugin checkout under
`~/.hermes/plugins/cortex/`. To update it after a reviewed/pushed change:

```bash
cd ~/.hermes/plugins/cortex
git fetch origin
git status --short --branch
git pull --ff-only origin main
./install.sh --with-hermes-venv --with-hermes-skills
hermes cortex status
```

What that install command does:

- installs/refreshes the Python package and CLI in the Hermes Agent venv;
- copies the bundled long-term-memory skills into `~/.hermes/skills/`;
- leaves profile-local Cortex config and vector/index state untouched.

Restart the gateway only when runtime code or hooks changed and gateway workers
need to load the new code. Pure README/docs changes do not require a restart.
Skill text changes affect new sessions after the skill files have been synced;
current conversations may still carry already-injected context until `/reset` or
a new session. Yes, prompt caching is a lifestyle choice.

## Skill updates

Bundled skills live under [`../skills/`](../skills/). The default runtime install
copies `skills/long-term-memory/` to `~/.hermes/skills/long-term-memory/` when
`--with-hermes-skills` is passed.

When editing a skill:

1. edit the repo copy under `skills/.../SKILL.md`;
2. validate frontmatter shape by loading or parsing the skill;
3. run relevant tests/smokes;
4. commit and push;
5. sync runtime skills with `./install.sh --with-hermes-skills` from the runtime
   plugin checkout after it has been fast-forwarded.

Do not edit only the runtime copy unless you intentionally want an uncommitted
local hotfix. Those are fun for approximately seven minutes.

## SessionDB and live-environment tests

Some tests exercise SessionDB behavior. A local run with the real default
`HERMES_HOME` can pick up live sessions and fail expectations that are intended
for an empty profile. Use the isolated `HERMES_HOME` pattern above for hermetic
unit tests.

## Release preflight

Before making a release or changing public-facing defaults, run the README's
release preflight plus the local gate above. Keep these values in sync before
tagging:

```text
pyproject.toml  → [project].version
plugin.yaml     → version
```

The Python package build is a dependency/packaging gate. The supported Hermes
plugin deployment remains the Git checkout under `~/.hermes/plugins/cortex/`.
