# Cortex CLI reference

This page documents the user-facing Cortex CLI as exposed through Hermes:

```bash
hermes cortex ...
```

Operator documentation uses `hermes cortex ...` exclusively because that is the command surface registered by the Hermes plugin.

## Top-level commands

```text
hermes cortex init
hermes cortex index
hermes cortex embed
hermes cortex search
hermes cortex search-eval
hermes cortex validate-frontmatter
hermes cortex wiki-health
hermes cortex context
hermes cortex graph
hermes cortex config
hermes cortex status
hermes cortex lifecycle
hermes cortex session-sources
hermes cortex cron
hermes cortex reset
```

Use `--help` on any command for the authoritative current options:

```bash
hermes cortex --help
hermes cortex search --help
hermes cortex graph --help
```

## Init

Create the Cortex config, vault folders, templates, and seed notes.

```bash
hermes cortex init --yes
hermes cortex init --dry-run
hermes cortex init --vault ~/hermes-workspace/vault --config ~/.hermes/cortex/config.yaml
```

Options:

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Non-interactive, accept defaults |
| `--dry-run` | Show actions without writing files |
| `--vault PATH` | Select the Vault path during init. With an existing config, a mismatching non-interactive `--yes --vault` run fails before seeding instead of creating a second Vault. |
| `--config PATH` | Override config path |
| `--legacy-update-hermes-memory` | Opt in to updating Hermes `MEMORY.md` vault coordinates |
| `--legacy-update-soul-memory-rules` | Opt in to patching `SOUL.md` with Cortex memory rules |

By default, init does not mutate `SOUL.md`, `MEMORY.md`, or `USER.md`.

`vault.path` in the active Cortex config is the only durable runtime Vault source
of truth. Init path selection is deliberately one-way:

1. explicit `--vault PATH` or interactive operator input;
2. existing config `vault.path` when no explicit path was supplied;
3. `WIKI_PATH` only as an init/default input for new, otherwise unconfigured installs;
4. the built-in default `~/hermes-workspace/vault`.

After init, commands such as `index`, `embed`, `search`, hooks, and lifecycle use
configured `vault.path`. They do not fall back to `WIKI_PATH`, `wiki.path`, or a
second wiki directory.

For non-interactive runs, Cortex refuses split-brain init state: `init --yes
--config existing.yaml --vault NEW` fails before seed writes when
`existing.yaml` already points at a different `vault.path` and the config update
would be skipped. Re-run interactively to choose an overwrite policy or update the
config manually.

## Index

Build or update `chunks.jsonl` from the configured vault.

```bash
hermes cortex index
hermes cortex index --force
hermes cortex index --config /path/to/config.yaml
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--force` | Re-index all files, ignoring hashes |

There is no per-call `--path` override. The vault path comes from config.

## Embed

Compute embeddings and upsert chunks into Chroma.

```bash
hermes cortex embed
hermes cortex embed --force
hermes cortex embed --batch-size 64
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--force` | Re-embed all chunks |
| `--batch-size N` | Embedding batch size; default is `32` |

## Search

Run hybrid search over the indexed vault.

```bash
hermes cortex search "memory promotion" --top-k 20
hermes cortex search "memory promotion" --type fact,decision --status active
hermes cortex search "memory promotion" --tags-any memory,retrieval --json
hermes cortex search "memory promotion" --no-boost
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--top-k N` | Number of results; default comes from config |
| `--no-boost` | Disable recency/importance boosts for this call |
| `--json` | Print JSON instead of human-readable text |
| `--type CSV` | Filter by frontmatter `type`; OR within field |
| `--status CSV` | Filter by frontmatter `status` |
| `--domain CSV` | Filter by frontmatter `domain` |
| `--project CSV` | Filter by frontmatter `project` |
| `--folder CSV` | Filter by top-level vault folder |
| `--tags-any CSV` | Match any listed tag |
| `--tags-all CSV` | Match all listed tags |
| `--wikilinks-any CSV` | Match any listed wikilink target |
| `--wikilinks-all CSV` | Match all listed wikilink targets |
| `--importance-min N` / `--importance-max N` | Filter by importance range, `1..5` |
| `--confidence-min N` / `--confidence-max N` | Filter by confidence range, `0..1` |
| `--modified-after YYYY-MM-DD` | Include files modified on/after the date |
| `--modified-before YYYY-MM-DD` | Include files modified on/before the date |

## Search eval

