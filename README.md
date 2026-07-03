# hermes-cortex

[![Hermes Agent](https://img.shields.io/badge/Hermes-Agent-14130f?style=flat-square)](https://hermes-agent.nousresearch.com)
[![CI](https://github.com/Mugen0815/hermes-cortex/actions/workflows/ci.yml/badge.svg)](https://github.com/Mugen0815/hermes-cortex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square)](./LICENSE)

Cortex-backed vault memory for [Hermes Agent](https://hermes-agent.nousresearch.com).

`hermes-cortex` indexes a Markdown / Obsidian-style vault and exposes it to
Hermes as tools, lifecycle hooks, and an operator CLI. The normal user-facing
command surface is `hermes cortex ...` once the plugin is installed and enabled.

## What this is

- A standalone Hermes plugin loaded from `~/.hermes/plugins/cortex/`
- A local vault indexer for Markdown notes
- Hybrid retrieval: BM25 + vector embeddings + wikilink graph expansion
- Hermes tools: `vault_search`, `vault_read_note`, `vault_build_context`
- A maintenance CLI for indexing, embeddings, graph artifacts, lifecycle checks,
  cron packaging, and graph viewer generation

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
       ├─ hooks: skill/bootstrap/recent/dynamic context helpers
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

Initialize the vault config and build retrieval artifacts:

```bash
hermes cortex init --yes
hermes cortex index
hermes cortex embed
hermes cortex graph build
```

Fresh init is idempotent and does not mutate `SOUL.md`, `MEMORY.md`, or
`USER.md` by default. Legacy memory-file mutation is opt-in only through explicit
`hermes cortex init` flags.

Cortex has exactly one durable runtime Vault source of truth: `vault.path` in the
active Cortex config. During `init`, an explicit `--vault` path wins; otherwise an
existing config wins; only then may `WIKI_PATH` seed the initial `vault.path` for
new/non-configured installs. Runtime commands, hooks, search, indexing, and
lifecycle operations use the configured `vault.path`, not `WIKI_PATH`, `wiki.path`,
or a fallback wiki directory.

Fresh Vaults also include llm-wiki-compatible root/orientation material in that
same Vault root: `SCHEMA.md`, `index.md`, `log.md`, and `raw/{articles,papers,transcripts,assets}`.
`raw/`, `00_inbox/`, and `80_templates/` are excluded from curated answer indexing
by default unless an operator explicitly opts into a diagnostic/source workflow.

Start a new Hermes session after first install. Existing sessions cache their
tool list, so `/reset` or a full restart is the boring but correct fix.

## Verify

```bash
hermes plugins list
hermes tools list
hermes cortex --help
hermes cortex status
hermes cortex search "memory-query-flow" --top-k 3
scripts/smoke-runtime-cortex-cli.sh
```

Expected:

- plugin `cortex` is enabled
- toolset `cortex` exposes `vault_search`, `vault_read_note`, `vault_build_context`
- `hermes cortex --help` lists the Cortex CLI commands
- `hermes cortex search ...` returns vault results after `index` + `embed`
- `scripts/smoke-runtime-cortex-cli.sh` confirms the eval JSON envelope for
  `hermes cortex search-eval --json --allow-failures`

## Quick start

```bash
# Search the vault from the shell
hermes cortex search "project architecture" --top-k 10

# Build cited Markdown context for an LLM prompt
hermes cortex context "how does memory promotion work?" --budget 4000

# Validate vault metadata
hermes cortex validate-frontmatter --strict

# Check the Cortex/llm-wiki Vault contract without mutating files
hermes cortex wiki-health --strict

# Rebuild all derived artifacts
hermes cortex lifecycle maintenance
```

Inside Hermes, use the plugin tools through the `cortex` toolset:

| Tool | Purpose |
|---|---|
| `vault_search` | Hybrid search over indexed notes |
| `vault_read_note` | Read a full note, optionally limited to a heading path |
| `vault_build_context` | Build a cited Markdown context blob under a token budget |

## Common commands

| Command | Purpose |
|---|---|
| `hermes cortex init --yes` | Create config, vault folders, and seed notes using defaults |
| `hermes cortex init --dry-run` | Preview init actions without writing files |
| `hermes cortex index [--force]` | Chunk vault notes into `chunks.jsonl` |
| `hermes cortex embed [--force]` | Build/update Chroma embeddings |
| `hermes cortex search "query" --top-k 20` | Search the vault from the shell |
| `hermes cortex context "query" --budget 4000` | Build cited Markdown context |
| `hermes cortex validate-frontmatter --strict` | Validate vault note metadata |
| `hermes cortex wiki-health [--strict]` | Read-only check for llm-wiki root/raw material and curated-source drift |
| `hermes cortex status` | Show plugin/code path, config, vault, index, graph, and hook status |
| `hermes cortex config path` | Print the active Cortex config path |
| `hermes cortex config show` | Print effective config and hook lifecycle summary |
| `hermes cortex graph build [--force]` | Build wikilink graph artifacts |
| `hermes cortex graph status` | Show graph health and diagnostics |
| `hermes cortex graph broken` | List unresolved or ambiguous links |
| `hermes cortex graph export --format d3-json -o graph_data.json` | Export graph data |
| `hermes cortex graph viewer -o graph.html --embed-data` | Generate a static graph viewer |
| `hermes cortex lifecycle maintenance` | Run index → embed → graph build |
| `hermes cortex lifecycle nightly --dry-run` | Preview explicit `00_inbox/` promotion/cleanup |
| `hermes cortex lifecycle weekly --dry-run` | Print a read-only WeeklyReview report |
| `hermes cortex session-sources --lookback-days 1` | Inspect recent Hermes sessions for promotion inputs |
| `hermes cortex cron status` | Check all installed Cortex cron jobs |
| `hermes cortex cron install --job nightly` | Install/update the configured nightly promotion job |
| `hermes cortex cron install --job weekly` | Install/update the configured weekly review job |
| `hermes cortex reset --all --yes` | Delete rebuildable chunks/vector state |

Full command reference: [`docs/CLI.md`](docs/CLI.md).

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
  include_folders:
    - 10_facts
    - 20_decisions
    - 30_projects
    - 40_runbooks
    - 50_people
    - 60_maps
  exclude_folders:
    - 00_inbox
    - 80_templates
    - raw
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
    source: sessiondb
    budget: 1000
    lookback_days: 7
    max_sessions: 500
    max_groups: 8
    include_sources: []
    exclude_sources: [cron, api_server]
    diagnostics: true
    query_hint: false
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

`skill_context` is the each-turn runtime rules channel. `bootstrap_context` is
the first-turn static bootstrap channel. `recent_context` can inject
deterministic recent-session metadata from Hermes SessionDB. `dynamic_context`
stays off by default and only injects Vault context when explicitly enabled.
`context_injection` is a legacy compatibility fallback; new configs should prefer
the semantic hook blocks above.

`HERMES_HOME` is respected. Worker profiles therefore use their own Cortex config:

```text
~/.hermes/profiles/<profile>/cortex/config.yaml
```

Create worker profiles with cloned skills/tools when they need vault access:

```bash
hermes profile create researcher --clone-all
hermes -p researcher tools enable cortex
```

## Hook lifecycle status

`hermes cortex config show` and `hermes cortex status` print an operator-facing
hook lifecycle table. It separates lifecycle phases from legacy compatibility
fields:

- `cache_warm` runs at `session_start` and only warms process-local cache.
- `skill_bootstrap`, `static_file_bootstrap`, `recent_context`, and
  `dynamic_context` describe `pre_llm` hook context.
- `recent_context` reads session titles/sources/timestamps only, never transcript
  message bodies and never calls an LLM. It defaults to `source: sessiondb` with
  `cron` and `api_server` excluded.
- `legacy_context_injection` is displayed for compatibility. In a legacy-only
  config it is `legacy-active`; when semantic hook blocks are configured it is
  `legacy-ignored`.

The lifecycle table describes behavior for sessions/processes that loaded the
current plugin code and config. Already-running TUI sessions, gateway processes,
and Kanban workers may retain older hook code/config until a new session/process
starts or an operator-approved restart is performed.

## Embeddings and cache behavior

Cortex uses SentenceTransformer embeddings and stores vector data in Chroma.
Cache/reuse behavior is explicit and visible:

- `embeddings.cache_folder` controls the SentenceTransformer cache path.
- `embeddings.local_files_only` defaults to `auto`; warm caches can be reused
  without network access, while explicit `true` forces offline-only behavior.
- `embedding_manifest.json` sits next to the Chroma store and records model/dim
  metadata for skip decisions.
- `hermes cortex embed` and `hermes cortex status` report whether the model was
  reused and which cache mode is active.
- `HF_TOKEN` is not required; token values are not logged.

## Nightly promotion and WeeklyReview

Cortex can package Hermes cron jobs for vault maintenance workflows. Repo defaults
are public-safe and do not install jobs unless you explicitly opt in through
config and `hermes cortex cron install`.

NightlyPromotion is intended to promote durable session knowledge into the vault.
WeeklyReview is read-only and reports graph/metadata hygiene issues such as
broken references, stale high-importance notes, or consolidation candidates.

Useful commands:

```bash
hermes cortex cron status
hermes cortex cron install --job nightly
hermes cortex cron install --job weekly
hermes cortex cron uninstall --job weekly
```

Job config lives under `cron.nightly_promotion` and `cron.weekly_review` in the
active Cortex config. Keep personal delivery targets in private local config, not
in tracked defaults, examples, seed notes, tests, or docs.

Cron validation has one sharp edge: `HERMES_HOME` changes which cron store is
visible. A check from an arbitrary worker profile can be empty even when the
scheduler is fine. Prefer checking from the scheduler/home context when in doubt:

```bash
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cron list
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cortex cron status
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

The viewer includes search, node/edge filters, selected-node detail,
neighborhood focus, force-layout sliders, and optional diagnostics for
broken/ambiguous links and orphan nodes.

## Vault metadata

Cortex reads YAML frontmatter for filtering, boosting, and graph diagnostics.
The short version:

```yaml
---
type: fact | decision | project | runbook | map | person | note
status: active | draft | archived | deprecated | stale | superseded
tags: [memory, retrieval]
confidence: high        # or numeric 0..1
importance: medium      # or numeric 1..5
stability: stable | evolving | experimental
---
```

Full schema and canonical enum tables: [`docs/METADATA.md`](docs/METADATA.md).

## Update

Runtime updates are Git-only. No copy, rsync, generated stub, or symlink farm.
Keep your development checkout and active runtime plugin checkout separate; a
commit in a development clone does not affect the plugin Hermes is currently
loading until the runtime checkout is pulled.

```bash
cd ~/.hermes/plugins/cortex
git fetch origin --prune
git pull --ff-only origin main
./install.sh --with-hermes-venv --with-hermes-skills
scripts/smoke-runtime-cortex-cli.sh
```

Then start a new Hermes session or `/reset` the current one. Restart the gateway
only when runtime code or hooks changed and gateway workers need to load the new
code. Pure README/docs changes do not require a restart.

## Troubleshooting

### Tools are missing in chat

Hermes caches tools per session. Start a new session or use `/reset`.

Also check:

```bash
hermes plugins list
hermes tools list
hermes cortex status
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

## Repository layout

```text
.
├── __init__.py          # Hermes directory-plugin entry point
├── plugin.yaml          # Plugin manifest
├── plugin_runtime.py    # Tool/hook/CLI registration
├── cortex/              # CLI, indexing, search, graph, lifecycle code
├── skills/              # Companion Hermes skills
├── tests/               # Pytest suite
└── docs/                # Architecture, CLI, development, and metadata docs
```

## License

MIT — see [`LICENSE`](LICENSE).
