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
A workspace directory holds active project work. It is distinct from any optional
versioned infrastructure checkout and from the Hermes runtime/config directory.

## Structure

```
<workspace>/
├── vault/               curated Obsidian vault (cortex-managed)
└── hermes-cortex/       cortex source repo
```

## What goes where

| Type | Location |
|---|---|
| Active project repos | `<workspace>/<project>/` |
| Curated knowledge | configured vault path |
| Versioned infra | user-defined project path |
| Hermes runtime / config | active Hermes home/profile |
| Throwaway downloads | purposeful scratch directory, not the repo root |

## Rules
- Keep the workspace organized by purpose, not dumped into a home directory.
- One repo per directory. No nested checkouts.

## Related
- [[Map - Infrastructure]]
- [[Project - hermes-cortex]]
