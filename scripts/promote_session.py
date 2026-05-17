#!/usr/bin/env python3
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable


MEMORY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\bprefer',
        r'\bpreference',
        r'\bdislike',
        r'\bwant\b',
        r'\btrust\b',
        r'\ballow\b',
        r'\balways\b',
        r'\bnever\b',
        r'\bremember\b',
        r'\bworkflow',
        r'\bskills?\b',
        r'\bintuition\b',
        r'mir ist wichtig',
        r'ich möchte',
        r'ich will',
        r'bevorzug',
        r'dauerhaft',
        r'systematisch',
        r'proaktiv',
    ]
]

VAULT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\bdecision\b',
        r'\bfact\b',
        r'\brunbook\b',
        r'\bproject\b',
        r'\bworkflow\b',
        r'\bmemory\b',
        r'\bvault\b',
        r'\bobsidian\b',
        r'\bstruktur',
        r'\bservice\b',
        r'\bläuft\b',
        r'\blangt?zeit',
        r'\bgateway\b',
        r'\bdashboard\b',
        r'\bcdp\b',
        r'\bbackup\b',
        r'\bdecision\b',
    ]
]

REPO_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'/opt/',
        r'/home/',
        r'~/',
        r'\bdocs?/',
        r'\bREADME\b',
        r'\bscripts?/',
        r'\.sh\b',
        r'\.py\b',
        r'\bsystemd\b',
        r'\bdocker\b',
        r'\bport\b',
        r'\bgit\b',
        r'\brepo\b',
    ]
]

SENSITIVE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r'\btoken\b',
        r'\bsecret\b',
        r'\bpassword\b',
        r'\bpasswd\b',
        r'\bapi[_ -]?key\b',
        r'\bcredential',
        r'sk-[A-Za-z0-9_-]{8,}',
    ]
]


class SessionParseError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create a review digest for Session → Memory/Vault/Repo promotion.')
    parser.add_argument('session_file', type=Path, help='Path to a Hermes session file (.jsonl or .json)')
    parser.add_argument('--vault-root', type=Path, default=None, help='Vault root path (default: HERMES_WORKSPACE_VAULT or ~/hermes-workspace/vault)')
    parser.add_argument('--output', type=Path, default=None, help='Explicit output markdown path')
    parser.add_argument('--stdout', action='store_true', help='Print digest to stdout instead of writing a file')
    return parser.parse_args()


def default_vault_root() -> Path:
    env_value = os.environ.get('HERMES_WORKSPACE_VAULT')
    if env_value:
        return Path(env_value).expanduser()
    candidate = Path('~/hermes-workspace/vault').expanduser()
    if candidate.exists():
        return candidate
    return Path('~/obsidian-vault').expanduser()


def load_session_rows(path: Path) -> list[dict]:
    rows = []
    raw = path.read_text(encoding='utf-8').strip()
    if not raw:
        return rows

    # Try JSON array first (session_*.json format — TUI/CLI sessions)
    if raw.startswith('['):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
        except json.JSONDecodeError:
            pass

    # Try single JSON object (API dumps, some session formats)
    if raw.startswith('{'):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Could be a single message or a wrapper object with a messages key
                if 'messages' in parsed and isinstance(parsed['messages'], list):
                    return [m for m in parsed['messages'] if isinstance(m, dict)]
                return [parsed]
        except json.JSONDecodeError:
            pass

    # Fallback: JSONL format (one JSON object per line — Signal sessions)
    with path.open('r', encoding='utf-8') as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionParseError(f'Invalid JSON on line {line_number}: {exc}') from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def is_candidate_text(content: str) -> bool:
    stripped = ' '.join(content.split())
    if len(stripped) < 20:
        return False
    if stripped.startswith('{') and stripped.endswith('}'):
        return False
    return True


def classify_candidate(content: str) -> set[str]:
    categories: set[str] = set()
    if any(pattern.search(content) for pattern in MEMORY_PATTERNS):
        categories.add('memory')
    if any(pattern.search(content) for pattern in VAULT_PATTERNS):
        categories.add('vault')
    if any(pattern.search(content) for pattern in REPO_PATTERNS):
        categories.add('repo')
    return categories


def contains_sensitive_info(content: str) -> bool:
    return any(pattern.search(content) for pattern in SENSITIVE_PATTERNS)


def sanitize_content(content: str) -> str:
    sanitized = content
    sanitized = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[REDACTED]', sanitized)
    sanitized = re.sub(
        r'(?i)\b(token|secret|password|passwd|api[_ -]?key|credential)\b([^\n]*)',
        lambda match: f"{match.group(1)} [REDACTED]",
        sanitized,
    )
    return sanitized


