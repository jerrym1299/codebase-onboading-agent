from services.chunk_and_embed import CodeChunk, embed_chunks
from services.db import get_cached_chunk_embeddings, store_cached_chunk_embeddings


async def hydrate_embeddings(repo_url: str, chunks: list[CodeChunk]) -> dict:
    """Attach cached embeddings where possible, then embed/cache misses."""
    cached = await get_cached_chunk_embeddings(repo_url, chunks)
    cached_count = 0
    for chunk in chunks:
        if chunk.embedding_sha256 and chunk.embedding_sha256 in cached:
            chunk.embedding = cached[chunk.embedding_sha256]
            cached_count += 1

    pending_by_hash: dict[str, CodeChunk] = {}
    uncacheable: list[CodeChunk] = []
    for chunk in chunks:
        if chunk.embedding is not None:
            continue
        if chunk.embedding_sha256:
            pending_by_hash.setdefault(chunk.embedding_sha256, chunk)
        else:
            uncacheable.append(chunk)

    unique_pending = list(pending_by_hash.values())
    embedded_count = embed_chunks(unique_pending + uncacheable)

    embedded_by_hash = {
        chunk.embedding_sha256: chunk.embedding
        for chunk in unique_pending
        if chunk.embedding_sha256 and chunk.embedding is not None
    }
    for chunk in chunks:
        if chunk.embedding is None and chunk.embedding_sha256 in embedded_by_hash:
            chunk.embedding = embedded_by_hash[chunk.embedding_sha256]

    stored_count = await store_cached_chunk_embeddings(repo_url, unique_pending)
    return {
        "cached": cached_count,
        "embedded": embedded_count,
        "stored": stored_count,
        "total": len(chunks),
    }
