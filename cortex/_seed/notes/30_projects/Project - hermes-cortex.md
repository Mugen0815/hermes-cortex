---
type: project
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: memory
project: hermes-cortex
tags: [project, memory, retrieval, rag, obsidian]
aliases: [cortex]
source: session
related:
  - '[[Jarvis Memory Architecture]]'
  - '[[Vault Schema]]'
  - '[[Hermes Memory Files]]'
confidence: high
importance: high
stability: evolving
---

# Project - hermes-cortex

## Goal
Cognitive architecture and memory lifecycle for Hermes-based AI assistants.
Knowledge in (promotion) → stored (Obsidian) → found (hybrid retrieval) →
used (context builder) → improved (review/feedback).

## Status
Phase 0 — Foundation. Repo scaffolded, schema defined, config loader done
with 8 tests passing. Vault skeleton at `~/hermes-workspace/vault/`.

## Architecture / Approach
- **Obsidian** = source of truth
- **Vector + BM25** = hybrid search index (cache, rebuildable)
- **Tools** (LLM-invoked): `vault_search`, `vault_read_note`, `vault_build_context`
- **Plugins** (background): SessionCapture, NightlyPromotion, WeeklyReview, IndexMaintenance, ...
- Hermes memory files (`MEMORY.md`, `USER.md`, `SOUL.md`) read as context, not indexed.

## Key Paths
- Repo: `https://github.com/Mugen0815/hermes-cortex.git`
- Local checkout: `~/hermes-workspace/hermes-cortex/`
- Vault: `~/hermes-workspace/vault/`
- Docs: `hermes-cortex/docs/{ARCHITECTURE,METADATA,ROADMAP}.md`

## Stack
| Component | Choice |
|---|---|
| Embeddings | `all-MiniLM-L6-v2` (local) |
| Vector store | Chroma |
| BM25 | `rank_bm25` |
| Reranking | Reciprocal Rank Fusion (RRF) |
| Wikilinks | 1-hop traversal |

## Open Questions
- [x] Repo structure moved from `src/cortex/` to root `cortex/` so the repository can run as a standalone Hermes directory plugin checkout.
- [ ] Weekly review: fully automated with optional manual review output

## Related
- [[Jarvis Memory Architecture]]
- [[Vault Schema]]
- [[Hermes Memory Files]]
