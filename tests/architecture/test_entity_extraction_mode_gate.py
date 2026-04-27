from __future__ import annotations

from pathlib import Path

APP_GLINER_SITES = [
    Path("src/ncms/application/ingestion/pipeline.py"),
    Path("src/ncms/application/index_worker.py"),
    Path("src/ncms/application/retrieval/pipeline.py"),
    Path("src/ncms/application/memory_service.py"),
    Path("src/ncms/application/temporal_arithmetic.py"),
    Path("src/ncms/application/document_service.py"),
    Path("src/ncms/application/reindex_service.py"),
]


def test_application_gliner_call_sites_are_mode_gated() -> None:
    """Production GLiNER use must stay behind the extraction lane switch."""

    for path in APP_GLINER_SITES:
        text = path.read_text()
        if "gliner_extractor" in text:
            assert "use_gliner_entities" in text, f"{path} must gate GLiNER behind mode policy"
