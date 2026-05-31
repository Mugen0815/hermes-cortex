# Frontmatter Edge Cases

## Missing closing `---` delimiter

**Symptom:** Graph viewer shows "suspicious memory" flags such as "missing
status" or "missing domain" for notes that visibly contain those fields in YAML.

**Root cause:** The file has an opening `---` but no closing `---` before the
body begins. The cortex parser regex (`^---[ \t]*\n(.*?)\n---[ \t]*\n`)
requires both delimiters. Without a closing `---`, it returns an empty
frontmatter dict.

**Example pattern:** A note starts with YAML-like fields and then immediately
continues into Markdown content without the second delimiter.

**Fix:**
1. Verify with `python3 -c "import re; text=open('path/to/file.md').read(); print(bool(re.search(r'^---[ \t]*\n(.*?)\n---[ \t]*\n', text, re.DOTALL)))"` — should print `True`
2. Add `---` as the line between frontmatter and body
3. `cortex index --force && cortex graph build --force` to rebuild

## Unknown enum values

The `cortex.frontmatter` module validates enum-like fields against the canonical
schema documented in `docs/METADATA.md`. Current public status values are:

- `active`
- `draft`
- `archived`
- `deprecated`
- `stale`
- `superseded`

Values such as `proposed`, `planned`, `approved`, `implemented`, or `review` are
workflow labels, not canonical `status` values. They produce a non-fatal warning
and may not appear correctly in status filters:

```text
unknown status 'proposed'
unknown status 'planned'
```

Use a canonical `status` and move workflow state into a separate field, for
example:

```yaml
status: draft
review_status: pending
roadmap_phase: planned
```

Canonical `stability` values are `stable`, `evolving`, and `experimental`. Do not
use `draft` or `deprecated` as `stability`; lifecycle belongs in `status`.

## Quoted dates in frontmatter

Dates with quotes (`last_verified: '2026-05-05'`) are valid YAML but may cause
issues with some frontmatter parsers that expect unquoted ISO dates. Cortex
handles them, but for compatibility prefer unquoted:

```yaml
last_verified: 2026-05-05    # preferred
last_verified: '2026-05-05'  # also works but may confuse some parsers
```

## Detection script

```python
import re
from pathlib import Path

vault = Path('/path/to/vault')
issues = []

for f in sorted(vault.rglob('*.md')):
    if any(p.startswith('.') for p in f.relative_to(vault).parts):
        continue
    text = f.read_text(encoding='utf-8')
    fm_re = re.compile(r'^---[ \t]*\n(.*?)\n---[ \t]*\n', re.DOTALL)
    if text.startswith('---') and not fm_re.match(text):
        issues.append(f'no closing ---: {f.relative_to(vault)}')

for i in issues:
    print(i)
```
