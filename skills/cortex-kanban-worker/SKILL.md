---
name: cortex-kanban-worker
description: "Use when a Kanban worker has Cortex tools available and needs to retrieve, apply, and cite Vault knowledge while executing a task. Covers vault_search, vault_read_note, and vault_build_context workflows for task context, research, and completion summaries."
version: 1.0.0
tags:
  - kanban
  - cortex
  - vault
  - research
  - worker
---

# Cortex Kanban Worker — Vault-backed task execution

> This skill is loaded for Hermes Kanban workers that run with the Cortex plugin
> and Vault tools available (`HERMES_KANBAN_TASK` is set).

## Overview

As a Kanban worker with Cortex tools, use the Vault as durable project memory while
executing a task: retrieve relevant knowledge, inspect source notes, apply the
findings to the work, and record which Vault evidence was used.

## Workflow

### 1. Read the task and load context

```python
# First step: inspect the current Kanban task.
task = kanban_show()
title = task["title"]
body = task.get("body", "")

# Load relevant Vault context for the task topic.
context = vault_build_context(
    query=f"{title} {body}",
    budget=1500,        # roughly 1500 tokens of context
)
# context.text contains ranked Vault excerpts.
```

### 2. Run targeted Vault research

```python
# Search for specific information.
results = vault_search(
    query="search terms derived from the task context",
    top_k=10,
    filters={"type": ["fact", "decision"]},  # optional: restrict note types
)
for r in results["results"]:
    note = vault_read_note(file=r["file"])
    print(note["content"])  # full note text
```

### 3. Record findings in the task result

```python
kanban_complete(
    summary="Research completed — 3 relevant Vault notes found and evaluated",
    metadata={
        "vault_notes_consulted": [
            "10_facts/example.md",
            "30_projects/example.md",
        ],
        "vault_search_queries": ["search terms used"],
        "findings": "short summary of the findings",
    },
)
```

## Patterns

### Pattern A: Research task

```text
Task: "Research X and summarize the findings"
  → vault_build_context(query="X", budget=2000)
  → vault_search(query="X", top_k=5)
  → For each relevant note: vault_read_note()
  → Return findings via kanban_complete(metadata={...})
```

### Pattern B: Add Vault context to an implementation task

```text
Task: "Build feature Y"
  → vault_build_context(query="Feature Y architecture", budget=2000)
  → vault_search(query="Feature Y similar projects")
  → vault_read_note() for relevant architecture decisions
  → Use the retrieved knowledge while implementing the feature
```

### Pattern C: Decision support from the Vault

```text
Task: "Choose between option A and option B"
  → vault_search(query="Option A comparison", filters={"type": ["decision"]})
  → vault_search(query="Option B lessons learned")
  → vault_read_note() for matching decisions
  → Include the evidence in the task rationale
```

## Filters for `vault_search` / `vault_build_context`

| Filter | Values | Effect |
|--------|--------|--------|
| `type` | `fact`, `decision`, `project`, `runbook`, `map`, `task` | Restrict results to selected note types |
| `tags_any` | `["cortex", "hermes"]` | Notes with at least one listed tag |
| `tags_all` | `["cortex", "kanban"]` | Notes with all listed tags |
| `domain` | `"development"`, `"infrastructure"` | Notes in one domain |
| `project` | `"hermes-cortex"` | Notes for one project |
| `importance_min` | `3` | Notes with importance >= 3 |

## Notes

- Cache warming may run at session start, but the first `vault_search` call can
  still be slower than later calls.
- `vault_build_context` is the most efficient overview path: one call, ranked and
  token-budgeted.
- Use `vault_search` + `vault_read_note` for deep dives into individual notes.
- Results include `scores` and `ranks`; Cortex ranking is hybrid (BM25 + vector +
  graph).
- Record the Vault notes and search queries used in `kanban_complete(metadata=...)`
  so reviewers can trace conclusions back to evidence.
