"""Interactive installer for hermes-cortex.

Usage (programmatic):
    from cortex.installer import Installer, InstallPlan
    plan = InstallPlan(vault_path=..., ...)
    Installer(plan).run()

Usage (CLI):
    cortex init                    # interactive
    cortex init --yes              # non-interactive, all defaults
    cortex init --dry-run          # show what would happen
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

# ---- Default install targets ------------------------------------------------

DEFAULT_VAULT_PATH = Path.home() / "hermes-workspace" / "vault"
DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "cortex" / "config.yaml"
DEFAULT_CHUNKS_PATH = Path.home() / ".hermes" / "cortex" / "chunks.jsonl"
DEFAULT_CHROMA_PATH = Path.home() / ".hermes" / "cortex" / "chroma"

DEFAULT_HERMES_MEMORY = Path.home() / ".hermes" / "memories" / "MEMORY.md"
DEFAULT_HERMES_USER = Path.home() / ".hermes" / "memories" / "USER.md"
DEFAULT_HERMES_SOUL = Path.home() / ".hermes" / "SOUL.md"

VAULT_FOLDERS = [
    "00_inbox",
    "10_facts",
    "20_decisions",
    "30_projects",
    "40_runbooks",
    "50_people",
    "60_maps",
    "80_templates",
]


# ---- Plan -------------------------------------------------------------------


@dataclass
class InstallPlan:
    """Resolved install configuration. Pure data — no side effects."""

    vault_path: Path = DEFAULT_VAULT_PATH
    config_path: Path = DEFAULT_CONFIG_PATH
    chunks_path: Path = DEFAULT_CHUNKS_PATH
    chroma_path: Path = DEFAULT_CHROMA_PATH

    install_templates: bool = True
    install_seed_notes: bool = True
    install_vault_readme: bool = True

    hermes_memory_path: Optional[Path] = DEFAULT_HERMES_MEMORY
    hermes_user_path: Optional[Path] = DEFAULT_HERMES_USER
    hermes_soul_path: Optional[Path] = DEFAULT_HERMES_SOUL
    # Legacy Markdown mutation is explicit opt-in only. The paths above are
    # config/context coordinates; their presence must not imply write access to
    # Hermes' Markdown memory files.
    update_hermes_memory: bool = False
    update_hermes_soul_memory_rules: bool = False

    overwrite_policy: str = "ask"  # "ask" | "skip" | "force"
    dry_run: bool = False

    # Filled in during run; informational
    actions: list[str] = field(default_factory=list)


# ---- Prompt helpers ---------------------------------------------------------


class Prompt:
    """Tiny prompt helper. Injectable for tests (pass a fake reader)."""

    def __init__(self, reader: Callable[[str], str] = input, writer: Callable[[str], None] = print):
        self._read = reader
        self._write = writer

    def info(self, msg: str) -> None:
        self._write(msg)

    def ask(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        raw = self._read(f"{question}{suffix}: ").strip()
        return raw or default

    def confirm(self, question: str, default: bool = True) -> bool:
        d = "Y/n" if default else "y/N"
        raw = self._read(f"{question} [{d}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes", "j", "ja")

    def choose(self, question: str, options: list[str], default: str) -> str:
        opts = "/".join(o.upper() if o == default else o for o in options)
        while True:
            raw = self._read(f"{question} ({opts}): ").strip().lower()
            if not raw:
                return default
            if raw in options:
                return raw
            self._write(f"  Please answer one of: {', '.join(options)}")


# ---- Installer --------------------------------------------------------------


class Installer:
    """Executes an InstallPlan. Side effects guarded by dry_run."""

    def __init__(self, plan: InstallPlan, prompt: Optional[Prompt] = None):
        self.plan = plan
        self.prompt = prompt or Prompt()

    # ---- Public API --------------------------------------------------------

    def run(self) -> InstallPlan:
        p = self.plan
        self._announce("Setting up hermes-cortex")
        self._setup_vault()
        if p.install_templates:
            self._copy_templates()
        if p.install_vault_readme:
            self._copy_vault_readme()
        if p.install_seed_notes:
            self._copy_seed_notes()
        self._write_config()
        if p.update_hermes_memory:
            self._update_hermes_memory_coordinates()
        if p.update_hermes_soul_memory_rules:
            self._update_hermes_soul_memory_rules()
        self._enable_cortex_toolset()
        self._summary()
        return p

    # ---- Steps -------------------------------------------------------------

    def _setup_vault(self) -> None:
        vault = self.plan.vault_path
        if vault.exists() and any(vault.iterdir()):
            self._action(f"Vault already exists with content: {vault}")
        else:
            self._action(f"Create vault directory: {vault}")
            self._mkdir(vault)
        for folder in VAULT_FOLDERS:
            sub = vault / folder
            if not sub.exists():
                self._mkdir(sub)
                gitkeep = sub / ".gitkeep"
                self._write_text(gitkeep, "")
        self._action(f"Ensured {len(VAULT_FOLDERS)} vault folders exist")

    def _copy_templates(self) -> None:
        target_dir = self.plan.vault_path / "80_templates"
        for src_path in _seed_files("templates"):
            dst = target_dir / src_path.name
            self._copy_file(src_path, dst)

    def _copy_vault_readme(self) -> None:
        src = _seed_root() / "vault-README.md"
        dst = self.plan.vault_path / "README.md"
        self._copy_file(src, dst)

    def _copy_seed_notes(self) -> None:
        seed_notes = _seed_root() / "notes"
        for src in seed_notes.rglob("*.md"):
            # Folder READMEs document the seed package layout; they are not vault
            # notes and may intentionally omit note frontmatter.
            if src.name == "README.md":
                continue
            rel = src.relative_to(seed_notes)
            dst = self.plan.vault_path / rel
            self._copy_file(src, dst)

    def _write_config(self) -> None:
        cfg = self.plan.config_path
        content = self._render_config()
        self._copy_text(content, cfg)

    def _update_hermes_memory_coordinates(self) -> None:
        mem = self.plan.hermes_memory_path
        if not mem:
            self._action("SKIP Hermes MEMORY.md update: no path configured")
            return
        if not mem.exists():
            self._action(f"SKIP Hermes MEMORY.md update: not found: {mem}")
            return

        original = mem.read_text(encoding="utf-8")
        updated = _render_updated_hermes_memory(original, self.plan.vault_path)
        if updated == original:
            self._action(f"Hermes MEMORY.md already points at vault: {self.plan.vault_path}")
            return

        self._action(f"Update Hermes MEMORY.md vault coordinates: {mem}")
        if self.plan.dry_run:
            return

        backup = mem.with_suffix(mem.suffix + ".bak")
        backup.write_text(original, encoding="utf-8")
        tmp = mem.with_suffix(mem.suffix + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(mem)

    def _update_hermes_soul_memory_rules(self) -> None:
        """Patch SOUL.md with cortex-specific memory rules if not already present."""
        soul = self.plan.hermes_soul_path
        if not soul or not soul.exists():
            self._action("SKIP SOUL.md memory rules: path not configured or file not found")
            return

        vault = str(self.plan.vault_path)
        original = soul.read_text(encoding="utf-8")
        expected_lines = [
            "# Memory Rules",
            "- Vault/Cortex is the primary source of truth. holographic/fact_store are supplemental fallback only — never use them before the Vault for stable project/infrastructure facts.",
            f"- Persistent memory lives in the Obsidian Vault (`{vault}`), queried via vault_search/vault_read_note tools. Cortex binary handles index/embed after writes.",
            "- Always load the `memory-query-flow` skill before using `session_search`. No exceptions.",
            "- Lookup-Reihenfolge: SOUL.md (Prompt) \u2192 MEMORY.md/USER.md (Prompt) \u2192 vault_search/vault_read_note (Vault) \u2192 session_search (Sessions)",
        ]

        # Check if Memory Rules section already exists and is up to date
        if "# Memory Rules" in original:
            # Check if the vault path line matches
            if f"`{vault}`" in original:
                self._action("SOUL.md already contains Memory Rules with correct vault path")
                return
            self._action("SOUL.md Memory Rules present but vault path may be outdated — skipping (manual edit recommended)")
            return

        # Insert after the first top-level heading, or at the end
        lines = original.splitlines()
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("# ") and line != "# Memory Rules":
                insert_at = i + 1
                break
        else:
            insert_at = len(lines)

        new_block = [""] + expected_lines + [""]
        updated_lines = lines[:insert_at] + new_block + lines[insert_at:]
        updated = "\n".join(updated_lines).strip() + "\n"

        if updated == original:
            self._action("SOUL.md memory rules unchanged")
            return

        self._action(f"Update SOUL.md with Memory Rules section: {soul}")
        if self.plan.dry_run:
            return
        soul.write_text(updated, encoding="utf-8")

    def _enable_cortex_toolset(self) -> None:
        """Ensure cortex plugin and toolset are enabled in Hermes config.yaml."""
        config = self.plan.config_path
        if not config:
            return

        # The Hermes config.yaml is at ~/.hermes/config.yaml, not the cortex config
        hermes_config = Path.home() / ".hermes" / "config.yaml"
        if not hermes_config.exists():
            self._action(f"SKIP cortex toolset: Hermes config not found: {hermes_config}")
            return

        original = hermes_config.read_text(encoding="utf-8")
        changes = []

        # Enable plugin
        if "plugins:" in original:
            if "- cortex" not in original.split("plugins:")[1].split("\n")[0:10]:
                # Add cortex to enabled plugins list
                updated = original.replace(
                    "  enabled:\n",
                    "  enabled:\n  - cortex\n",
                    1
                )
                if updated != original:
                    changes.append("cortex plugin enabled")
                    original = updated
            else:
                changes.append("cortex plugin already enabled")
        else:
            updated = original + "\nplugins:\n  enabled:\n    - cortex\n  disabled: []\n"
            changes.append("cortex plugin section added")
            original = updated

        # Enable toolset for CLI
        if "platform_toolsets:" in original:
            cli_section = original.split("platform_toolsets:")[1]
            if "cli:" in cli_section:
                cli_part = cli_section.split("cli:")[1].split("\n")[0]
                if "cortex" not in cli_part.split("\n")[0]:
                    # Add cortex after hermes-cli in cli tools
                    updated = original.replace(
                        "    - hermes-cli",
                        "    - hermes-cli\n    - cortex",
                        1
                    )
                    if updated != original:
                        changes.append("cortex toolset enabled for CLI")
                        original = updated
                    else:
                        changes.append("cortex CLI toolset already present")
                else:
                    changes.append("cortex CLI toolset already enabled")
            else:
                updated = original.replace(
                    "platform_toolsets:",
                    "platform_toolsets:\n  cli:\n    - cortex",
                    1
                )
                if updated != original:
                    changes.append("cortex CLI toolset added")
                    original = updated
        else:
            updated = original + "\nplatform_toolsets:\n  cli:\n    - cortex\n"
            changes.append("cortex toolset section added")
            original = updated

        if changes:
            self._action(f"Update Hermes config: {', '.join(changes)}")
            if not self.plan.dry_run:
                hermes_config.write_text(original, encoding="utf-8")
        else:
            self._action("Hermes config already has cortex enabled")

    def _summary(self) -> None:
        self.prompt.info("")
        self.prompt.info("Done." if not self.plan.dry_run else "Dry-run complete — no files were written.")
        for a in self.plan.actions:
            self.prompt.info(f"  • {a}")
        if not self.plan.dry_run:
            self.prompt.info("")
            self.prompt.info(f"Vault:  {self.plan.vault_path}")
            self.prompt.info(f"Config: {self.plan.config_path}")
            self.prompt.info("")
            self.prompt.info("Next: run `cortex index`, `cortex embed`, then `cortex graph build`.")

    # ---- File helpers (dry-run aware, overwrite policy aware) --------------

    def _mkdir(self, path: Path) -> None:
        if self.plan.dry_run:
            return
        path.mkdir(parents=True, exist_ok=True)

    def _write_text(self, path: Path, content: str) -> None:
        if self.plan.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _copy_file(self, src: Path, dst: Path) -> None:
        if dst.exists() and not self._allowed_to_overwrite(dst):
            self._action(f"SKIP existing: {dst}")
            return
        verb = "OVERWRITE" if dst.exists() else "Create"
        self._action(f"{verb}: {dst}")
        if self.plan.dry_run:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _copy_text(self, content: str, dst: Path) -> None:
        if dst.exists() and not self._allowed_to_overwrite(dst):
            self._action(f"SKIP existing: {dst}")
            return
        verb = "OVERWRITE" if dst.exists() else "Create"
        self._action(f"{verb}: {dst}")
        if self.plan.dry_run:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    def _allowed_to_overwrite(self, dst: Path) -> bool:
        policy = self.plan.overwrite_policy
        if policy == "force":
            return True
        if policy == "skip":
            return False
        # ask
        return self.prompt.confirm(f"  {dst} exists — overwrite?", default=False)

    # ---- Misc --------------------------------------------------------------

    def _action(self, msg: str) -> None:
        self.plan.actions.append(msg)

    def _announce(self, msg: str) -> None:
        self.prompt.info("")
        self.prompt.info(f"=== {msg} ===")
        if self.plan.dry_run:
            self.prompt.info("(dry run — no changes will be written)")

    def _render_config(self) -> str:
        p = self.plan
        hm = []
        if p.hermes_memory_path:
            hm.append(f"  memory_path: {p.hermes_memory_path}")
        if p.hermes_user_path:
            hm.append(f"  user_path: {p.hermes_user_path}")
        if p.hermes_soul_path:
            hm.append(f"  soul_path: {p.hermes_soul_path}")
        hermes_block = "hermes_memory:\n" + "\n".join(hm) if hm else "hermes_memory: {}"

        return f"""# hermes-cortex configuration (generated by `cortex init`)