Run fixed ranking evaluation cases and emit diagnostics.

```bash
hermes cortex search-eval --top-k 20
hermes cortex search-eval --json --allow-failures
hermes cortex search-eval --output search-eval-report.json
hermes cortex search-eval --baseline previous-report.json --output new-report.json
hermes cortex search-eval --lint-vault-files
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--cases PATH` | YAML eval cases; default is `tests/fixtures/search_eval_cases.yaml` |
| `--top-k N` | Results per case; default is `10` |
| `--baseline PATH` | Previous JSON report to compare against |
| `--output PATH`, `-o PATH` | Write JSON report to this path |
| `--json` | Print full JSON report |
| `--no-unboosted` | Skip boosted-vs-unboosted rank comparison |
| `--lint-vault-files` | Fail if expected files are absent from the configured vault |
| `--allow-failures` | Exit 0 even if expected ranks miss thresholds |

## Validate frontmatter

Validate vault note metadata without modifying files.

```bash
hermes cortex validate-frontmatter
hermes cortex validate-frontmatter --strict
hermes cortex validate-frontmatter --json
hermes cortex validate-frontmatter --path '30_projects/Project - hermes-cortex.md'
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--path REL_OR_ABS ...` | Validate specific notes/directories; relative paths resolve under the vault |
| `--json` | Print JSON instead of human-readable text |
| `--strict` | Treat warnings as non-zero exit |

Default exit behavior: errors return exit code `1`; warnings only return `0`
unless `--strict` is set.

## Wiki health

Check the configured Vault against the Cortex/llm-wiki single-Vault contract
without modifying files.

```bash
hermes cortex wiki-health
hermes cortex wiki-health --strict
hermes cortex wiki-health --json
```

The report includes the active config path, `vault.path`, missing root files
(`SCHEMA.md`, `index.md`, `log.md`), missing raw folders
(`raw/articles`, `raw/papers`, `raw/transcripts`, `raw/assets`), missing Cortex
canonical folders, curated-source drift that would index `raw`, `00_inbox`, or
`80_templates`, and raw-source frontmatter/hash drift diagnostics when present.
Missing or non-file `log.md` is reported; runtime lifecycle commands do not create
it as application state. `log.md` is append-only operator/lifecycle history when
it exists.

`--strict` returns non-zero for warnings. `--json` emits deterministic JSON for
scripts and tests.

## Context

Search and build an LLM-injectable Markdown context blob under a token budget.

```bash
hermes cortex context "memory promotion" --budget 4000
hermes cortex context "memory promotion" --top-k 30 --no-hermes-memory
hermes cortex context "memory promotion" --type fact,runbook --json
```

Options are the same filter flags as `search`, plus:

| Option | Meaning |
|---|---|
| `--budget N` | Override token budget for this call |
| `--no-hermes-memory` | Do not include `MEMORY.md` / `USER.md` sections |
| `--json` | Output JSON diagnostics; default is raw Markdown |

## Graph

Build, inspect, export, and visualize the wikilink graph.

```bash
hermes cortex graph build
hermes cortex graph status
hermes cortex graph broken
hermes cortex graph orphans
hermes cortex graph centrality
hermes cortex graph stale
hermes cortex graph contradictions
hermes cortex graph export --format d3-json -o graph_data.json
hermes cortex graph viewer -o graph.html --embed-data
```

Subcommands:

| Command | Purpose |
|---|---|
| `graph build` | Build graph artifacts from `chunks.jsonl` |
| `graph status` | Show comprehensive graph status report |
| `graph broken` | List unresolved or ambiguous references |
| `graph orphans` | List orphan nodes with zero edges |
| `graph centrality` | Show centrality rankings |
| `graph stale` | List stale note candidates |
| `graph contradictions` | List contradiction edges |
| `graph export` | Export graph as JSON, D3 JSON, or Mermaid |
| `graph viewer` | Generate a standalone static D3 graph viewer |

All graph subcommands accept `--config PATH` at the `graph` level:

```bash
hermes cortex graph --config /path/to/config.yaml status
```

## Config and status

Inspect active config paths and effective settings.

```bash
hermes cortex config path
hermes cortex config show
hermes cortex status
hermes cortex status --config /path/to/config.yaml
```

Commands:

| Command | Purpose |
|---|---|
| `config path` | Print active Cortex config path |
| `config show` | Print effective config summary and hook lifecycle rows |
| `status` | Show plugin, config, vault, index, graph, and hook status |

