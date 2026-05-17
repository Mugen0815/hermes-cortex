# Frontmatter Edge Cases

## Missing closing `---` delimiter

**Symptom:** Graph viewer shows "suspicious memory" flags: "missing status", "missing domain" for notes that clearly have those fields in their YAML.

**Root cause:** The file has an opening `---` but no closing `---` before the body begins. The cortex parser regex (`^---[ \t]*\n(.*?)\n---[ \t]*\n`) requires both delimiters. Without a closing `---`, it returns an empty frontmatter dict.

**Real-world example (May 2026):** Three notes in the vault:
- `10_facts/Hermes TTS Stimme.md` — frontmatter content present, no closing `---`
- `10_facts/Hermes Vision Limitation.md` — same
- `10_facts/Integration - Audio STT und YouTube.md` — no frontmatter at all (started with `# Title`)

**Fix:**
1. Verify with `python3 -c "import re; text=open('path/to/file.md').read(); print(bool(re.search(r'^---[ \t]*\n(.*?)\n---[ \t]*\n', text, re.DOTALL)))"` — should print `True`
2. Add `---` as the line between frontmatter and body
3. `cortex index --force && cortex graph build --force` to rebuild

## Unknown status values

The `cortex frontmatter` module validates against known enums. Known status values:
- `active`, `archived`, `draft`, `proposed`, `planned`

Non-standard values like `"proposed"` or `"planned"` (previously unseen) produce a warning during indexing:
```
unknown status 'proposed'
unknown status 'planned'
unknown stability 'draft'
```

These warnings are **non-fatal** — the note is still indexed — but the graph viewer's status filter won't recognise them.

## Quoted dates in frontmatter

Dates with quotes (`last_verified: '2026-05-05'`) are valid YAML but may cause issues with some frontmatter parsers that expect unquoted ISO dates. Cortex handles them, but for compatibility prefer unquoted:

```yaml
last_verified: 2026-05-05  # preferred
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
