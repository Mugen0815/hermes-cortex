# Archived Incident: Hook Injection Debug — 2026-05-11

> Historical incident note. This file is kept for archaeology only and is not
> current setup guidance. The current public architecture is documented in
> `docs/ARCHITECTURE.md` and uses `plugin_runtime.py` as the Hermes plugin
> registration/runtime layer.

## Why this is archived

The original incident mixed one-off runtime state, local config observations, and
then-current fixes. That was useful during debugging but misleading as stable
public documentation. Do not copy commands or config from this note into new
installations.

## Historical problem

A pre-LLM hook was expected to inject the `memory-query-flow` skill, but later
turns did not contain the expected context.

## Historical diagnosis pattern

The useful debugging pattern was:

1. Test skill loading directly in Python.
2. Test the hook function directly with a synthetic user message.
3. Inspect the active Cortex/Hermes hook lifecycle rather than trusting a single
   boolean flag.
4. Verify where Hermes injects hook output: user-message context versus system
   prompt policy.
5. Separate first-turn bootstrap behavior from each-turn skill/context behavior.

## Current interpretation

Current Cortex deployments use semantic hook blocks such as `skill_context`,
`bootstrap_context`, `recent_context`, `dynamic_context`, and `static_files`.
Legacy `hooks.context_injection` may still be parsed for compatibility, but when
semantic blocks are present it is compatibility baggage, not the active mental
model.

Use these current commands instead of old incident snippets:

```bash
hermes cortex status
hermes cortex config show
```

Then read the hook lifecycle rows and confirm:

- what is injected,
- when it runs,
- where it is injected,
- which config path is effective,
- and which process/session has loaded the runtime code.
