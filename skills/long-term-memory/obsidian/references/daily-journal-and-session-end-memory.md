# Daily Journal and session-end short-term memory

Use this when designing or operating Cortex/Vault memory flows where the user wants chronological orientation and same-day continuity without promoting everything to long-term memory.

## Problem

Nightly promotion is good for durable knowledge, but it leaves a same-day gap: a new session started before the nightly job runs can feel like it starts from zero. Asking the user to say "remember this" for every transient-but-useful thread is too much friction, and many such details do not belong in canonical long-term notes.

## Layer model

| Layer | Purpose | Typical source | Persistence |
|---|---|---|---|
| Session transcript | Raw audit trail | Hermes session store | Raw history |
| Session-end short-term memory | Same-day continuation context | `on_session_end` distill/queue | Ephemeral, TTL-like |
| Daily journal | Chronological orientation: what happened when | Nightly digest or session-end distills | Durable but non-authoritative |
| Canonical Vault notes | Facts, decisions, runbooks, project state | Nightly promotion / explicit writes | Durable truth layer |

## Design guidance

- Treat `on_session_end` as short-term working memory, not as a direct canonical writer.
- Prefer a lightweight queue or distill written at session end, then let nightly promotion decide what becomes canonical.
- Daily journal entries should answer "what did we work on or discuss today?" not "what is permanently true?".
- Keep journal entries concise, dated, and source-linked; avoid pasting transcript chunks.
- Do not require the user to explicitly say "remember this" for same-day continuity.
- Exclude noisy sources such as cron/api_server unless specifically useful.

## MVP shape

Start with an indexed map note rather than a new vault folder, because existing Cortex configs often index `60_maps/` already:

```text
60_maps/Map - Daily Journal.md
```

Suggested daily section:

```md
## YYYY-MM-DD

### Kurzfassung
- ...

### Themen / Projekte
- [[Project - ...]]

### Entscheidungen / Richtungen
- ...

### Offene Fäden
- ...

### Quellen
- session_id: ...
```

If a dedicated `70_journal/` folder is introduced later, update Cortex `vault.include_folders` and frontmatter validation/schema expectations first. Otherwise the notes may not be indexed or may produce metadata warnings.

## Future config sketch

```yaml
hooks:
  session_end_distill:
    enabled: true
    mode: queue|distill
    min_messages: 8
    exclude_sources: [cron, api_server]
    write_to: ~/.hermes/cortex/session_distills
    promote_direct: false

journal:
  enabled: true
  mode: daily_digest
  target: 60_maps/Map - Daily Journal.md
  source: sessiondb
  max_bullets_per_day: 12
  link_canonical_notes: true
```
