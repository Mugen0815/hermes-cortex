# Cortex Search Scoring Triage Notes

Use this reference when evaluating whether `vault_search` / `hermes cortex search` ranking should become a Kanban triage ticket.

## Diagnostic commands

Run searches through the Hermes runtime Python environment, not system Python, so Cortex dependencies are available:

```bash
hermes cortex search 'hermes-cortex scoring search ranking BM25 vector RRF' --top-k 8 --json
```

For boosted vs unboosted comparison, use the Hermes venv directly and call `HybridSearcher.search(..., apply_boost=...)`:

```bash
$HOME/.hermes/hermes-agent/venv/bin/python3 - <<'PY'
from cortex.config import load_config
from cortex.search import HybridSearcher

q = 'hermes-cortex scoring search ranking BM25 vector RRF'
cfg = load_config()
s = HybridSearcher(cfg)
for boost in (False, True):
    print('\nboost', boost)
    for i, r in enumerate(s.search(q, top_k=8, apply_boost=boost), 1):
        print(
            f"{i}. final={r.final_score:.5f} rrf={r.rrf_score:.5f} "
            f"mult={r.debug.get('boost_multiplier'):.2f} "
            f"br={r.bm25_rank} vr={r.vector_rank} "
            f"file={r.chunk.get('file')} heading={r.chunk.get('heading')}"
        )
PY
```

Check graph health separately:

```bash
hermes cortex graph status
```

## What to look for

### 1. Boost dominance

Default boosts are multiplicative:

```text
final = rrf * (1 + recency_factor) * (1 + importance_factor)
```

With defaults, max multiplier is `1.20 * 1.30 = 1.56`. If a highly relevant chunk is rank #1 unboosted but drops several positions after boosts, scoring is being dominated by metadata freshness/importance rather than retrieval relevance.

Observed pattern: `Fact - hermes-cortex Phase 3 and 4 Implementation` was #1 unboosted for a Cortex scoring query, but dropped to #5 boosted because newer/high-importance project-summary chunks received ~1.56x.

### 2. Link-only chunk dominance

Watch for top results with headings like `Links` / `Related` and bodies that are mostly wikilinks. BM25 can over-rank these because they are short and contain exact note names. They are useful graph evidence but usually weak answer context.

Potential fix: add a low-information chunk penalty for `Links`/`Related` or wikilink-heavy chunks unless the query explicitly asks for links/relations.

### 3. German/natural-language query weakness

Queries like `was weißt du über scoring von cortex` may not surface the architecture/scoring notes, because the vault mixes German natural language with English technical terms (`ranking`, `RRF`, `boosts`, `search`). Evaluate with both German and English test queries before changing defaults.

Potential fixes:
- stronger multilingual embedding model
- synonym/query expansion for technical terms
- optional cross-encoder reranker on top-N candidates

### 4. Graph channel not contributing

If many results show `graph_rank=None` despite `hermes cortex graph status` reporting a healthy graph, the graph channel is not materially affecting search quality for that query class. Add diagnostics for seed expansion before tuning graph weight.

## Triage ticket shape

Title:

```text
Improve Cortex search scoring: reduce boost dominance, downrank link-only chunks, add reranking diagnostics
```

Acceptance criteria:

- Architecture/scoring queries rank the Cortex hybrid-search implementation note above generic project-summary/link chunks.
- Link-only chunks do not dominate top-5 unless the query asks for links/relations.
- Boosts cannot move a clearly less relevant chunk above a much stronger BM25+Vector hit without diagnostic visibility.
- German query `was weißt du über scoring von cortex` returns Cortex search/scoring architecture in top results.
- Existing search tests remain green.
