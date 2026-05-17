---
type: fact
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: memory
project: hermes-cortex
tags: [hermes, memory, persona]
aliases: [MEMORY.md, USER.md, SOUL.md]
source: manual
related:
  - '[[Jarvis Memory Architecture]]'
confidence: high
importance: high
stability: stable
---

# Hermes Memory Files

## Summary
Three files outside the vault hold runtime memory that Hermes injects into
every session. `hermes-cortex` reads them as complementary context but does
not index or modify them.

## Files

| File | Purpose | Path |
|---|---|---|
| `MEMORY.md` | Short working memory: runtime coordinates, environment facts | `~/.hermes/memories/MEMORY.md` |
| `USER.md`   | User profile: who the user is, preferences, communication style | `~/.hermes/memories/USER.md` |
| `SOUL.md`   | Agent persona: character, rules, voice | `~/.hermes/SOUL.md` |

## Why separate from the vault
- These files are injected into **every** session — they must stay compact.
- They describe the agent and the user, not the world.
- `SOUL.md` is identity, not knowledge; mixing them muddies retrieval.
- The vault is for durable, retrievable, citable knowledge.

## Cortex Integration
- `context.py` may prepend any/all of these to a context pack when relevant.
- Configurable via `context_builder.include_hermes_memory` in `config.yaml`.
- Missing files are skipped silently.

## Related
- [[Jarvis Memory Architecture]]
