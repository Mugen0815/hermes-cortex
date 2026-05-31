# vault_search Ranking Diagnostic

This public reference explains why an existing note may not appear in a small
`vault_search` result window. Keep real note names, local paths, scores, and
one-off search transcripts in private operator notes.

## Problem

A specific known note exists on disk and is indexed, but `vault_search(...,
top_k=10)` does not return it.

## Cause

BM25 can strongly favor short, exact wikilink chunks over the long target note.
For example, many notes may contain a compact link such as:

```markdown
- [[Project - Example System]]
```

That tiny chunk can score better than the full project note because the query
terms are close together and not diluted across a long body. In a small `top_k`,
short link/index chunks can crowd out the intended long-form note.

## Four-step diagnosis

### 1. Raise `top_k`

```python
vault_search("Project Example System", top_k=30)
```

Still absent? Continue.

### 2. Verify the file exists

```python
search_files(pattern="*Example System*", target="files", path="/path/to/vault")
```

Filesystem search bypasses the index entirely and is the fastest existence check.

### 3. Check index membership

```bash
grep '"file": "30_projects/Project - Example System.md"' ~/.hermes/cortex/chunks.jsonl | wc -l
```

If the count is `0`, rebuild or inspect frontmatter/exclusion rules. If chunks
exist but search still misses the note, this is ranking behavior rather than an
indexing miss.

### 4. Read the note directly

```python
vault_read_note(file="30_projects/Project - Example System.md")
```

For known specific notes, direct read is valid after filesystem/index checks.
Search is for discovery; it is not a guarantee that every known long-form note
will appear in a small result window. Charming, in the way ranking systems are.

## Prevention

- Use `top_k=20+` when searching for a specific note title.
- Pair `vault_search` with `search_files(target="files")` for exact filename/title
  lookups.
- Prefer `vault_read_note` once the path is known.
- Keep short map/link notes useful, but do not mistake them for canonical content.
