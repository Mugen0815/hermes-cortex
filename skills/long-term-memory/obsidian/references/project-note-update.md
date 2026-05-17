# Updating a project note

Workflow for updating a project note in the vault after a work session.

## Steps

1. **Check roadmap** — `vault_read_note("30_projects/Project - <name>.md", "Roadmap")` and identify current phase
2. **Capture git log** — `git -C <checkout> log --oneline -5` for recent commits
3. **Update frontmatter** — `updated: YYYY-MM-DD`, adjust `status:` if needed
4. **Update status section** — brief timeline of completed phase + git log
5. **Save** — `vault_create_note()` or `write_file()` into the vault
6. **Update index** — `hermes cortex lifecycle maintenance`
7. **Update map note** — if a new wikilink is needed, add to `60_maps/Map - Knowledge Index.md`

## Example

See `vault/30_projects/Project - hermes-cortex.md` for a live instance.
