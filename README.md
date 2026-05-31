# hermes-cortex

[![Hermes Agent](https://img.shields.io/badge/Hermes-Agent-14130f?style=flat-square)](https://hermes-agent.nousresearch.com)
[![CI](https://github.com/Mugen0815/hermes-cortex/actions/workflows/ci.yml/badge.svg)](https://github.com/Mugen0815/hermes-cortex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square)](./LICENSE)

Cortex-backed vault memory for [Hermes Agent](https://hermes-agent.nousresearch.com).

`hermes-cortex` indexes a Markdown / Obsidian-style vault and exposes three
Hermes tools: search notes, read notes, and build compact prompt context. The
operator-facing CLI is available as `hermes cortex ...` once the plugin is
enabled.

## What this is

- A standalone Hermes plugin loaded from `~/.hermes/plugins/cortex/`
- A local vault indexer for Markdown notes
- Hybrid retrieval: BM25 + vector embeddings + wikilink graph expansion
- A context builder that returns source-cited Markdown within a token budget
- A maintenance CLI for index, embeddings, graph artifacts, lifecycle checks,
  and static graph viewer generation

## What this is not

- Not an Obsidian community plugin
- Not a hosted memory service
- Not a database of truth; the Markdown vault stays the source of truth
- Not a replacement for Hermes' own `MEMORY.md`, `USER.md`, or `SOUL.md`

## Architecture

```text
Hermes Agent
  └─ cortex plugin
       ├─ tools: vault_search, vault_read_note, vault_build_context
       ├─ hooks: session/context helpers
       └─ CLI: hermes cortex ...
             │
             ▼
Markdown vault (*.md)
  ├─ indexer        → chunks.jsonl
  ├─ embedder       → Chroma vector store
  ├─ graph builder  → wikilink graph artifacts
  └─ search         → BM25 + vector + graph, fused by RRF
             │
             ▼
Context builder → compact, cited prompt context
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the component-level view.

## Install

Install as a Hermes user plugin:

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/Mugen0815/hermes-cortex.git ~/.hermes/plugins/cortex
cd ~/.hermes/plugins/cortex

hermes plugins enable cortex
hermes tools enable cortex
./install.sh --with-hermes-venv --with-hermes-skills
```

Initialize the vault config and build the retrieval artifacts. Fresh init is
idempotent and does not mutate `SOUL.md`, `MEMORY.md`, or `USER.md` by default;
legacy memory-file mutation stays opt-in only.

```bash
hermes cortex init --yes
hermes cortex index
hermes cortex embed
hermes cortex graph build
```

Start a new Hermes session after first install. Existing sessions cache their
tool list; `/reset` or a full restart is the boring but correct fix.

## Verify

```bash
hermes plugins list
hermes tools list
hermes cortex --help
hermes cortex search "memory-query-flow" --top-k 3
scripts/smoke-runtime-cortex-cli.sh
```

Expected:

- plugin `cortex` is enabled
- toolset `cortex` exposes `vault_search`, `vault_read_note`, `vault_build_context`
- `hermes cortex --help` includes `search-eval`
- `hermes cortex search ...` returns vault results after `index` + `embed`
- `scripts/smoke-runtime-cortex-cli.sh` confirms `hermes cortex search-eval --json --allow-failures` returns the eval JSON envelope (`schema_version`, `case_count`, `passed`, `failed`, `cases`)

## Frontmatter validation

`hermes cortex validate-frontmatter` is read-only. It parses YAML frontmatter,
checks required vault metadata, and does not rewrite notes.

```bash
hermes cortex validate-frontmatter --json
hermes cortex validate-frontmatter --path '30_projects/Project - hermes-cortex.md'
hermes cortex validate-frontmatter --strict
```

Behavior:

- default exit code is `0` when only warnings are present
- any validation error returns exit code `1`
- `--strict` upgrades warnings to exit code `1`
- `--json` prints a stable report with `schema_version`, `vault_path`, `checked_count`, `error_count`, `warning_count`, and per-file `issues`
- `--path` limits scope to specific note or directory paths inside the vault

Common issue codes include `missing_frontmatter`, `yaml_parse_error`, `missing_required`, `missing_domain`, and `normalization_warning`.

## Update

Runtime updates are Git-only. No copy, rsync, generated stub, or symlink farm. Keep your development checkout and active runtime plugin checkout (`~/.hermes/plugins/cortex/`) separate; pushing a development repo does not update `hermes cortex ...` until the runtime checkout is pulled.

```bash
cd ~/.hermes/plugins/cortex
git fetch origin --prune
git pull --ff-only origin main
./install.sh --with-hermes-venv --with-hermes-skills
scripts/smoke-runtime-cortex-cli.sh
```

Then start a new Hermes session or `/reset` the current one.

## Common commands

| Command | Purpose |
|---|---|
| `hermes cortex init --yes` | Create config, vault folders, and seed notes without mutating Hermes memory files by default |
| `hermes cortex index [--force]` | Chunk vault notes into `chunks.jsonl` |
| `hermes cortex embed [--force]` | Build/update Chroma embeddings |
| `hermes cortex graph build [--force]` | Build wikilink graph artifacts |
| `hermes cortex validate-frontmatter [--json] [--strict] [--path ...]` | Read-only Vault frontmatter validation |
| `hermes cortex search "query" --top-k 20` | Search the vault from the shell |
| `hermes cortex search-eval --output search-eval-baseline.json --baseline baseline.json --allow-failures` | Run fixed ranking eval cases with per-hit diagnostics (`final_score`, `rrf_score`, channel ranks, raw/capped boost multiplier, quality factor/reason); `--baseline` adds compare summary and baseline deltas |
| `hermes cortex context "query" --budget 4000` | Build cited Markdown context |
| `hermes cortex config path` | Show active config path |
| `hermes cortex config show` | Show effective config: vault, index, hooks, skill path, and hook lifecycle rows |
| `hermes cortex status` | Show plugin/code path plus config, vault, index state, and effective hook lifecycle/status |
| `hermes cortex graph status` | Show graph health and diagnostics |
| `hermes cortex graph viewer -o graph.html --embed-data` | Generate a static graph viewer |
| `hermes cortex lifecycle maintenance` | Run index → embed → graph build |
| `hermes cortex lifecycle nightly --dry-run` | Preview explicit `00_inbox/` review-candidate promotion/cleanup |
| `hermes cortex lifecycle weekly --dry-run` | Print the read-only WeeklyReview Markdown report |
| `hermes cortex cron status` | Check all installed Cortex cron jobs |
| `hermes cortex cron status --job nightly` | Check only the nightly promotion cron job |
| `hermes cortex cron status --job weekly` | Check only the WeeklyReview cron job |

## Hook lifecycle status

`cortex config show` and `cortex status` now print an operator-facing hook
lifecycle table. It separates lifecycle phases from legacy projection fields:

- `cache_warm` runs at `session_start` and only warms process-local cache.
- `skill_bootstrap`, `static_file_bootstrap`, `recent_context`, and
  `dynamic_context` describe `pre_llm` user-message hook context.
- `legacy_context_injection` is still displayed for compatibility. In a
  legacy-only config it is `legacy-active`; when any semantic hook block is
  configured it is `legacy-ignored` and the skipped reason says why. If absent,
  it is shown as `legacy-absent`.

The table shows `enabled`, `effective`, timing (`first_turn`, `each_turn`, or
`session_start`), origin, source, payload, target, and skipped reason. Supported
`when` values are validated per block: `skill_context` accepts `first_turn` or
`each_turn`; `bootstrap_context` and `recent_context` accept only `first_turn`;
`dynamic_context` accepts only `each_turn`.

## Nightly promotion lifecycle

The nightly promotion job is **canonical-first**:

- Clear, high-confidence durable knowledge is written directly to the canonical
  vault folders: `10_facts/`, `20_decisions/`, `30_projects/`, `40_runbooks/`.
- `00_inbox/` is only for uncertain, duplicate-sensitive, or human-review cases.
- Active inbox candidates use explicit review metadata:
  `status: draft`, `review_status: pending`, and `review_reason: "..."`.
- Archived source notes must not remain promotable. In other words,
  `status: archived` + `promote: true` is an invalid state.

`cortex lifecycle nightly` still exists for explicit inbox candidates and safe
cleanup, but the normal cron prompt should not use `00_inbox/` as a staging dump.
After vault writes, run `hermes cortex lifecycle maintenance` to refresh index,
embeddings, and graph artifacts.

The packaged cron job is configured under `cron.nightly_promotion` in the active
Cortex config. Repo defaults are intentionally public-safe and do **not** install jobs unless you explicitly opt in:

```yaml
cron:
  nightly_promotion:
    enabled: false
    name: hermes-cortex-nightly-promotion
    schedule: "0 2 * * *"
    timezone: Europe/Berlin
    deliver: origin
    enabled_toolsets: [file, terminal]
    lookback_days: 1
    session_globs:
      - ~/.hermes/sessions/*.jsonl
      - ~/.hermes/sessions/session_*.json
    dry_run_first: true
```

`dry_run_first: true` (the default) makes the cron prompt include a `lifecycle nightly --dry-run` step before the `--write` apply step, so the LLM reviews what would be promoted before writing. Set to `false` to skip the dry run and go straight to apply. Both modes still run `lifecycle maintenance` (index → embed → graph) after writing.

Nightly/session promotion is now SessionDB-primary on the ticket branch: the live contract reads `~/.hermes/state.db` first, falls back to legacy JSON/JSONL only under the documented failure/empty-source cases, ignores `request_dump_*.json`, and reports which backend won, how many sessions each source contributed, and why fallback was used. This repo-doc update describes the behavior; it does not perform a runtime plugin deploy.

Use a private local config override for personal delivery targets, e.g. a Signal
DM. Do not put user-specific/personal recipients into repo defaults, examples, or tests.
`timezone` is used in the generated prompt and lookback wording. The Hermes cron
scheduler itself reads its schedule clock from Hermes' runtime timezone
configuration (global Hermes `timezone` / `HERMES_TIMEZONE`, or server-local), not
from this per-Cortex cron section. Yes, two knobs. No, they are not the same knob.

WeeklyReview is configured next to NightlyPromotion and is selected explicitly with `--job weekly`. It is disabled by default; set `enabled: true` in your private config before installing it:

```yaml
cron:
  weekly_review:
    enabled: false
    name: hermes-cortex-weekly-review
    schedule: "0 8 * * 1"
    timezone: Europe/Berlin
    deliver: origin
    output_format: markdown
    dry_run: true
    stale_days: 180
    stale_min_importance: 4.0
    consolidation_min_degree: 3
```

`dry_run: true` adds `--dry-run` to the generated `cortex lifecycle weekly` command; `false` omits it. WeeklyReview remains read-only either way. The generated Markdown report includes graph stats, duplicates, stale high-importance notes, broken references, consolidation proposals, orphan nodes, contradictions, duration, and any error.

`cortex cron install` and `cortex cron uninstall` remain backward-compatible and target NightlyPromotion by default. `cortex cron status` defaults to `--job all` so operators see both NightlyPromotion and WeeklyReview at a glance; use `--job nightly` or `--job weekly` for a single job. Install updates configured/default/legacy job identities instead of creating duplicates. `enabled: false` makes install refuse/skip creation.

**Runtime plugin note:** The code and config changes described here live in the
development repo. The active Hermes plugin at
`~/.hermes/plugins/cortex/` is a separate Git checkout and must be updated
explicitly — see [Update](#update) section above.

Cron validation has one sharp edge: `HERMES_HOME` changes which cron store is
visible. A `cronjob list` from an arbitrary worker profile can be empty even when
the scheduler is fine. Prefer checking from the scheduler/home context by setting
`HOME` to the account or directory where the scheduler stores cron jobs:

```bash
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cron list
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cortex cron status
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cortex cron status --job weekly
```

## Graph viewer

Cortex can generate a standalone D3 graph viewer for the vault. No server or
frontend build step is required.

```bash
hermes cortex graph build
hermes cortex graph viewer -o graph.html --embed-data --diagnostics
python3 -m http.server 8765
```

Open <http://localhost:8765/graph.html>.

Useful variants:

```bash
# Keep graph data in a separate JSON file
hermes cortex graph export --format d3-json -o graph_data.json --diagnostics
hermes cortex graph viewer -o graph.html --data graph_data.json

# Focus on one neighborhood
hermes cortex graph export --format d3-json \
  --neighborhood "30_projects/Project - hermes-cortex.md" \
  -o cortex_neighborhood.json
```

The viewer includes search, node/edge filters, selected-node detail, neighborhood
focus, force-layout sliders, and optional diagnostics for broken/ambiguous links
and orphan nodes.

## Configuration

Default config path:

```text
~/.hermes/cortex/config.yaml
```

For discovery/debugging, prefer the CLI over guessing paths:

```bash
hermes cortex config path
hermes cortex config show
hermes cortex status
```

`~/.hermes/plugins/cortex/` is the Git-managed plugin/code checkout. Keep user
config and rebuildable state (`chunks.jsonl`, Chroma, graph artifacts) under
`~/.hermes/cortex/` so runtime updates do not dirty the plugin repo.

Minimal shape:

```yaml
vault:
  path: ~/hermes-workspace/vault
search:
  top_k: 20
  bm25_weight: 0.5
  vector_weight: 0.5
  graph_weight: 0.2
context_builder:
  token_budget: 4000
  include_hermes_memory: false
  include_static_files: []
hooks:
  cache_warm:
    enabled: true
  skill_context:
    enabled: true
    when: each_turn
    load_skill: true
    skill_path: ""
    budget: 1000
  bootstrap_context:
    enabled: true
    when: first_turn
    budget: 1000
    include_static_files: []
  recent_context:
    enabled: false
    when: first_turn
    source: disabled_placeholder
  dynamic_context:
    enabled: false
    when: each_turn
    budget: 1000
    query: ""
  context_injection:
    enabled: false
    budget: 1000
    query: ""
    load_skill: false
```

`skill_context` is the each-turn runtime rules channel. `bootstrap_context`
is the first-turn static bootstrap channel, with deterministic
`include_static_files` ordering. `dynamic_context` stays off by default and only
injects Vault context when explicitly enabled. `context_injection` remains a
legacy compatibility fallback; new configs should prefer the semantic blocks.

`HERMES_HOME` is respected. Worker profiles therefore use their own Cortex config:

```text
~/.hermes/profiles/<profile>/cortex/config.yaml
```

Create worker profiles with cloned skills/tools when they need vault access:

```bash
hermes profile create researcher --clone-all
hermes -p researcher tools enable cortex
```

## Vault metadata

Cortex reads YAML frontmatter for filtering, boosting, and graph diagnostics.
The short version:

```yaml
---
type: fact | decision | project | runbook
status: active | archived | draft | superseded
tags: [memory, retrieval]
confidence: high        # or numeric 0..1
importance: medium      # or numeric 1..5
stability: stable | evolving | deprecated
---
```

Full schema: [`docs/METADATA.md`](docs/METADATA.md).

## Troubleshooting

### Tools are missing in chat

Hermes caches tools per session. Start a new session or use `/reset`.

Also check:

```bash
hermes plugins list
hermes tools list
```

### Search returns stale or weird results

Rebuild all artifacts:

```bash
hermes cortex index --force
hermes cortex embed --force
hermes cortex graph build --force
```

### A known note does not show up

Use a higher `top_k` first. Short wikilink/index chunks can outrank the target
note in small result windows.

```bash
hermes cortex search "exact note title" --top-k 30
```

If it still does not appear, verify the file exists in the vault and rebuild the
index. Annoying, but empirical.

## Development

```bash
cd ~/hermes-workspace/hermes-cortex
./install.sh --dev
source .venv/bin/activate
ruff check .
pytest
python -m build --no-isolation
```

The GitHub Actions workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
runs the same basic release gate on pull requests and pushes to `main`: install the
package with development dependencies, lint with Ruff, run the pytest suite, and
build the Python package on Python 3.11 and 3.12.

Development happens in the source checkout. Runtime uses the dedicated plugin
checkout:

| Role | Path |
|---|---|
| Development source | `~/hermes-workspace/hermes-cortex/` |
| Hermes runtime plugin | `~/.hermes/plugins/cortex/` |
| Vault | `~/hermes-workspace/vault/` |

## Release preflight

Before making the repository public or cutting a release, run the same checks CI
runs from a clean checkout:

```bash
ruff check .
pytest
python -m build --no-isolation
git diff --check
git grep -n "gitlab.skynet\|skynet-node\|/home/dennis\|jarvis@\|/opt/jarvis\|Dennis" -- . ':!README.md'
```

The public repository URL used by install docs, helper scripts, and the CI badge
is:

```text
https://github.com/Mugen0815/hermes-cortex.git
```

Keep personal paths, private remotes, private mail addresses, and local delivery
targets out of tracked defaults, examples, seed vault notes, tests, and docs.

Versioning is shared between the Python package metadata and the Hermes plugin
manifest. Keep these values in sync before tagging:

```text
pyproject.toml  → [project].version
plugin.yaml     → version
```

Packaging note: the supported Hermes installation path is a Git checkout under
`~/.hermes/plugins/cortex/`, because Hermes reads `plugin.yaml`, `__init__.py`, and
`plugin_runtime.py` from the checkout root. The Python package build is still
validated so the `cortex` library and CLI remain installable and dependency issues
are caught early; publishing to PyPI is a separate decision, not required for the
standalone Hermes plugin workflow.

## Repository layout

```text
.
├── __init__.py          # Hermes directory-plugin entry point
├── plugin.yaml          # Plugin manifest
├── plugin_runtime.py    # Tool/hook/CLI registration
├── cortex/              # CLI, indexing, search, graph, lifecycle code
├── skills/              # Companion Hermes skills
├── tests/               # Pytest suite
└── docs/                # Architecture and vault metadata reference
```

## License

MIT — see [`LICENSE`](LICENSE).