def extract_candidates(rows: Iterable[dict]) -> tuple[dict[str, list[dict]], Counter]:
    buckets = {'memory': [], 'vault': [], 'repo': [], 'sensitive': []}
    role_counts: Counter = Counter()
    seen: set[tuple[str, str]] = set()

    for row in rows:
        role = row.get('role', 'unknown')
        role_counts[role] += 1
        if role not in {'user', 'assistant'}:
            continue

        content = row.get('content')
        if not isinstance(content, str) or not is_candidate_text(content):
            continue

        if contains_sensitive_info(content):
            sanitized = ' '.join(sanitize_content(content).split())
            key = ('sensitive', sanitized)
            if key not in seen:
                seen.add(key)
                buckets['sensitive'].append({'role': role, 'content': sanitized})
            continue

        categories = classify_candidate(content)
        if not categories:
            continue

        normalized = ' '.join(content.split())
        for category in categories:
            key = (category, normalized)
            if key in seen:
                continue
            seen.add(key)
            buckets[category].append({'role': role, 'content': normalized})

    return buckets, role_counts


def format_candidate_lines(items: list[dict]) -> str:
    if not items:
        return '- _(none detected)_'
    return '\n'.join(f"- **{item['role']}**: {item['content']}" for item in items)


def detect_session_timestamp(rows: list[dict], session_stem: str) -> str:
    for row in rows:
        timestamp = row.get('timestamp')
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    try:
        dt = datetime.strptime(session_stem.split('.')[0], '%Y%m%d_%H%M%S_%f')
    except ValueError:
        parts = session_stem.split('_')
        if len(parts) >= 2:
            try:
                dt = datetime.strptime('_'.join(parts[:2]), '%Y%m%d_%H%M%S')
                return dt.isoformat()
            except ValueError:
                pass
        return 'unknown'
    return dt.isoformat()


def build_markdown(session_path: Path, rows: list[dict], buckets: dict[str, list[dict]], role_counts: Counter) -> str:
    session_id = session_path.stem
    observed_at = detect_session_timestamp(rows, session_id)
    total_candidates = sum(len(items) for items in buckets.values())

    return f"""---
type: inbox
status: draft
review_status: pending
review_reason: "Session digest needs human selection before promotion"
created: {datetime.now().date().isoformat()}
source: session
session_id: {session_id}
session_file: {session_path}
---

# Session Promotion - {session_id}

## Purpose
Review-oriented digest for promoting durable knowledge out of a raw session into Memory, Obsidian, or the Homebase repo.

## Session metadata
- Session file: `{session_path}`
- Observed at: `{observed_at}`
- Message counts: `{dict(role_counts)}`
- Candidate hits: `{total_candidates}`

## Review rules
- Memory = compact stable preferences or environment facts
- Vault = durable facts, decisions, runbooks, or project knowledge
- Repo = scripts, docs, templates, or canonical operational references
- Anything noisy, temporary, or discarded should remain only in sessions

## Memory candidates
{format_candidate_lines(buckets['memory'])}

## Vault candidates
{format_candidate_lines(buckets['vault'])}

## Repo candidates
{format_candidate_lines(buckets['repo'])}

## Sensitive lines suppressed
{format_candidate_lines(buckets['sensitive'])}

## Next actions
1. Keep only durable items.
2. Promote stable human-readable knowledge into the vault.
3. Promote scripts/templates/docs into the appropriate project repository.
4. Update Hermes Memory only for compact stable facts or preferences.
"""


def resolve_output_path(args: argparse.Namespace, session_path: Path) -> Path:
    if args.output:
        return args.output
    vault_root = args.vault_root or default_vault_root()
    return vault_root / '00_inbox' / f'Session Promotion - {session_path.stem}.md'


def main() -> int:
    args = parse_args()
    session_path = args.session_file.expanduser().resolve()
    if not session_path.exists():
        print(f'ERROR session file not found: {session_path}', file=os.sys.stderr)
        return 1

    try:
        rows = load_session_rows(session_path)
        buckets, role_counts = extract_candidates(rows)
        markdown = build_markdown(session_path, rows, buckets, role_counts)

        if args.stdout:
            print(markdown)
            return 0

        output_path = resolve_output_path(args, session_path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding='utf-8')
        print(f'WROTE {output_path}')
        return 0
    except SessionParseError as exc:
        print(f'ERROR invalid session log: {exc}', file=os.sys.stderr)
        return 1
    except OSError as exc:
        print(f'ERROR file operation failed: {exc}', file=os.sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
