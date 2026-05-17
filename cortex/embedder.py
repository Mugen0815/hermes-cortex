"""Embedding pipeline for hermes-cortex.

Reads chunks.jsonl, computes embeddings with sentence-transformers, upserts
into Chroma. Incremental via content_hash comparison stored in Chroma metadata.

Locked decisions for Phase 3:

- **Numeric metadata stays numeric.** ``confidence`` (0..1) and
  ``importance`` (1..5) are written to Chroma as floats so range filters
  (``{"$gte": 0.5}``) and boost math work natively.

- **Lists become "flat" sentinels** (``tags_flat="|foo|bar|"``) for
  debug/transport purposes. These are **NOT** the canonical filtering path.
  Chroma's string-metadata does not guarantee reliable substring/contains
  semantics across versions. Tag and wikilink membership filtering happens
  in the Search layer against the arrays stored in ``chunks.jsonl``
  (or a sidecar BM25 index). The ``*_flat`` fields exist only for
  human readability in Chroma Browse / debugging.

- **Chroma's authoritative role** is:
    1. Vector retrieval (ANN/cosine).
    2. Scalar metadata filters on real types:
       - ``confidence`` / ``importance`` (float, range filters)
       - ``modified_date`` (ISO string, lexicographic range)
       - ``file`` / ``id`` (exact string match)
    Tag / wikilink / domain membership filtering is **always** done
    post-fetch in the Search layer.

- **Model/dimension guard.** Every collection records ``embedding_model``
  and ``embedding_dim`` in its collection metadata at first write. On every
  subsequent run we refuse to upsert when either differs and tell the user
  to run ``cortex reset``. No silent corruption.

- **Length reporting.** The report carries char/token stats so users can
  see when chunks are likely to exceed the model's context window. We do
  NOT truncate ourselves — the tokenizer truncates internally, we just warn.

Device selection:
    cfg.embeddings.device may be 'cpu', 'cuda', 'mps', or 'auto'.
    'auto' picks cuda > mps > cpu based on availability.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cortex.config import Config
from cortex.text import estimate_tokens

log = logging.getLogger("cortex.embedder")


# ---- Constants -------------------------------------------------------------

# Collection-level metadata keys we use to guard against model/dim changes.
COLLECTION_META_MODEL = "embedding_model"
COLLECTION_META_DIM = "embedding_dim"

# Soft warning threshold: chunks above this estimated token count are likely
# to be truncated by most sentence-transformers (which max out at 256-512).
# Reported, not enforced.
TOKEN_WARN_THRESHOLD = 500


class ModelMismatchError(RuntimeError):
    """Raised when the configured embedding model/dim doesn't match the existing collection."""


# ---- Device auto-detect ---------------------------------------------------


def detect_device(requested: str = "auto") -> str:
    """Resolve a device preference into an actual device string.

    'auto' picks cuda > mps > cpu. Explicit 'cuda'/'mps' fall back to cpu
    with a warning if unavailable. 'cpu' is always honored.
    """
    requested = (requested or "auto").lower().strip()
    if requested == "cpu":
        return "cpu"

    try:
        import torch  # noqa: F401
    except ImportError:
        log.warning("torch not installed; falling back to cpu")
        return "cpu"

    import torch  # type: ignore[no-redef]

    has_cuda = torch.cuda.is_available()
    has_mps = getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()

    if requested == "auto":
        if has_cuda:
            return "cuda"
        if has_mps:
            return "mps"
        return "cpu"
    if requested == "cuda":
        if has_cuda:
            return "cuda"
        log.warning("cuda requested but unavailable; falling back to cpu")
        return "cpu"
    if requested == "mps":
        if has_mps:
            return "mps"
        log.warning("mps requested but unavailable; falling back to cpu")
        return "cpu"
    log.warning("Unknown device '%s'; falling back to cpu", requested)
    return "cpu"


# ---- Report ---------------------------------------------------------------


