from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

import rag


async def _collect_index_events(*args, **kwargs) -> list[dict]:
    return [event async for event in rag.build_index(*args, **kwargs)]


def test_build_index_stops_discovery_after_the_file_limit(tmp_path: Path, monkeypatch):
    paths = []
    for index in range(5):
        path = tmp_path / f"document-{index}.md"
        path.write_text(f"작업 {index} 구현", encoding="utf-8")
        paths.append(path)

    seen: list[str] = []

    def candidates(_root: Path):
        for path in paths:
            seen.append(path.name)
            yield path, path.name

    async def fake_embed(_host: str, _model: str, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(rag, "iter_indexable", candidates)
    monkeypatch.setattr(rag, "embed_texts", fake_embed)

    events = asyncio.run(_collect_index_events(tmp_path, "http://ollama", "embed", max_files=2))
    done = events[-1]

    assert seen == ["document-0.md", "document-1.md", "document-2.md"]
    assert done["type"] == "done"
    assert done["files"] == 2
    assert done["truncated"] is True
    assert done["file_limit_reached"] is True
    assert done["total_found"] == 3
    assert done["total_found_exact"] is False


def test_rag_store_cache_is_bounded_and_uses_recent_entries(tmp_path: Path):
    rag._CACHE.clear()
    roots = [tmp_path / f"workspace-{index}" for index in range(rag.MAX_CACHED_STORES + 1)]
    for index, root in enumerate(roots):
        rag._save_store(
            root,
            {"embed_model": "embed", "dim": 2, "count": 1, "files": {}},
            [{"file": "note.md", "start": 1, "end": 1, "text": f"note {index}"}],
            np.asarray([[1.0, 0.0]], dtype=np.float32),
        )

    rag._load_store(roots[0])
    rag._load_store(roots[1])
    rag._load_store(roots[0])  # refresh the first store in LRU order
    rag._load_store(roots[2])

    assert list(rag._CACHE) == [str(roots[0]), str(roots[2])]
