"""Phase 5: deterministic embedding and local document ingestion contracts."""

from pathlib import Path

import pytest

from app.rag.embeddings import HashEmbeddingProvider
from app.rag.ingest import _chunks, _read


def test_hash_embeddings_are_normalized_and_deterministic():
    embedder = HashEmbeddingProvider(32)
    first = embedder.embed("Refunds are available within thirty days.")
    assert first == embedder.embed("Refunds are available within thirty days.")
    assert len(first) == 32
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_chunking_has_overlap_and_normalizes_whitespace():
    chunks = _chunks("alpha   beta " * 100, size=40, overlap=10)
    assert len(chunks) > 1
    assert "  " not in chunks[0]
    assert chunks[0][-10:] == chunks[1][:10]


def test_supported_text_and_yaml_files_are_read(tmp_path: Path):
    markdown = tmp_path / "policy.md"
    markdown.write_text("# Return window\nThirty days.", encoding="utf-8")
    assert _read(markdown) == ("# Return window\nThirty days.", "markdown")

    policy = tmp_path / "policy.yaml"
    policy.write_text("version: '1.0.0'\n", encoding="utf-8")
    content, source_type = _read(policy)
    assert source_type == "yaml" and "version: 1.0.0" in content


def test_unsupported_file_type_is_rejected(tmp_path: Path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"not supported")
    with pytest.raises(ValueError, match="unsupported"):
        _read(path)
