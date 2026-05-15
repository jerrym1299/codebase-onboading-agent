import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services import chunk_and_embed
from services.chunk_and_embed import CodeChunk, MAX_TOKENS, embed_chunks, split_oversized
from services.walk_repo import collect_file_paths


class _FakeEmbeddings:
    def create(self, *, input, model):
        for idx, text in enumerate(input):
            token_count = len(chunk_and_embed.encoder.encode(text))
            assert token_count <= chunk_and_embed.EMBEDDING_HARD_MAX_TOKENS, (
                idx,
                token_count,
                chunk_and_embed.EMBEDDING_HARD_MAX_TOKENS,
            )
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[float(i), 0.0, 0.0])
                for i, _ in enumerate(input)
            ]
        )


class _FakeClient:
    embeddings = _FakeEmbeddings()


def test_single_line_chunk_is_split_under_budget():
    long_line = "const payload = '" + ("abcdef " * 12000) + "';"
    chunk = CodeChunk(
        content=long_line,
        chunk_type="declaration",
        file_path="/repo/src/generated-but-kept.js",
        name="payload",
        start_line=10,
        end_line=10,
    )

    pieces = split_oversized(chunk)

    assert len(pieces) > 1
    assert all(piece.token_count <= MAX_TOKENS for piece in pieces)
    assert all(piece.metadata["split_strategy"] == "token" for piece in pieces)


def test_embed_chunks_preflights_inputs():
    old_client = chunk_and_embed._openai_client
    chunk_and_embed._openai_client = _FakeClient()
    try:
        chunks = [
            CodeChunk(
                content="def tiny():\n    return 1\n",
                chunk_type="function",
                file_path="/repo/tiny.py",
                name="tiny",
                start_line=0,
                end_line=1,
            )
        ]
        assert embed_chunks(chunks) == 1
        assert chunks[0].embedding == [0.0, 0.0, 0.0]
    finally:
        chunk_and_embed._openai_client = old_client


async def test_generated_and_lock_files_are_skipped():
    with tempfile.TemporaryDirectory() as repo:
        src = os.path.join(repo, "src")
        os.mkdir(src)
        with open(os.path.join(src, "app.ts"), "w") as f:
            f.write("export const app = true;\n")
        with open(os.path.join(src, "bundle.min.js"), "w") as f:
            f.write("x" * 1000)
        with open(os.path.join(repo, "package-lock.json"), "w") as f:
            f.write("{}")

        paths = await collect_file_paths(repo)
        rel_paths = sorted(os.path.relpath(path, repo) for path in paths)

    assert rel_paths == ["src/app.ts"]


def main():
    test_single_line_chunk_is_split_under_budget()
    test_embed_chunks_preflights_inputs()
    asyncio.run(test_generated_and_lock_files_are_skipped())
    print("embedding boundary tests passed")


if __name__ == "__main__":
    main()
