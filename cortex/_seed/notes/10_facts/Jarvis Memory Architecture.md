---
type: fact
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: memory
project: hermes-cortex
tags: [memory, architecture, hermes, obsidian]
aliases: [Memory Layers, Memory Model]
source: session
related:
  - '[[Hermes Memory Files]]'
  - '[[Vault Schema]]'
  - '[[Project - hermes-cortex]]'
confidence: high
importance: high
stability: stable
---

# Jarvis Memory Architecture

## Summary
Jarvis uses a four-layer memory model: short injected memory for runtime
coordinates, a curated Obsidian vault for durable knowledge, raw session
transcripts for lookup, and a versioned infra repo for code/config.

## Layers

| Layer | Purpose | Location |
|---|---|---|
| Injected memory | Short working memory; runtime facts and coordinates | `~/.hermes/memories/MEMORY.md` |
| User profile | Who the user is, preferences | `~/.hermes/memories/USER.md` |
| Persona | Agent character, rules | `~/.hermes/SOUL.md` |
| Curated vault | Durable knowledge, structured | `~/hermes-workspace/vault/` |
| Sessions | Raw transcripts for lookup | `~/.hermes/sessions/` |
| Homebase | Optional versioned infra (scripts/docs/config) | user-defined path |

## Why it matters
Each layer has a different read/write cadence and different trust model.
Mixing them was the original failure mode — facts got shoved into injected
memory until it became a junk drawer.

## Rules
- Live chat reads memory; it does not rewrite it.
- Obsidian is the source of truth for durable knowledge.
- Vector/BM25 indices are caches — rebuildable, never authoritative.
- Daily/weekly jobs curate; live sessions consume.

## Related
- [[Hermes Memory Files]]
- [[Project - hermes-cortex]]