@dataclass
class EmbedReport:
    chunks_total: int = 0
    chunks_embedded: int = 0
    chunks_skipped_unchanged: int = 0
    chunks_removed: int = 0
    chunks_over_token_threshold: int = 0
    max_chunk_chars: int = 0
    max_chunk_tokens_est: int = 0
    device: str = ""
    model: str = ""
    embedding_dim: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Embedded {self.chunks_embedded}/{self.chunks_total} chunks "
            f"({self.chunks_skipped_unchanged} unchanged, "
            f"{self.chunks_removed} removed). "
            f"Model: {self.model} (dim={self.embedding_dim}) on {self.device}. "
            f"Largest chunk: {self.max_chunk_chars} chars / "
            f"~{self.max_chunk_tokens_est} tokens; "
            f"{self.chunks_over_token_threshold} above {TOKEN_WARN_THRESHOLD}-token warn threshold. "
            f"Errors: {len(self.errors)}."
        )


# ---- chunks.jsonl helpers --------------------------------------------------


def load_chunks(chunks_path: Path) -> list[dict[str, Any]]:
    if not chunks_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("Skipping malformed chunks.jsonl line: %s", e)
    return out


def chunk_text_for_embedding(chunk: dict[str, Any]) -> str:
    """Compose the text we actually embed.

    Includes the heading path (joined by ' / ') + body so a section about
    "Memory Model" embeds its title context, not just the prose. We use the
    full heading_path when available, falling back to the leaf heading for
    backwards compatibility with chunks written by older versions.
    """
    path = chunk.get("heading_path")
    if path:
        prefix = " / ".join(p for p in path if p)
    else:
        prefix = chunk.get("heading") or ""
    text = chunk.get("text") or ""
    if prefix:
        return f"{prefix}\n\n{text}"
    return text


# ---- Chroma metadata projection -------------------------------------------


def _flat_list(values: list[str] | None) -> str:
    """Encode a list as ``|a|b|c|`` for debug/transport in Chroma metadata.

    **NOT for filtering.** Chroma does not provide reliable substring/contains
    semantics on string metadata. This field exists for human readability in
    Chroma Browse. Tag and wikilink membership checks always happen in the
    Search layer against the source arrays in ``chunks.jsonl``.
    Empty list → empty string (field is dropped from Chroma metadata).
    """
    if not values:
        return ""
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    if not cleaned:
        return ""
    return "|" + "|".join(cleaned) + "|"


