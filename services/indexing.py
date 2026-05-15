import logging

from services.chunk_and_embed import chunk_file_list
from services.db import (
    get_latest_repo_manifest_sha,
    prepare_repo_index,
    store_chunks,
    store_dir_summaries,
    store_repo_manifest,
    store_repo_text_lines,
    update_repo_index_job_status,
)
from services.dir_summaries import generate_dir_summaries
from services.embedding_cache import hydrate_embeddings
from services.exact_search import build_text_lines
from services.repo_manifest import build_repo_manifest
from services.walk_repo import collect_file_paths

logger = logging.getLogger(__name__)


async def index_repo_path(
    *,
    repo_url: str,
    repo_dir: str,
    source: str,
    tenant_id: str | None = None,
    repo_connection_id: str | None = None,
    job_id: str | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
    generate_summaries: bool = True,
) -> dict:
    """Index a local checkout and persist tenant/repo/index metadata.

    This is the shared Phase 1 entrypoint used by Temporal activities and the
    local one-shot worker. The HTTP debug endpoints still build custom response
    payloads, but should create the same RepoIndexContext before persisting.
    """
    repo_url = repo_url.rstrip("/")
    if job_id:
        await update_repo_index_job_status(job_id, "manifesting")

    paths = await collect_file_paths(repo_dir)
    chunks = chunk_file_list(paths, embed=False)
    manifest = build_repo_manifest(repo_dir, paths, chunks)
    text_lines = build_text_lines(repo_dir, manifest)

    previous_manifest_sha = await get_latest_repo_manifest_sha(repo_url)
    previous_summary_manifest_sha = await get_latest_repo_manifest_sha(
        repo_url,
        summary_generated=True,
    )

    index_context = await prepare_repo_index(
        repo_url,
        manifest,
        text_line_count=len(text_lines),
        tenant_id=tenant_id,
        repo_connection_id=repo_connection_id,
        job_id=job_id,
        commit_sha=commit_sha,
        branch=branch,
        metadata={"source": source},
        status="indexing",
    )

    text_lines_stored = await store_repo_text_lines(
        repo_url,
        text_lines,
        manifest.files,
        index_context=index_context,
    )

    if job_id:
        await update_repo_index_job_status(
            job_id,
            "embedding",
            repo_index_id=index_context.repo_index_id,
        )
    embedding_stats = await hydrate_embeddings(
        repo_url,
        chunks,
        index_context=index_context,
    )
    stored_chunks = await store_chunks(
        repo_url,
        chunks,
        replace=True,
        index_context=index_context,
    )

    metadata = {
        "source": source,
        "previous_manifest_sha256": previous_manifest_sha,
        "previous_summary_manifest_sha256": previous_summary_manifest_sha,
        "manifest_changed": previous_manifest_sha != manifest.manifest_sha256,
        "embeddings": embedding_stats,
        "stored_chunks": stored_chunks,
        "text_line_count": len(text_lines),
        "text_lines_stored": text_lines_stored,
    }

    summary_count = 0
    if not generate_summaries:
        metadata.update({
            "summary_generated": False,
            "summary_skipped": True,
            "summary_skip_reason": "disabled",
        })
    elif previous_summary_manifest_sha == manifest.manifest_sha256:
        logger.info("Repo manifest unchanged for %s", repo_url)
        metadata.update({
            "summary_generated": False,
            "summary_skipped": True,
        })
    else:
        if job_id:
            await update_repo_index_job_status(
                job_id,
                "summarizing",
                repo_index_id=index_context.repo_index_id,
            )
        logger.info("Generating per-directory summaries for %s", repo_url)
        dir_sums = await generate_dir_summaries(chunks, repo_dir)
        await store_dir_summaries(
            repo_url,
            dir_sums,
            index_context=index_context,
        )
        summary_count = len(dir_sums)
        metadata.update({
            "summary_generated": True,
            "summary_count": summary_count,
        })

    manifest_record = await store_repo_manifest(
        repo_url,
        manifest,
        metadata=metadata,
        index_context=index_context,
    )

    return {
        "tenant_id": index_context.tenant_id,
        "repo_connection_id": index_context.repo_connection_id,
        "repo_index_id": index_context.repo_index_id,
        "repo_url": repo_url,
        "manifest_sha256": manifest.manifest_sha256,
        "file_count": len(manifest.files),
        "chunk_count": len(manifest.chunks),
        "text_line_count": len(text_lines),
        "text_lines_stored": text_lines_stored,
        "stored_chunks": stored_chunks,
        "summary_count": summary_count,
        "embeddings": embedding_stats,
        "manifest": manifest_record,
    }
