"""hermes-cortex command line entry point.

Subcommands:
    cortex init    [--yes] [--dry-run] [--vault PATH] [--config PATH]
    cortex index   [--config PATH] [--force]
    cortex embed   [--config PATH] [--force] [--batch-size N]
    cortex search  "<query>" [--top-k N] [filters...] [--no-boost] [--json]
    cortex search-eval [--cases PATH] [--baseline PATH] [--output PATH] [--json]
    cortex validate-frontmatter [--config PATH] [--path PATH ...] [--json] [--strict]
    cortex context "<query>" [--top-k N] [filters...] [--budget N] [--no-hermes-memory]
    cortex config  path|show
    cortex cron    install|uninstall|status [--config PATH]
    cortex status  [--config PATH]
    cortex graph   build [--force]
    cortex graph   status [--stale-days N]
    cortex graph   broken [--json]
    cortex graph   orphans [--json]
    cortex graph   centrality [--limit N] [--node-type TYPE] [--pagerank] [--json]
    cortex graph   stale [--stale-days N] [--json]
    cortex graph   contradictions [--json]
    cortex graph   export --format json|mermaid|d3-json [--node-type TYPE] [--edge-type TYPE] [--neighborhood ID] [--diagnostics] [-o FILE]
    cortex graph   viewer -o graph.html [--data graph_data.json | --embed-data] [--diagnostics]
    cortex lifecycle maintenance [--force] [--dry-run]
    cortex lifecycle nightly [--dry-run]
    cortex lifecycle weekly [--dry-run]
    cortex session-sources [--lookback-days N] [--timezone TZ] [--state-db-path PATH] [--session-glob GLOB] [--no-legacy-fallback]
    cortex reset   [--config PATH] [--chroma | --chunks | --all] [--yes]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cortex import __version__
from cortex.cli_helpers import (
    add_filter_flags,
    build_filters_from_args,
    print_error,
    print_json,
    resolve_config,
)
from cortex.installer import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HERMES_MEMORY,
    DEFAULT_HERMES_SOUL,
    DEFAULT_HERMES_USER,
    DEFAULT_VAULT_PATH,
    InstallPlan,
    Installer,
    build_plan_interactively,
)


# ---- init --------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    if args.yes:
        plan = InstallPlan(
            vault_path=Path(args.vault).expanduser().resolve() if args.vault else DEFAULT_VAULT_PATH,
            config_path=Path(args.config).expanduser().resolve() if args.config else DEFAULT_CONFIG_PATH,
            hermes_memory_path=DEFAULT_HERMES_MEMORY if DEFAULT_HERMES_MEMORY.exists() else None,
            hermes_user_path=DEFAULT_HERMES_USER if DEFAULT_HERMES_USER.exists() else None,
            hermes_soul_path=DEFAULT_HERMES_SOUL if DEFAULT_HERMES_SOUL.exists() else None,
            update_hermes_memory=args.legacy_update_hermes_memory,
            update_hermes_soul_memory_rules=args.legacy_update_soul_memory_rules,
            overwrite_policy="skip",
            dry_run=args.dry_run,
        )
    else:
        plan = build_plan_interactively()
        if args.vault:
            plan.vault_path = Path(args.vault).expanduser().resolve()
        if args.config:
            plan.config_path = Path(args.config).expanduser().resolve()
        plan.dry_run = args.dry_run

    Installer(plan).run()
    return 0


# ---- index -------------------------------------------------------------------


def _cmd_index(args: argparse.Namespace) -> int:
    from cortex.indexer import index_vault

    cfg = resolve_config(getattr(args, "config", None))
    print(f"Indexing vault: {cfg.vault.path}")
    print(f"Chunks file:    {cfg.index.chunks_path}")
    report = index_vault(cfg, force=args.force)
    print(report.summary())
    if report.notes_missing_frontmatter:
        print(f"\n  Missing frontmatter ({len(report.notes_missing_frontmatter)}):")
        for f in report.notes_missing_frontmatter[:10]:
            print(f"    - {f}")
    if report.notes_invalid_frontmatter:
        print(f"\n  Incomplete frontmatter ({len(report.notes_invalid_frontmatter)}):")
        for f, missing in report.notes_invalid_frontmatter[:10]:
            print(f"    - {f}  \u2192  missing: {missing}")
    if report.notes_with_warnings:
        print(f"\n  Frontmatter warnings ({len(report.notes_with_warnings)}):")
        for f, warns in report.notes_with_warnings[:10]:
            print(f"    - {f}: {'; '.join(warns)}")
    if report.chunks_oversize:
        print(
            f"\n  \u26a0  {report.chunks_oversize} chunks exceed the soft size cap "
            f"(>2000 chars after sub-splitting). Consider adding ### subheadings."
        )
    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for f, err in report.errors[:10]:
            print(f"    - {f}: {err}")
    return 0 if not report.errors else 1


# ---- embed -------------------------------------------------------------------


def _cmd_embed(args: argparse.Namespace) -> int:
    from cortex.embedder import ModelMismatchError, embed_chunks

    cfg = resolve_config(getattr(args, "config", None))
    print(f"Chunks file:  {cfg.index.chunks_path}")
    print(f"Chroma path:  {cfg.index.chroma_path}")
    print(f"Model:        {cfg.embeddings.model}")
    print(f"Device:       {cfg.embeddings.device} (will auto-resolve)")
    try:
        report = embed_chunks(cfg, force=args.force, batch_size=args.batch_size)
    except ModelMismatchError as e:
        return print_error(f"\n  \u2717  {e}", exit_code=2)
    print(report.summary())
    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for cid, err in report.errors[:10]:
            print(f"    - {cid}: {err}")
    return 0 if not report.errors else 1


# ---- search ------------------------------------------------------------------


def _cmd_search(args: argparse.Namespace) -> int:
    from cortex.search import HybridSearcher

    cfg = resolve_config(getattr(args, "config", None))

    filters = build_filters_from_args(args)
    try:
        filters.validate()
    except ValueError as e:
        return print_error(f"Invalid filters: {e}", exit_code=2)

    searcher = HybridSearcher(cfg)
    apply_boost = False if args.no_boost else None

    try:
        results = searcher.search(args.query, top_k=args.top_k, filters=filters, apply_boost=apply_boost)
    except ValueError as e:
        return print_error(f"Invalid filters: {e}", exit_code=2)

    if args.json:
        payload = [
            {
                "chunk_id": r.chunk_id,
                "file": r.chunk.get("file"),
                "heading_path": r.chunk.get("heading_path"),
                "final_score": r.final_score,
                "rrf_score": r.rrf_score,
                "bm25_rank": r.bm25_rank,
                "vector_rank": r.vector_rank,
                "graph_rank": r.graph_rank,
                "debug": r.debug,
            }
            for r in results
        ]
        print_json(payload)
        return 0

    if not results:
        print("(no results)")
        return 0

    print(f"Query: {args.query!r}  \u2192  {len(results)} hit(s)")
    for i, r in enumerate(results, 1):
        head = " / ".join(r.chunk.get("heading_path") or []) or "(intro)"
        channels = []
        if r.bm25_rank is not None:
            channels.append(f"bm25#{r.bm25_rank}")
        if r.vector_rank is not None:
            channels.append(f"vec#{r.vector_rank}")
        if r.graph_rank is not None:
            channels.append(f"graph#{r.graph_rank}")
        print(
            f"  {i:>2}. [{r.final_score:.4f}] {r.chunk.get('file')}  ::  {head}\n"
            f"      ({', '.join(channels) or 'no-channel'})"
        )
        text = (r.chunk.get("text") or "").strip().replace("\n", " ")
        if text:
            preview = text[:160] + ("\u2026" if len(text) > 160 else "")
            print(f"      {preview}")
    return 0


# ---- search eval -------------------------------------------------------------


def _cmd_search_eval(args: argparse.Namespace) -> int:
    """Run the reproducible search ranking eval harness."""
    from cortex.search_eval import (
        build_report_metadata,
        load_baseline,
        load_eval_cases,
        missing_expected_files,
        run_search_eval,
        summarize_report,
        write_report,
    )

    cfg = resolve_config(getattr(args, "config", None))
    try:
        cases = load_eval_cases(args.cases)
        if args.lint_vault_files:
            missing = missing_expected_files(cases, cfg.vault.path)
            if missing:
                details = "; ".join(
                    f"{case_id}: {', '.join(files)}" for case_id, files in sorted(missing.items())
                )
                raise ValueError(f"expected eval files missing from vault {cfg.vault.path}: {details}")
        baseline = load_baseline(args.baseline)
        metadata = build_report_metadata(
            cfg,
            config_path=args.config,
            cases_path=args.cases,
        )
        report = run_search_eval(
            cfg,
            cases,
            top_k=args.top_k,
            compare_unboosted=not args.no_unboosted,
            baseline=baseline,
            metadata=metadata,
        )
    except (OSError, ValueError) as e:
        return print_error(f"Search eval failed: {e}", exit_code=2)

    if args.output:
        write_report(args.output, report)
    if args.json:
        print_json(report)
    else:
        print(summarize_report(report))
        if args.output:
            print(f"  wrote: {args.output}")
    return 0 if args.allow_failures or report["failed"] == 0 else 1


# ---- validate-frontmatter -----------------------------------------------------

def _cmd_validate_frontmatter(args: argparse.Namespace) -> int:
    """Validate note frontmatter without mutating the vault."""
    from cortex.frontmatter_validator import validate_frontmatter

    cfg = resolve_config(getattr(args, "config", None))
    paths: list[str] = []
    for group in getattr(args, "path", None) or []:
        paths.extend(group)

    try:
        report = validate_frontmatter(cfg, paths=paths)
    except ValueError as e:
        return print_error(str(e), exit_code=2)
    if args.json:
        print_json(report.to_json())
    else:
        print(f"Frontmatter validation: {report.checked_count} file(s) checked")
        print(f"Vault: {report.vault_path}")
        print(f"Issues: {report.error_count} error(s), {report.warning_count} warning(s)")
        for result in report.files:
            if not result.issues:
                continue
            print(f"\n  {result.file}")
            for issue in result.issues:
                field = f" [{issue.field}]" if issue.field else ""
                print(f"    - {issue.severity.upper()} {issue.code}{field}: {issue.message}")

    if report.error_count:
        return 1
    if args.strict and report.warning_count:
        return 1
    return 0


# ---- context ----------------------------------------------------------------


def _cmd_context(args: argparse.Namespace) -> int:
    """Search + build a context blob in one go."""
    from cortex.context import ContextBuilder
    from cortex.search import HybridSearcher

    cfg = resolve_config(getattr(args, "config", None))

    if args.no_hermes_memory:
        cfg.context_builder.include_hermes_memory = False

    filters = build_filters_from_args(args)
    try:
        filters.validate()
    except ValueError as e:
        return print_error(f"Invalid filters: {e}", exit_code=2)

    searcher = HybridSearcher(cfg)
    apply_boost = False if args.no_boost else None
    try:
        results = searcher.search(args.query, top_k=args.top_k, filters=filters, apply_boost=apply_boost)
    except ValueError as e:
        return print_error(f"Invalid filters: {e}", exit_code=2)

    builder = ContextBuilder(cfg)
    ctx = builder.build(results, budget_override=args.budget)

    if args.json:
        payload = {
            "text": ctx.text,
            "tokens_used": ctx.tokens_used,
            "tokens_budget": ctx.tokens_budget,
            "chunks_included": ctx.chunks_included,
            "chunks_skipped_oversize": ctx.chunks_skipped_oversize,
            "hermes_memory_included": ctx.hermes_memory_included,
            "hermes_user_included": ctx.hermes_user_included,
            "citation_count": ctx.citation_count,
        }
        print_json(payload)
        return 0

    # Default: print the context Markdown verbatim — pipeable to a file
    # or directly to an LLM. Diagnostics go to stderr so they don't
    # corrupt the payload.
    print(ctx.text, end="")
    print(
        f"\n[ctx] tokens={ctx.tokens_used}/{ctx.tokens_budget}  "
        f"chunks_in={len(ctx.chunks_included)}  "
        f"skipped={len(ctx.chunks_skipped_oversize)}  "
        f"hermes={'mem' if ctx.hermes_memory_included else '-'}/"
        f"{'usr' if ctx.hermes_user_included else '-'}",
        file=sys.stderr,
    )
    return 0


# ---- reset -------------------------------------------------------------------


def _cmd_reset(args: argparse.Namespace) -> int:
    """Wipe the Chroma vector store and/or the chunks.jsonl file."""
    from cortex.embedder import reset_chroma

    cfg = resolve_config(getattr(args, "config", None))

    targets = []
    if args.all or args.chroma:
        targets.append(("Chroma vector store", cfg.index.chroma_path))
    if args.all or args.chunks:
        targets.append(("chunks.jsonl", cfg.index.chunks_path))
    if not targets:
        return print_error("Nothing to reset. Pass --chroma, --chunks, or --all.", exit_code=2)

    print("This will permanently delete:")
    for label, path in targets:
        existed = path.exists()
        print(f"  - {label}: {path}  {'(exists)' if existed else '(does not exist, no-op)'}")

    if not args.yes:
        try:
            answer = input("\nProceed? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    for label, path in targets:
        if path.exists():
            if label == "Chroma vector store":
                reset_chroma(cfg)
            else:
                path.unlink()
            print(f"  \u2713 removed {path}")
        else:
            print(f"  \u00b7 {path} did not exist; skipped")
    print("Done. Run `cortex index` and `cortex embed` to rebuild.")
    return 0


# ---- lifecycle --------------------------------------------------------------


def _cmd_lifecycle_dispatch(args: argparse.Namespace) -> int:
    """Dispatch for ``cortex lifecycle`` without a subcommand."""
    if not getattr(args, "lifecycle_cmd", None):
        print(
            "Usage: cortex lifecycle {maintenance|nightly|weekly}",
            file=sys.stderr,
        )
        return 2
    return args.func(args)


def _cmd_lifecycle_maintenance(args: argparse.Namespace) -> int:
    from cortex.lifecycle import run_maintenance

    cfg = resolve_config(getattr(args, "config", None))
    report = run_maintenance(cfg, force=getattr(args, "force", False), dry_run=getattr(args, "dry_run", False))
    print(report.summary())
    return 0 if report.ok else 1


def _cmd_lifecycle_nightly(args: argparse.Namespace) -> int:
    from cortex.lifecycle import run_nightly_promotion

    cfg = resolve_config(getattr(args, "config", None))
    report = run_nightly_promotion(cfg, dry_run=getattr(args, "dry_run", True))
    print(report.summary())
    return 0 if report.ok else 1


def _cmd_lifecycle_weekly(args: argparse.Namespace) -> int:
    from cortex.lifecycle import run_weekly_review

    cfg = resolve_config(getattr(args, "config", None))
    report = run_weekly_review(
        cfg,
        dry_run=getattr(args, "dry_run", False),
        stale_days=getattr(args, "stale_days", 180),
        stale_min_importance=getattr(args, "stale_min_importance", 4.0),
        consolidation_min_degree=getattr(args, "consolidation_min_degree", 3),
    )
    print(report.to_markdown())
    return 0 if report.ok else 1


# ---- session sources ---------------------------------------------------------


def _cmd_session_sources(args: argparse.Namespace) -> int:
    """Print recent Hermes sessions as JSON for cron/promotions.

    Exposed through ``hermes cortex`` so packaged cron prompts do not depend on
    a bare ``cortex`` console script or ``PYTHONPATH`` being present in the
    scheduler's agent shell.
    """
    from cortex.session_sources import _DEFAULT_SESSION_GLOBS, collect_recent_sessions

    result = collect_recent_sessions(
        lookback_days=getattr(args, "lookback_days", 1),
        timezone=getattr(args, "timezone", "Europe/Berlin"),
        state_db_path=getattr(args, "state_db_path", "~/.hermes/state.db"),
        session_globs=getattr(args, "session_globs", None) or _DEFAULT_SESSION_GLOBS,
        legacy_fallback_enabled=not getattr(args, "no_legacy_fallback", False),
    )
    print_json(result.as_dict())
    return 0


# ---- cron --------------------------------------------------------------------


def _cmd_cron_dispatch(args: argparse.Namespace) -> int:
    """Dispatch for ``cortex cron`` without a subcommand."""
    if not getattr(args, "cron_cmd", None):
        print(
            "Usage: cortex cron {install|uninstall|status}",
            file=sys.stderr,
        )
        return 2
    return args.func(args)


def _print_cron_install_result(result: dict) -> int:
    action = result["action"]
    if action == "disabled":
        print(f"  {result.get('job', 'cron')} cron job '{result['name']}' is disabled in config")
        print(f"  Reason: {result['reason']}")
        return 1
    print(f"  {action} {result.get('job', 'cron')} cron job '{result['name']}' (id: {result['job_id']})")
    print(f"  schedule: {result['schedule']}")
    print(f"  deliver:  {result['deliver']}")
    if result.get("removed_duplicates"):
        print(f"  removed duplicates: {result['removed_duplicates']}")
    if action == "updated":
        print("  (existing lifecycle fields preserved)")
    return 0


def _cmd_cron_install(args: argparse.Namespace) -> int:
    from cortex.cron import install

    result = install(
        vault_path=getattr(args, "vault", None),
        config_path=getattr(args, "config", None),
        job=getattr(args, "job", "nightly"),
    )
    if result.get("action") == "multiple":
        codes = [_print_cron_install_result(item) for item in result["jobs"]]
        print("  config: ~/.hermes/cron/jobs.json")
        return 0 if all(code == 0 for code in codes) else 1
    code = _print_cron_install_result(result)
    print("  config: ~/.hermes/cron/jobs.json")
    return code


def _print_cron_uninstall_result(result: dict) -> int:
    if result["action"] == "not_found":
        print(f"  {result.get('job', 'cron')} cron job not found (id: {result['job_id']})")
        return 1
    print(f"  removed {result.get('job', 'cron')} cron job '{result['name']}'")
    return 0


def _cmd_cron_uninstall(args: argparse.Namespace) -> int:
    from cortex.cron import uninstall

    result = uninstall(config_path=getattr(args, "config", None), job=getattr(args, "job", "nightly"))
    if result.get("action") == "multiple":
        codes = [_print_cron_uninstall_result(item) for item in result["jobs"]]
        return 0 if all(code == 0 for code in codes) else 1
    return _print_cron_uninstall_result(result)


def _print_cron_status(s: dict) -> int:
    label = "Weekly review" if s.get("job") == "weekly" else "Nightly promotion"
    if not s["installed"]:
        print(f"  {label} cron: NOT INSTALLED")
        print(f"  Run `cortex cron install --job {s.get('job', 'nightly')}` to set it up.")
        return 1

    print(f"  Job:        {s.get('job', 'nightly')}")
    print(f"  Name:       {s['name']}")
    print(f"  ID:         {s['job_id']}")
    print(f"  Schedule:   {s['schedule']}")
    print(f"  Configured: {s['configured_schedule']}")
    print(f"  Deliver:    {s['deliver']}")
    print(f"  Toolsets:   {', '.join(s['enabled_toolsets'] or [])}")
    print(f"  Enabled:    {s['enabled']}")
    print(f"  Config on:  {s['configured_enabled']}")
    print(f"  State:      {s['state']}")
    if s.get("duplicates"):
        print(f"  Duplicates: {s['duplicates']} (run `cortex cron install --job {s.get('job', 'nightly')}` to reconcile)")
    print(f"  Last run:   {s['last_run'] or 'never'}")
    print(f"  Last status: {s['last_status'] or 'N/A'}")
    return 0


def _cmd_cron_status(args: argparse.Namespace) -> int:
    from cortex.cron import status

    result = status(config_path=getattr(args, "config", None), job=getattr(args, "job", "all"))
    if result.get("action") == "multiple":
        codes = [_print_cron_status(item) for item in result["jobs"]]
        return 0 if all(code == 0 for code in codes) else 1
    return _print_cron_status(result)


# ---- config/status ------------------------------------------------------------

def _legacy_context_label(cfg) -> str:
    suffix = " (deprecated/ignored)" if cfg.hooks.legacy_context_injection_deprecated else ""
    return f"{cfg.hooks.legacy_context_injection_present}{suffix}"


def _static_file_summary(entry) -> str:
    source = str(entry.path) if entry.path is not None else "(missing path)"
    bits = [
        f"{entry.label}: enabled={entry.enabled}",
        f"source={source}",
        f"optional={entry.optional}",
    ]
    if entry.budget is not None:
        bits.append(f"budget={entry.budget}")
    if entry.max_bytes is not None:
        bits.append(f"max_bytes={entry.max_bytes}")
    if entry.enabled and entry.optional and (entry.path is None or not entry.path.exists()):
        bits.append("skipped=optional file missing")
    return "; ".join(bits)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _print_hook_lifecycle(cfg, *, indent: str = "") -> None:
    print(f"{indent}Hook lifecycle:")
    print(f"{indent}  Runtime mode: {'semantic' if cfg.hooks.uses_semantic_runtime() else 'legacy'}")
    print(f"{indent}  Phase         Name                      Enabled  Effective  Timing         Origin           Skipped reason")
    for row in cfg.hooks.hook_statuses():
        skipped = row.skipped_reason or "-"
        print(
            f"{indent}  {row.phase:<13} {row.name:<25} "
            f"{_yes_no(row.enabled):<8} {_yes_no(row.effective):<10} "
            f"{row.timing:<14} {row.origin:<16} {skipped}"
        )
        print(f"{indent}    source: {row.source}; payload: {row.payload}; target: {row.target}")


def _cmd_config_path(args: argparse.Namespace) -> int:
    from cortex.config import find_config

    cfg_path = find_config()
    if cfg_path is None:
        return print_error("No cortex config found", exit_code=1)
    print(cfg_path)
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    cfg = resolve_config(getattr(args, "config", None))
    print(f"Config:          {cfg.source_path}")
    print(f"Vault:           {cfg.vault.path}")
    print(f"Chunks:          {cfg.index.chunks_path}")
    print(f"Chroma:          {cfg.index.chroma_path}")
    print(f"Collection:      {cfg.index.collection}")
    print(f"Embeddings:      {cfg.embeddings.model} ({cfg.embeddings.device})")
    print(f"Cache warm:      {cfg.hooks.cache_warm_enabled}")
    print(
        f"Skill context:   {cfg.hooks.skill_context.enabled} ({cfg.hooks.skill_context.when}); "
        f"load_skill={cfg.hooks.skill_context.load_skill}; "
        f"source={cfg.hooks.skill_context.skill_path or '(default profile skill path)'}; "
        f"budget={cfg.hooks.skill_context.budget}"
    )
    print(
        f"Bootstrap ctx:   {cfg.hooks.bootstrap_context.enabled} ({cfg.hooks.bootstrap_context.when}); "
        f"budget={cfg.hooks.bootstrap_context.budget}"
    )
    recent_skip = "; skipped=placeholder only" if cfg.hooks.recent_context.enabled else ""
    print(
        f"Recent context:  {cfg.hooks.recent_context.enabled} ({cfg.hooks.recent_context.when}); "
        f"source={cfg.hooks.recent_context.source}{recent_skip}"
    )
    print(f"Dynamic context: {cfg.hooks.dynamic_context.enabled} ({cfg.hooks.dynamic_context.when})")
    print(f"Dynamic budget:  {cfg.hooks.dynamic_context.budget}")
    print(f"Dynamic query:   {cfg.hooks.dynamic_context.query or '(user message when enabled)'}")
    print(f"Static files:    {len(cfg.hooks.bootstrap_context.include_static_files)} configured")
    for entry in cfg.hooks.bootstrap_context.include_static_files:
        print(f"  - {_static_file_summary(entry)}")
    print(f"Legacy context:  {_legacy_context_label(cfg)}")
    print(f"Runtime projection: {cfg.hooks.context_injection_enabled}")
    print(f"Load skill:      {cfg.hooks.load_skill}")
    print(f"Skill path:      {cfg.hooks.skill_path or '(default profile skill path)'}")
    _print_hook_lifecycle(cfg)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = resolve_config(getattr(args, "config", None))
    plugin_root = Path(__file__).resolve().parents[1]
    print("hermes-cortex status")
    print(f"  Plugin/code:    {plugin_root}")
    print(f"  Config:         {cfg.source_path}")
    print(f"  Vault:          {cfg.vault.path} ({'ok' if cfg.vault.path.exists() else 'missing'})")
    print(f"  Chunks:         {cfg.index.chunks_path} ({'ok' if cfg.index.chunks_path.exists() else 'missing'})")
    print(f"  Chroma:         {cfg.index.chroma_path} ({'ok' if cfg.index.chroma_path.exists() else 'missing'})")
    print(f"  Cache warm:     {cfg.hooks.cache_warm_enabled}")
    print(
        f"  Skill context:  {cfg.hooks.skill_context.enabled} ({cfg.hooks.skill_context.when}); "
        f"source={cfg.hooks.skill_context.skill_path or '(default profile skill path)'}"
    )
    print(
        f"  Bootstrap ctx:  {cfg.hooks.bootstrap_context.enabled} ({cfg.hooks.bootstrap_context.when}); "
        f"budget={cfg.hooks.bootstrap_context.budget}"
    )
    recent_skip = "; skipped=placeholder only" if cfg.hooks.recent_context.enabled else ""
    print(
        f"  Recent context: {cfg.hooks.recent_context.enabled} ({cfg.hooks.recent_context.when}); "
        f"source={cfg.hooks.recent_context.source}{recent_skip}"
    )
    print(
        f"  Dynamic ctx:    {cfg.hooks.dynamic_context.enabled} ({cfg.hooks.dynamic_context.when}); "
        f"query={cfg.hooks.dynamic_context.query or '(user message when enabled)'}; "
        f"budget={cfg.hooks.dynamic_context.budget}"
    )
    print(f"  Static files:   {len(cfg.hooks.bootstrap_context.include_static_files)} configured")
    for entry in cfg.hooks.bootstrap_context.include_static_files:
        print(f"    - {_static_file_summary(entry)}")
    print(f"  Legacy context: {_legacy_context_label(cfg)}")
    print(f"  Load skill:     {cfg.hooks.load_skill}")
    _print_hook_lifecycle(cfg, indent="  ")
    return 0


# ---- entry point -------------------------------------------------------------


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Configure an argparse parser with cortex subcommands.

    This is used by both the standalone ``cortex`` console script and the
    Hermes plugin CLI extension (``hermes cortex ...``), so the command
    surface stays identical instead of maintaining two parser trees.
    """
    parser.add_argument("--version", action="version", version=f"hermes-cortex {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # init
    init = sub.add_parser("init", help="Set up vault, templates, and config interactively")
    init.add_argument("--yes", "-y", action="store_true", help="Non-interactive, accept all defaults")
    init.add_argument("--dry-run", action="store_true", help="Show actions without writing files")
    init.add_argument("--vault", type=str, help="Override vault path")
    init.add_argument("--config", type=str, help="Override config path")
    init.add_argument(
        "--legacy-update-hermes-memory",
        action="store_true",
        help="Legacy opt-in: update Hermes MEMORY.md vault coordinates",
    )
    init.add_argument(
        "--legacy-update-soul-memory-rules",
        action="store_true",
        help="Legacy opt-in: patch SOUL.md with Cortex Memory Rules",
    )
    init.set_defaults(func=_cmd_init)

    # index
    idx = sub.add_parser("index", help="Build/update the chunks index from the vault")
    idx.add_argument("--config", type=str, help="Path to config.yaml")
    idx.add_argument("--force", action="store_true", help="Re-index all files, ignoring hashes")
    idx.set_defaults(func=_cmd_index)

    # embed
    emb = sub.add_parser("embed", help="Compute embeddings and upsert into the vector store")
    emb.add_argument("--config", type=str, help="Path to config.yaml")
    emb.add_argument("--force", action="store_true", help="Re-embed all chunks")
    emb.add_argument("--batch-size", type=int, default=32, help="Embedding batch size")
    emb.set_defaults(func=_cmd_embed)

    # search
    sr = sub.add_parser("search", help="Hybrid search over the indexed vault")
    sr.add_argument("query", type=str, help="Free-text query")
    sr.add_argument("--config", type=str, help="Path to config.yaml")
    sr.add_argument("--top-k", type=int, default=None, help="Number of results (default: cfg.search.top_k)")
    sr.add_argument("--no-boost", action="store_true", help="Disable recency/importance boosts for this call")
    sr.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text")
    add_filter_flags(sr)
    sr.set_defaults(func=_cmd_search)

    # search-eval
    ev = sub.add_parser(
        "search-eval",
        help="Run fixed search ranking eval cases and emit rank diagnostics",
    )
    ev.add_argument("--config", type=str, help="Path to config.yaml")
    ev.add_argument("--cases", type=str, help="YAML eval cases (default: tests/fixtures/search_eval_cases.yaml)")
    ev.add_argument("--top-k", type=int, default=10, help="Results per case (default: 10)")
    ev.add_argument(
        "--baseline",
        type=str,
        help=(
            "Previous JSON report to compare against. Baseline updates should happen only "
            "after lint-vault-files and index/embed maintenance pass."
        ),
    )
    ev.add_argument("--output", "-o", type=str, help="Write JSON report to this path")
    ev.add_argument("--json", action="store_true", help="Print full JSON report")
    ev.add_argument("--no-unboosted", action="store_true", help="Skip boosted-vs-unboosted rank comparison")
    ev.add_argument(
        "--lint-vault-files",
        action="store_true",
        help="Before running, fail if any expected_files entry is absent from the configured vault",
    )
    ev.add_argument("--allow-failures", action="store_true", help="Exit 0 even if expected ranks miss their thresholds")
    ev.set_defaults(func=_cmd_search_eval)

    # validate-frontmatter
    vf = sub.add_parser(
        "validate-frontmatter",
        help="Validate vault note frontmatter metadata without modifying files",
    )
    vf.add_argument("--config", type=str, help="Path to config.yaml")
    vf.add_argument(
        "--path",
        action="append",
        nargs="+",
        metavar="REL_OR_ABS",
        help="Specific note or directory path(s) to validate; relative paths resolve under the vault",
    )
    vf.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text")
    vf.add_argument("--strict", action="store_true", help="Treat warnings as a non-zero exit")
    vf.set_defaults(func=_cmd_validate_frontmatter)

    # context
    cx = sub.add_parser(
        "context",
        help="Search and build an LLM-injectable Markdown context blob under a token budget",
    )
    cx.add_argument("query", type=str, help="Free-text query")
    cx.add_argument("--config", type=str, help="Path to config.yaml")
    cx.add_argument("--top-k", type=int, default=None, help="Number of results to consider (default: cfg.search.top_k)")
    cx.add_argument("--budget", type=int, default=None, help="Override token budget for this call")
    cx.add_argument("--no-boost", action="store_true", help="Disable recency/importance boosts")
    cx.add_argument("--no-hermes-memory", action="store_true", help="Do not include MEMORY.md / USER.md sections")
    cx.add_argument("--json", action="store_true", help="Output a JSON envelope with diagnostics; default is raw Markdown")
    add_filter_flags(cx)
    cx.set_defaults(func=_cmd_context)

    # graph (delegated to cli_graph module)
    from cortex.cli_graph import add_graph_subparser

    add_graph_subparser(sub)

    # config/status
    cfgp = sub.add_parser("config", help="Show cortex config paths and effective settings")
    cfg_sub = cfgp.add_subparsers(dest="config_cmd")

    cfg_path = cfg_sub.add_parser("path", help="Print the active cortex config path")
    cfg_path.set_defaults(func=_cmd_config_path)

    cfg_show = cfg_sub.add_parser("show", help="Print effective cortex config summary")
    cfg_show.add_argument("--config", type=str, help="Path to config.yaml")
    cfg_show.set_defaults(func=_cmd_config_show)

    cfgp.set_defaults(func=_cmd_config_show)

    st = sub.add_parser("status", help="Show plugin, config, vault, and index paths")
    st.add_argument("--config", type=str, help="Path to config.yaml")
    st.set_defaults(func=_cmd_status)

    # lifecycle
    lc = sub.add_parser("lifecycle", help="Run lifecycle automation (maintenance, review, promotion)")
    lc.add_argument("--config", type=str, help="Path to config.yaml")
    lc_sub = lc.add_subparsers(dest="lifecycle_cmd")

    lc_maint = lc_sub.add_parser("maintenance", help="Run index \u2192 embed \u2192 graph build pipeline")
    lc_maint.add_argument("--force", action="store_true", help="Force-rebuild all steps")
    lc_maint.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    lc_maint.set_defaults(func=_cmd_lifecycle_maintenance)

    lc_nightly = lc_sub.add_parser("nightly", help="Run nightly promotion workflow")
    lc_nightly.add_argument("--dry-run", action="store_true", default=True, help="Show what would change without writing (default)")
    lc_nightly.add_argument("--write", dest="dry_run", action="store_false", help="Apply eligible promotions atomically")
    lc_nightly.set_defaults(func=_cmd_lifecycle_nightly)

    lc_weekly = lc_sub.add_parser("weekly", help="Run weekly read-only graph review")
    lc_weekly.add_argument("--dry-run", action="store_true", help="Label output as dry-run; weekly never writes")
    lc_weekly.add_argument("--stale-days", type=int, default=180, help="Days threshold for stale detection (default: 180)")
    lc_weekly.add_argument("--stale-min-importance", type=float, default=4.0, help="Minimum importance for stale review items (default: 4.0)")
    lc_weekly.add_argument("--consolidation-min-degree", type=int, default=3, help="Minimum degree for consolidation proposals (default: 3)")
    lc_weekly.set_defaults(func=_cmd_lifecycle_weekly)

    lc.set_defaults(func=_cmd_lifecycle_dispatch)

    # session-sources
    ss = sub.add_parser(
        "session-sources",
        help="Collect recent Hermes sessions from SessionDB with legacy file fallback",
    )
    ss.add_argument("--lookback-days", type=int, default=1)
    ss.add_argument("--timezone", default="Europe/Berlin")
    ss.add_argument("--state-db-path", default="~/.hermes/state.db")
    ss.add_argument("--session-glob", action="append", dest="session_globs", default=[])
    ss.add_argument("--no-legacy-fallback", action="store_true")
    ss.set_defaults(func=_cmd_session_sources)

    # cron
    cr = sub.add_parser("cron", help="Manage Hermes cortex cron jobs (install/uninstall/status)")
    cr_sub = cr.add_subparsers(dest="cron_cmd")

    cr_install = cr_sub.add_parser("install", help="Install/update a cortex cron job")
    cr_install.add_argument("--config", type=str, help="Path to config.yaml")
    cr_install.add_argument("--job", choices=["nightly", "weekly", "all"], default="nightly", help="Cron job to install (default: nightly)")
    cr_install.add_argument("--vault", type=str, help="Override vault path (default: ~/hermes-workspace/vault)")
    cr_install.set_defaults(func=_cmd_cron_install)

    cr_uninstall = cr_sub.add_parser("uninstall", help="Remove a cortex cron job")
    cr_uninstall.add_argument("--config", type=str, help="Path to config.yaml")
    cr_uninstall.add_argument("--job", choices=["nightly", "weekly", "all"], default="nightly", help="Cron job to remove (default: nightly)")
    cr_uninstall.set_defaults(func=_cmd_cron_uninstall)

    cr_status = cr_sub.add_parser("status", help="Check if cortex cron job(s) are installed")
    cr_status.add_argument("--config", type=str, help="Path to config.yaml")
    cr_status.add_argument("--job", choices=["nightly", "weekly", "all"], default="all", help="Cron job to inspect (default: all)")
    cr_status.set_defaults(func=_cmd_cron_status)

    cr.set_defaults(func=_cmd_cron_dispatch)

    # reset
    rst = sub.add_parser("reset", help="Delete the vector store and/or chunks.jsonl to start fresh")
    rst.add_argument("--config", type=str, help="Path to config.yaml")
    rst.add_argument("--chroma", action="store_true", help="Delete the Chroma vector store")
    rst.add_argument("--chunks", action="store_true", help="Delete chunks.jsonl")
    rst.add_argument("--all", action="store_true", help="Delete both Chroma and chunks.jsonl")
    rst.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    rst.set_defaults(func=_cmd_reset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex", description="hermes-cortex CLI")
    configure_parser(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
