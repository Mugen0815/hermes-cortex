---
type: fact
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: infrastructure
project:
tags: [workspace, layout, conventions]
aliases: [Workspace, hermes-workspace]
source: manual
related:
  - '[[Map - Infrastructure]]'
confidence: high
importance: medium
stability: stable
---

# Workspace Layout

## Summary
`~/hermes-workspace/` holds active project work. It is distinct from
any optional versioned infrastructure checkout and `~/.hermes/` (Hermes runtime).

## Structure

```
~/hermes-workspace/
├── vault/               curated Obsidian vault (cortex-managed)
└── hermes-cortex/       cortex source repo
```

## What goes where

| Type | Location |
|---|---|
| Active project repos | `~/hermes-workspace/<project>/` |
| Curated knowledge | `~/hermes-workspace/vault/` |
| Versioned infra | user-defined project path |
| Hermes runtime / config | `~/.hermes/` |
| Throwaway downloads | NOT in `~` directly — pick a purposeful subdir |

## Rules
- Keep `~/hermes-workspace/` organized by purpose, not dumped into `~`.
- One repo per directory. No nested checkouts.

## Related
- [[Map - Infrastructure]]
- [[Project - hermes-cortex]]