## Lifecycle

Run maintenance and review/promotion workflows.

```bash
hermes cortex lifecycle maintenance
hermes cortex lifecycle maintenance --force
hermes cortex lifecycle maintenance --dry-run
hermes cortex lifecycle nightly --dry-run
hermes cortex lifecycle nightly --write
hermes cortex lifecycle weekly --dry-run
```

Subcommands:

| Command | Purpose |
|---|---|
| `lifecycle maintenance` | Run index → embed → graph build |
| `lifecycle nightly` | Run explicit inbox promotion workflow |
| `lifecycle weekly` | Run read-only graph review |

Options:

| Command | Option | Meaning |
|---|---|---|
| `maintenance` | `--force` | Force rebuild all steps |
| `maintenance` | `--dry-run` | Show what would change without writing |
| `nightly` | `--dry-run` | Show what would change without writing; default |
| `nightly` | `--write` | Apply eligible promotions atomically |
| `weekly` | `--dry-run` | Label output as dry-run; weekly never writes |
| `weekly` | `--stale-days N` | Days threshold for stale detection; default `180` |
| `weekly` | `--stale-min-importance N` | Minimum importance for stale review; default `4.0` |
| `weekly` | `--consolidation-min-degree N` | Minimum degree for consolidation proposals; default `3` |

## Session sources

Collect recent Hermes sessions from SessionDB with legacy file fallback. This is
mainly used by promotion automation and diagnostics.

```bash
hermes cortex session-sources --lookback-days 1
hermes cortex session-sources --lookback-days 7 --timezone Europe/Berlin
hermes cortex session-sources --state-db-path ~/.hermes/state.db
hermes cortex session-sources --session-glob '~/.hermes/sessions/*.jsonl'
hermes cortex session-sources --no-legacy-fallback
```

Options:

| Option | Meaning |
|---|---|
| `--lookback-days N` | Session lookback window; default `1` |
| `--timezone TZ` | Timezone for date calculations; default `Europe/Berlin` |
| `--state-db-path PATH` | Hermes state DB path; default `~/.hermes/state.db` |
| `--session-glob GLOB` | Legacy session-file glob; can be repeated |
| `--no-legacy-fallback` | Disable JSON/JSONL fallback when SessionDB is unavailable/empty |

## Cron

Install, uninstall, or inspect packaged Hermes cron jobs for Cortex workflows.

```bash
hermes cortex cron status
hermes cortex cron status --job nightly
hermes cortex cron status --job weekly
hermes cortex cron install --job nightly
hermes cortex cron install --job weekly
hermes cortex cron install --job all
hermes cortex cron uninstall --job weekly
```

Subcommands:

| Command | Purpose |
|---|---|
| `cron install` | Install/update a configured Cortex cron job |
| `cron uninstall` | Remove a configured Cortex cron job |
| `cron status` | Check whether Cortex cron jobs are installed |

Options:

| Command | Option | Meaning |
|---|---|---|
| `install` | `--config PATH` | Use an explicit Cortex config file |
| `install` | `--job nightly|weekly|all` | Job to install; default `nightly` |
| `install` | `--vault PATH` | Deprecated compatibility only; must match configured `vault.path` after normalization. Mismatching values are rejected before job build/save. The Cortex config `vault.path` is the single source of truth. |
| `uninstall` | `--config PATH` | Use an explicit Cortex config file |
| `uninstall` | `--job nightly|weekly|all` | Job to remove; default `nightly` |
| `status` | `--config PATH` | Use an explicit Cortex config file |
| `status` | `--job nightly|weekly|all` | Job to inspect; default `all` |

Cron jobs are controlled by `cron.nightly_promotion` and `cron.weekly_review` in
the active Cortex config. If `HERMES_HOME` points at a worker profile, `cron
status` may inspect that profile's cron store rather than the scheduler/home
store.

## Reset

Delete rebuildable derived state.

```bash
hermes cortex reset --chunks --yes
hermes cortex reset --chroma --yes
hermes cortex reset --all --yes
```

Options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use an explicit config file |
| `--chroma` | Delete the Chroma vector store |
| `--chunks` | Delete `chunks.jsonl` |
| `--all` | Delete both Chroma and chunks |
| `--yes`, `-y` | Skip confirmation prompt |

`reset` deletes derived state only. Rebuild with:

```bash
hermes cortex index
hermes cortex embed
hermes cortex graph build
```