vault:
  path: {p.vault_path}
  include_folders: [10_facts, 20_decisions, 30_projects, 40_runbooks, 50_people, 60_maps]
  exclude_folders: [00_inbox, 80_templates]

{hermes_block}

index:
  chunks_path: {p.chunks_path}
  chroma_path: {p.chroma_path}
  collection: cortex-vault

embeddings:
  model: sentence-transformers/all-MiniLM-L6-v2
  device: auto

search:
  top_k: 20
  bm25_weight: 0.5
  vector_weight: 0.5
  rrf_k: 60
  wikilink_traversal: 1
  recency_boost: true
  importance_boost: true

context_builder:
  token_budget: 4000
  cite_sources: true
  include_hermes_memory: false
  include_static_files: []

cron:
  nightly_promotion:
    enabled: false
    name: hermes-cortex-nightly-promotion
    schedule: "0 2 * * *"
    timezone: Europe/Berlin
    deliver: origin
    enabled_toolsets: [file, terminal]
    lookback_days: 1
    state_db_path: ~/.hermes/state.db
    legacy_fallback_enabled: true
    session_globs:
      - ~/.hermes/sessions/*.jsonl
      - ~/.hermes/sessions/session_*.json
    dry_run_first: true

hooks:
  cache_warm:
    enabled: false
  skill_context:
    enabled: true
    when: each_turn
    load_skill: true
    skill_path: ""
    budget: 1000
  bootstrap_context:
    enabled: false
    when: first_turn
    budget: 1000
    include_static_files:
      - enabled: false
        label: Hermes SOUL bootstrap
        path: ~/.hermes/SOUL.md
        order: 10
        max_bytes: 12000
        optional: true
      - enabled: false
        label: Hermes MEMORY bootstrap
        path: ~/.hermes/memories/MEMORY.md
        order: 20
        max_bytes: 8000
        optional: true
      - enabled: false
        label: Hermes USER bootstrap
        path: ~/.hermes/memories/USER.md
        order: 30
        max_bytes: 8000
        optional: true
  recent_context:
    enabled: false
    when: first_turn
    source: disabled_placeholder
    budget: 1000
  dynamic_context:
    enabled: false
    when: each_turn
    budget: 1000
    query: ""
  context_injection:
    enabled: false
    load_skill: false
    skill_path: ""
    budget: 1000
    query: ""

logging:
  level: INFO
"""


def _render_updated_hermes_memory(content: str, vault_path: Path) -> str:
    """Patch Hermes MEMORY.md coordinates without overwriting unrelated memory."""
    vault = str(vault_path)
    lines = content.splitlines()
    out: list[str] = []
    changed = False
    inserted_cortex = False

    for line in lines:
        if line.startswith("- Obsidian vault:"):
            new_line = f"- Obsidian vault: `{vault}`"
            out.append(new_line)
            changed = changed or line != new_line
            continue
        if line.startswith("- Cortex-backed vault:"):
            new_line = f"- Cortex-backed vault: `{vault}`"
            out.append(new_line)
            changed = changed or line != new_line
            inserted_cortex = True
            continue
        out.append(line)

    if not any(line.startswith("- Obsidian vault:") for line in out):
        insert_at = _runtime_insert_index(out)
        out.insert(insert_at, f"- Obsidian vault: `{vault}`")
        changed = True
        if insert_at <= len(out):
            inserted_cortex = False

    if not inserted_cortex:
        insert_at = _runtime_insert_index(out)
        out.insert(insert_at, f"- Cortex-backed vault: `{vault}`")
        changed = True

    final = "\n".join(out) + ("\n" if content.endswith("\n") else "")
    return final if changed else content


def _runtime_insert_index(lines: list[str]) -> int:
    """Insert at the end of the Runtime bullet block when possible."""
    try:
        runtime_idx = lines.index("# Runtime")
    except ValueError:
        return 0
    idx = runtime_idx + 1
    while idx < len(lines):
        line = lines[idx]
        if idx > runtime_idx + 1 and line.startswith("# "):
            # Prefer inserting before the blank line that separates Runtime
            # from the next heading, preserving Markdown spacing.
            return idx - 1 if idx - 1 > runtime_idx and lines[idx - 1] == "" else idx
        if line == "" or line.startswith("-"):
            idx += 1
            continue
        break
    return idx


# ---- Interactive plan builder -----------------------------------------------


def build_plan_interactively(prompt: Optional[Prompt] = None) -> InstallPlan:
    """Walk the user through the plan. Returns an InstallPlan ready to execute."""
    p = prompt or Prompt()
    plan = InstallPlan()

    p.info("")
    p.info("hermes-cortex installer")
    p.info("This will set up your vault, templates, and config.")
    p.info("")

    plan.vault_path = Path(p.ask("Vault path", str(DEFAULT_VAULT_PATH))).expanduser().resolve()
    plan.config_path = Path(p.ask("Config file path", str(DEFAULT_CONFIG_PATH))).expanduser().resolve()

    plan.install_templates = p.confirm("Install note templates (80_templates/)?", default=True)
    plan.install_seed_notes = p.confirm("Install seed notes (Map of Content + basic facts)?", default=True)
    plan.install_vault_readme = p.confirm("Install vault README.md?", default=True)

    p.info("")
    p.info("Hermes memory files:")
    if p.confirm("  Auto-detect from default Hermes paths?", default=True):
        plan.hermes_memory_path = DEFAULT_HERMES_MEMORY if DEFAULT_HERMES_MEMORY.exists() else None
        plan.hermes_user_path = DEFAULT_HERMES_USER if DEFAULT_HERMES_USER.exists() else None
        plan.hermes_soul_path = DEFAULT_HERMES_SOUL if DEFAULT_HERMES_SOUL.exists() else None
        for label, path in [("MEMORY.md", plan.hermes_memory_path),
                            ("USER.md", plan.hermes_user_path),
                            ("SOUL.md", plan.hermes_soul_path)]:
            mark = "✓" if path else "—"
            p.info(f"    {mark} {label}: {path or '(not found, skipped)'}")
    else:
        plan.hermes_memory_path = _ask_optional_path(p, "  MEMORY.md path (blank to skip)", DEFAULT_HERMES_MEMORY)
        plan.hermes_user_path = _ask_optional_path(p, "  USER.md path (blank to skip)", DEFAULT_HERMES_USER)
        plan.hermes_soul_path = _ask_optional_path(p, "  SOUL.md path (blank to skip)", DEFAULT_HERMES_SOUL)

    p.info("")
    plan.update_hermes_memory = p.confirm(
        "Legacy opt-in: update MEMORY.md vault coordinates?",
        default=False,
    )
    plan.update_hermes_soul_memory_rules = p.confirm(
        "Legacy opt-in: patch SOUL.md Memory Rules?",
        default=False,
    )

    p.info("")
    plan.overwrite_policy = p.choose(
        "Overwrite policy for existing files",
        options=["ask", "skip", "force"],
        default="ask",
    )

    p.info("")
    if not p.confirm("Proceed with these settings?", default=True):
        p.info("Aborted.")
        sys.exit(0)

    return plan


def _ask_optional_path(p: Prompt, question: str, default: Path) -> Optional[Path]:
    raw = p.ask(question, str(default))
    if not raw or raw.lower() in ("none", "skip", "-"):
        return None
    return Path(raw).expanduser().resolve()


# ---- Seed file helpers ------------------------------------------------------


def _seed_root() -> Path:
    """Locate the bundled _seed directory (works in source checkout & installed)."""
    # importlib.resources for installed packages; fall back to filesystem for editable.
    try:
        ref = resources.files("cortex") / "_seed"
        # ref may be MultiplexedPath; coerce to Path via str
        path = Path(str(ref))
        if path.exists():
            return path
    except Exception:
        pass
    return Path(__file__).parent / "_seed"


def _seed_files(subdir: str) -> list[Path]:
    root = _seed_root() / subdir
    return sorted(root.rglob("*.md"))
