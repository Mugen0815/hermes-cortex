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
```

## What does NOT live here

These belong to Hermes itself, not to the curated knowledge base:

| File | Purpose | Path |
|---|---|---|
| `MEMORY.md` | Injected short working memory (runtime facts, coordinates) | `~/.hermes/memories/MEMORY.md` |
| `USER.md`   | Who the user is, preferences | `~/.hermes/memories/USER.md` |
| `SOUL.md`   | Agent persona, character, rules | `~/.hermes/SOUL.md` |

`hermes-cortex` reads these as **complementary context** during retrieval, but does not store them in the vault.

## Note Schema

Every note uses YAML frontmatter — see `80_templates/` in the initialized vault and the cortex docs:
- `docs/METADATA.md` in the cortex repo
- Required fields: `type`, `status`, `created`, `updated`, `tags`, `confidence`, `importance`, `stability`

## Conventions

- One topic per note. Split early.
- Use `[[Wikilinks]]` generously — they feed the retrieval graph.
- Prefer short, declarative statements over prose.
- Keep `last_verified` honest. If you didn't check, don't bump it.
