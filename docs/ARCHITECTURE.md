# Architecture

`hermes-cortex` is a local retrieval layer for Hermes Agent. The Markdown vault
is the source of truth; generated artifacts are rebuildable caches.

## Data flow

```text
Markdown vault (*.md)
  │
  ▼
indexer.py
  ├─ parses YAML frontmatter
  ├─ chunks notes by headings
  ├─ extracts wikilinks
  └─ writes ~/.hermes/cortex/chunks.jsonl
  │
  ├──────────────┐
  ▼              ▼
embedder.py      graph_index.py
  │              │
  ▼              ▼
Chroma           graph_nodes.jsonl / graph_edges.jsonl / graph_broken.jsonl
  │              │
  └──────┬───────┘
         ▼
search.py
  ├─ BM25 lexical ranking
  ├─ vector ranking
  ├─ wikilink graph expansion
  ├─ metadata filters
  └─ RRF fusion + recency/importance boosts
         │
         ▼
context.py
  └─ token-budgeted Markdown context with citations
         │
         ▼
Hermes tools
  ├─ vault_search
  ├─ vault_read_note
  └─ vault_build_context
```

## Runtime integration

Hermes loads Cortex as a standalone directory plugin:

```text
~/.hermes/plugins/cortex/
├── plugin.yaml
├── __init__.py
├── plugin_runtime.py
└── cortex/
```

`plugin.yaml` declares the plugin. Hermes imports root `__init__.py`, which calls
`register(ctx)` and delegates tool, hook, and CLI registration to
`plugin_runtime.py`.

The active runtime source is the plugin checkout itself. Keep that checkout in sync
with the development repo via `git pull --ff-only origin main` inside
`~/.hermes/plugins/cortex/`; otherwise the `hermes cortex ...` command surface can
lag behind `python -m cortex.cli ...`. The runtime smoke check is:

```bash
scripts/smoke-runtime-cortex-cli.sh
```

## Knowledge sources

Cortex distinguishes between indexed vault knowledge and Hermes runtime memory.

| Source | Indexed? | Purpose |
|---|---:|---|
| Markdown vault | Yes | Durable facts, decisions, projects, runbooks |
| `~/.hermes/memories/MEMORY.md` | No | Compact runtime facts |
| `~/.hermes/memories/USER.md` | No | User preferences/profile |
| `~/.hermes/SOUL.md` | No | Agent persona/rules |

The vault is embedded and searched. Hermes memory files may be included as
static bootstrap context via `hooks.bootstrap_context.include_static_files`
(preferred) or the legacy `context_builder.include_hermes_memory` switch. They
are read as context only; they are not copied into the vault or vector store.

Runtime hook context is split semantically:

- `skill_context` — each-turn runtime rules and skill bootstrap
- `bootstrap_context` — first-turn static context, including deterministic
  `include_static_files`
- `recent_context` — disabled placeholder in this cutover; no SessionDB/topic
  condenser here
- `dynamic_context` — gated/off-by-default user-message Vault context

New semantic blocks take precedence over the legacy `hooks.context_injection`
block. Legacy configs still parse for compatibility, but they are treated as a
fallback path, not the primary model.

## Nightly/session promotion

NightlyPromotion uses Hermes SessionDB as the primary session source: it reads
`~/.hermes/state.db` first, falls back to legacy JSON/JSONL session files only
when the database is missing, unreadable, schema-incompatible, or otherwise
fails the documented selection rules, and ignores `request_dump_*.json` even if
the file glob would match them.

Nightly report payloads should make the source choice visible: which backend was
primary, how many sessions each backend saw, whether fallback was used, the
fallback reason, the ignored request-dump count, and the lookback cutoff/
timezone. No empty-file-glob story is acceptable if state.db actually contains
recent sessions.

## Main components

| Component | Role |
|---|---|
| `cortex.config` | Loads and validates Cortex config |
| `cortex.indexer` | Parses Markdown and writes `chunks.jsonl` |
| `cortex.embedder` | Computes embeddings and stores them in Chroma |
| `cortex.search` | Hybrid search and ranked result fusion |
| `cortex.graph_index` | Builds graph artifacts from notes and wikilinks |
| `cortex.graph_diagnostics` | Reports broken links, orphans, hubs, stale notes |
| `cortex.context` | Builds cited Markdown context under a token budget |
| `cortex.plugin` | Pure Python tool API used by Hermes integration |
| `plugin_runtime.py` | Hermes plugin registration layer |
| `cortex.cli` | Standalone and Hermes-routed CLI commands |

## Search model

Search combines three channels:

1. **BM25** — exact/lexical matches over normalized chunk text
2. **Vector** — semantic matches from Chroma embeddings
3. **Graph** — nearby chunks via wikilink expansion

Ranks are fused with weighted reciprocal rank fusion (RRF). Optional recency and
importance boosts are applied after fusion. Missing metadata stays neutral; it is
not treated as medium importance by accident.

## CLI guardrails

Two operator-facing commands are intentionally read-only or diagnostic-only:

- `cortex validate-frontmatter` / `hermes cortex validate-frontmatter`
  - validates YAML frontmatter and vault metadata
  - exits `1` on validation errors, or on warnings only when `--strict` is set
  - `--json` emits a stable report with `schema_version`, counts, and per-file issues
  - `--path` scopes validation to explicit notes or directories inside the vault
- `cortex search-eval` / `hermes cortex search-eval`
  - runs fixed real-vault ranking cases
  - `--output` writes the JSON report, `--json` prints the same payload
  - `--baseline` adds compare metadata and rank deltas; update baselines only after
    `cortex validate-frontmatter` and `--lint-vault-files` pass and index/embed
    maintenance has run
  - report cases include `final_score`, `rrf_score`, channel ranks, boost fields,
    and baseline comparison fields when a baseline is provided

## Rebuild policy

Generated artifacts are disposable:

```bash
cortex index --force
cortex embed --force
cortex graph build --force
```

If search behavior is suspicious, rebuild before theorizing. Machines enjoy
humbling us.