def _as_float(v: Any, default: float = 0.0) -> float:
    """Best-effort float coercion; returns default on failure."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):  # bool is a subclass of int; reject explicitly
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def chunk_metadata_for_chroma(chunk: dict[str, Any]) -> dict[str, Any]:
    """Flatten chunk metadata for Chroma. Chroma metadata must be scalar-only.

    Schema (Phase 3 contract):
      file, folder, heading, heading_path        — strings (heading_path joined by " / ")
      modified, modified_date, last_verified     — ISO strings
      type, status, domain, project, stability   — strings (enums; may be empty-dropped)
      confidence                                 — float in [0, 1]
      importance                                 — float in [1, 5]
      tags_flat, wikilinks_flat                  — "|a|b|" debug/transport only (NOT for Chroma filtering)
      char_len, token_estimate                   — int
      content_hash                               — string (incremental key)
    """
    fm = chunk.get("fm_normalized") or {}
    # Backwards-compat: older chunks.jsonl files may only have raw frontmatter.
    raw_fm = chunk.get("frontmatter") or {}

    heading_path_str = " / ".join(p for p in (chunk.get("heading_path") or []) if p)

    md: dict[str, Any] = {
        "file": chunk.get("file", ""),
        "folder": chunk.get("folder", ""),
        "heading": chunk.get("heading") or "",
        "heading_path": heading_path_str,
        "modified": chunk.get("modified", ""),
        "modified_date": chunk.get("modified_date", "") or chunk.get("modified", ""),
        "content_hash": chunk.get("content_hash", ""),
        # Categorical frontmatter
        "type": str(fm.get("type") or raw_fm.get("type") or ""),
        "status": str(fm.get("status") or raw_fm.get("status") or ""),
        "domain": str(fm.get("domain") or raw_fm.get("domain") or ""),
        "project": str(fm.get("project") or raw_fm.get("project") or ""),
        "stability": str(fm.get("stability") or raw_fm.get("stability") or ""),
        "last_verified": str(fm.get("last_verified") or raw_fm.get("last_verified") or ""),
        # Numeric frontmatter (real numbers!)
        "confidence": _as_float(fm.get("confidence"), default=0.5),
        "importance": _as_float(fm.get("importance"), default=3.0),
        # Flat list sentinels — debug/transport only, NOT for Chroma filtering.
        # Canonical tag/wikilink membership checks run in the Search layer
        # against the arrays in chunks.jsonl. See _flat_list() docstring.
        "tags_flat": _flat_list(chunk.get("tags")),
        "wikilinks_flat": _flat_list(chunk.get("wikilinks")),
        # Length stats (useful for diagnostics + Phase-3 budget logic)
        "char_len": int(chunk.get("char_len") or len(chunk.get("text") or "")),
        "token_estimate": int(
            chunk.get("token_estimate") or estimate_tokens(chunk.get("text") or "")
        ),
    }
    # Drop string keys that are empty so Chroma's storage stays clean and
    # ``$ne`` filters behave predictably. Numeric fields are kept regardless.
    out: dict[str, Any] = {}
    for k, v in md.items():
        if isinstance(v, str):
            if v != "":
                out[k] = v
        else:
            out[k] = v
    return out


# ---- Index hash (incremental key) -----------------------------------------


def index_hash_for_chunk(chunk: dict[str, Any]) -> str:
    """Deterministic hash over what actually ends up in Chroma for this chunk.

    Combines the embedding text + the Chroma metadata projection. This is
    strictly stronger than ``content_hash`` (which only covers the source
    file): a metadata-only edit (e.g. confidence bumped from 0.5 → 0.8) or
    a change to ``chunk_text_for_embedding`` semantics now also invalidates
    the cache and triggers a re-embed.

    The hash is **not** itself part of the metadata projection — that would
    be circular. It's added on top by the caller during upsert.
    """
    payload = {
        "embedding_text": chunk_text_for_embedding(chunk),
        "metadata": chunk_metadata_for_chroma(chunk),
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---- Chroma collection ----------------------------------------------------


def open_collection(cfg: Config):
    """Open or create the Chroma collection. Imported lazily."""
    import chromadb

    cfg.index.chroma_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(cfg.index.chroma_path))
    return client.get_or_create_collection(
        name=cfg.index.collection,
        metadata={"hnsw:space": "cosine"},
    )


def _collection_meta(collection) -> dict[str, Any]:
    """Read collection.metadata defensively across Chroma versions."""
    meta = getattr(collection, "metadata", None)
    if isinstance(meta, dict):
        return dict(meta)
    return {}


def _stamp_collection_meta(collection, model: str, dim: int) -> None:
    """Record (or update) the embedding model + dim on the collection.

    Uses ``modify`` if available, falls back to mutating ``.metadata``. We
    always preserve any existing keys (e.g. ``hnsw:space``).

    Failure modes are logged at WARNING level — silently dropping a stamp
    would weaken the model-/dim-mismatch guard, so we make the inability
    to persist visible.
    """
    current = _collection_meta(collection)
    new_meta = dict(current)
    new_meta[COLLECTION_META_MODEL] = model
    new_meta[COLLECTION_META_DIM] = int(dim)

    modify = getattr(collection, "modify", None)
    if callable(modify):
        try:
            modify(metadata=new_meta)
            return
        except Exception as e:  # noqa: BLE001
            log.warning(
                "collection.modify(metadata=...) failed (%s); "
                "falling back to direct attribute set",
                e,
            )
    # Fallback: best-effort attribute set (in-memory only on some backends).
    try:
        collection.metadata = new_meta  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Could not persist collection metadata (model=%r, dim=%d): %s. "
            "Model/dim guard will not survive a process restart on this backend.",
            model,
            dim,
            e,
        )


def _check_model_compatibility(collection, model: str, dim: int) -> None:
    """Raise ModelMismatchError if collection was built with a different model/dim.

    A collection with no recorded model is treated as "fresh" — we'll stamp it.
    """
    meta = _collection_meta(collection)
    existing_model = meta.get(COLLECTION_META_MODEL)
    existing_dim = meta.get(COLLECTION_META_DIM)
    if existing_model is None and existing_dim is None:
        return  # fresh collection
    if existing_model and existing_model != model:
        raise ModelMismatchError(
            f"Embedding model mismatch: collection was built with "
            f"{existing_model!r} but config says {model!r}. "
            f"Run `cortex reset --chroma` to wipe the vector store and re-embed."
        )
    if existing_dim and int(existing_dim) != int(dim):
        raise ModelMismatchError(
            f"Embedding dimension mismatch: collection has dim={existing_dim} "
            f"but model {model!r} produces dim={dim}. "
            f"Run `cortex reset --chroma` to wipe the vector store and re-embed."
        )


def existing_ids_with_hash(collection) -> tuple[set[str], dict[str, str]]:
    """Return ``(all_ids, id_to_hash)`` from Chroma.

    ``all_ids`` includes every entry in the collection — even ones with no
    recorded hash — so the caller can detect them as orphans during cleanup.
    Without this, stale entries from older versions (no ``index_hash`` /
    ``content_hash``) would be invisible and never removed.

    ``id_to_hash`` prefers ``index_hash`` (the strict incremental key) and
    falls back to ``content_hash`` for entries written by older code paths.
    Entries with neither hash are present in ``all_ids`` but absent from
    the mapping; the caller treats them as definitely-needs-re-embed.
    """
    all_ids: set[str] = set()
    id_to_hash: dict[str, str] = {}
    try:
        result = collection.get(include=["metadatas"])
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read existing Chroma collection: %s", e)
        return all_ids, id_to_hash
    ids = result.get("ids") or []
    metas = result.get("metadatas") or []
    for cid, meta in zip(ids, metas):
        all_ids.add(cid)
        if isinstance(meta, dict):
            h = meta.get("index_hash") or meta.get("content_hash")
            if h:
                id_to_hash[cid] = h
    return all_ids, id_to_hash


# ---- Embedder -------------------------------------------------------------


class Embedder:
    """Wraps a sentence-transformers model. Lazy-loads on first encode."""

    def __init__(self, model_name: str, device: str):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._dim: Optional[int] = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        log.info("Loading embedding model %s on %s", self.model_name, self.device)
        self._model = SentenceTransformer(self.model_name, device=self.device)

    @property
    def dim(self) -> int:
        """Embedding dimensionality. Triggers model load on first access."""
        if self._dim is not None:
            return self._dim
        self._load()
        assert self._model is not None
        # sentence-transformers exposes get_sentence_embedding_dimension(); fall
        # back to encoding a probe string if missing (some mocks / forks).
        getter = getattr(self._model, "get_sentence_embedding_dimension", None)
        if callable(getter):
            d = getter()
            if isinstance(d, int) and d > 0:
                self._dim = d
                return d
        probe = self._model.encode(
            ["dimension probe"],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        self._dim = int(probe.shape[-1])
        return self._dim

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        assert self._model is not None
        vectors = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return [v.tolist() for v in vectors]


# ---- Top-level: embed_chunks ----------------------------------------------


def embed_chunks(cfg: Config, *, force: bool = False, batch_size: int = 32) -> EmbedReport:
    """Read chunks.jsonl, embed new/changed chunks, upsert into Chroma.

    Incremental key is ``index_hash`` (covers embedding text + Chroma
    metadata projection), with fallback to ``content_hash`` when reading
    older Chroma entries. Removes chunks from Chroma that no longer appear
    in chunks.jsonl — orphan cleanup runs **regardless of ``force``** so
    a forced re-embed still leaves the store consistent.

    Refuses to run if the collection was built with a different model/dim.
    """
    device = detect_device(cfg.embeddings.device)
    report = EmbedReport(device=device, model=cfg.embeddings.model)

    chunks = load_chunks(cfg.index.chunks_path)
    report.chunks_total = len(chunks)
    if not chunks:
        log.warning("No chunks to embed at %s", cfg.index.chunks_path)
        return report

    # Length stats (cheap; informational).
    for c in chunks:
        cl = int(c.get("char_len") or len(c.get("text") or ""))
        te = int(c.get("token_estimate") or estimate_tokens(c.get("text") or ""))
        if cl > report.max_chunk_chars:
            report.max_chunk_chars = cl
        if te > report.max_chunk_tokens_est:
            report.max_chunk_tokens_est = te
        if te > TOKEN_WARN_THRESHOLD:
            report.chunks_over_token_threshold += 1

    collection = open_collection(cfg)

    # Determine target dimension by lazy-loading the model. We need this BEFORE
    # the compatibility check so we can compare against the recorded dim.
    embedder = Embedder(cfg.embeddings.model, device)
    target_dim = embedder.dim
    report.embedding_dim = target_dim

    # Refuse on model/dim mismatch (loud failure beats silent corruption).
    _check_model_compatibility(collection, cfg.embeddings.model, target_dim)
    # Stamp metadata (idempotent; no-op if already set to the same values).
    _stamp_collection_meta(collection, cfg.embeddings.model, target_dim)

    # Always read the existing index — even on force=True — so orphan cleanup
    # works regardless of whether we're skipping unchanged chunks or not.
    existing_ids, existing_hashes = existing_ids_with_hash(collection)

    to_embed_ids: list[str] = []
    to_embed_texts: list[str] = []
    to_embed_meta: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for c in chunks:
        raw_id = c.get("id")
        if not raw_id:
            report.errors.append(("<missing id>", "chunk has no 'id' field; skipped"))
            log.warning("Chunk without id encountered; skipping (file=%r)", c.get("file"))
            continue
        cid = str(raw_id)

        # Detect duplicate IDs in chunks.jsonl. Skipping the second occurrence
        # avoids silent overwrite (which would let the *last* duplicate win
        # depending on iteration order — a fragile correctness contract).
        if cid in seen_ids:
            report.errors.append((cid, "duplicate chunk id in chunks.jsonl; skipped"))
            log.warning("Duplicate chunk id %r in chunks.jsonl; skipping", cid)
            continue
        seen_ids.add(cid)

        idx_hash = index_hash_for_chunk(c)
        if not force and existing_hashes.get(cid) == idx_hash:
            report.chunks_skipped_unchanged += 1
            continue

        meta = chunk_metadata_for_chroma(c)
        # Stamp the incremental key onto the stored metadata. Kept separate
        # from chunk_metadata_for_chroma() to avoid circular hashing.
        meta["index_hash"] = idx_hash

        to_embed_ids.append(cid)
        to_embed_texts.append(chunk_text_for_embedding(c))
        to_embed_meta.append(meta)

    # Remove orphans: in Chroma but not in chunks.jsonl anymore. Runs even on
    # force=True so a forced re-embed cannot leave dangling vectors.
    orphans = [cid for cid in existing_ids if cid not in seen_ids]
    if orphans:
        try:
            collection.delete(ids=orphans)
            report.chunks_removed = len(orphans)
        except Exception as e:  # noqa: BLE001
            report.errors.append(("<delete orphans>", str(e)))

    # Embed and upsert in batches.
    if to_embed_ids:
        for start in range(0, len(to_embed_ids), batch_size):
            ids_b = to_embed_ids[start : start + batch_size]
            texts_b = to_embed_texts[start : start + batch_size]
            meta_b = to_embed_meta[start : start + batch_size]
            try:
                vectors = embedder.encode(texts_b, batch_size=batch_size)
                collection.upsert(
                    ids=ids_b,
                    embeddings=vectors,
                    documents=texts_b,
                    metadatas=meta_b,
                )
                report.chunks_embedded += len(ids_b)
            except Exception as e:  # noqa: BLE001
                for cid in ids_b:
                    report.errors.append((cid, str(e)))

    return report


# ---- Reset helper (used by `cortex reset`) --------------------------------


def reset_chroma(cfg: Config) -> Path:
    """Delete the Chroma directory entirely. Returns the path that was removed.

    Caller is responsible for confirming with the user; this function just
    does the work. Safe to call when the directory doesn't exist (no-op).
    """
    import shutil

    target = cfg.index.chroma_path
    if target.exists():
        shutil.rmtree(target)
    return target
