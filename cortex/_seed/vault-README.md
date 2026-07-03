# Vault README

This vault is managed by [`hermes-cortex`](https://github.com/Mugen0815/hermes-cortex).

## What lives here

Curated, durable knowledge — organized for retrieval:

```
00_inbox/       review-only candidates that need human judgment
10_facts/       stable facts about systems, tools, environments
20_decisions/   decisions + rationale
30_projects/    active project notes
40_runbooks/    repeatable procedures and troubleshooting
50_people/      people, contacts, relationships
60_maps/        index/overview notes (MOC = Map of Content)
80_templates/   note templates (not indexed)
raw/           immutable source/provenance material (not indexed as curated answers)
```

Fresh Cortex Vaults also include llm-wiki-compatible root files in this same
configured Vault root:

```text
SCHEMA.md      schema/conventions and provenance rules
index.md       operator-facing Vault catalog/map
log.md         append-only operator/lifecycle history
raw/articles/  web/article source captures
raw/papers/    papers and PDFs
raw/transcripts/ transcripts and meeting/source notes
raw/assets/    images and referenced binary assets
```

`vault.path` in the active Cortex config is the only durable runtime location for
this Vault. `WIKI_PATH` may seed `vault.path` during init for otherwise
unconfigured installs, but runtime commands and hooks use the configured path.

## What does NOT live here

These belong to Hermes itself, not to the curated knowledge base:

| File | Purpose | Path |
|---|---|---|
| `MEMORY.md` | Injected short working memory (runtime facts, coordinates) | `~/.hermes/memories/MEMORY.md` |
| `USER.md`   | Who the user is, preferences | `~/.hermes/memories/USER.md` |
| `SOUL.md`   | Agent persona, character, rules | `~/.hermes/SOUL.md` |

`hermes-cortex` reads these as **complementary context** during retrieval, but
does not store them in the vault. Fresh init keeps them read-only by default;
new configs should prefer the semantic hook blocks and static-file bootstrap
path over the old blanket `context_builder.include_hermes_memory` switch.

## Note Schema

Every note uses YAML frontmatter — see `80_templates/` in the initialized vault and the cortex docs:
- `docs/METADATA.md` in the cortex repo
- Required fields: `type`, `status`, `created`, `updated`, `tags`, `confidence`, `importance`, `stability`

## Conventions

- One topic per note. Split early.
- Use `[[Wikilinks]]` generously — they feed the retrieval graph.
- Prefer short, declarative statements over prose.
- Keep `last_verified` honest. If you didn't check, don't bump it.
- Keep raw source files immutable. Corrections and synthesis belong in curated notes.
- Use `hermes cortex wiki-health` for read-only checks of root files, raw folders,
  curated-source exclusions, and raw source drift.
