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
Three files outside the vault hold runtime memory that Hermes can inject into
sessions. `hermes-cortex` reads them as complementary context but does not
index or modify them by default.

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
- New configs prefer `hooks.bootstrap_context.include_static_files` for
  deterministic bootstrap ordering and labels.
- `context_builder.include_hermes_memory` remains a legacy compatibility switch
  for older configs.
- Missing files are only skipped when the corresponding include is optional.

## Related
- [[Jarvis Memory Architecture]]
