"""Integration tests for the Knowledge Loader - Matrix-style knowledge download."""

import json
import os
import tempfile

import pytest

from ncms.application.knowledge_loader import KnowledgeLoader


class TestKnowledgeLoaderText:
    @pytest.mark.asyncio
    async def test_load_text_creates_memories(self, memory_service):
        """Loading text should create searchable memories."""
        loader = KnowledgeLoader(memory_service)

        text = (
            "The API gateway uses Express.js behind NGINX.\n\n"
            "All endpoints are versioned under /api/v2/.\n\n"
            "Authentication uses JWT tokens with 1-hour expiry."
        )
        stats = await loader.load_text(
            text,
            domains=["architecture"],
            source_agent="loader",
        )

        assert stats.memories_created > 0
        assert len(stats.errors) == 0

        # Should be searchable
        results = await memory_service.search("API gateway Express")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_load_text_respects_domains(self, memory_service):
        """Loaded knowledge should be filterable by domain."""
        loader = KnowledgeLoader(memory_service)

        await loader.load_text(
            "PostgreSQL uses WAL mode for concurrent reads.",
            domains=["database"],
        )
        await loader.load_text(
            "React Query handles server state caching.",
            domains=["frontend"],
        )

        db_results = await memory_service.search("WAL mode", domain="database")
        assert len(db_results) >= 1

        fe_results = await memory_service.search("React Query", domain="frontend")
        assert len(fe_results) >= 1

    @pytest.mark.asyncio
    async def test_load_text_stats(self, memory_service):
        """Stats should accurately reflect the loading operation."""
        loader = KnowledgeLoader(memory_service)

        stats = await loader.load_text(
            "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.",
            domains=["test"],
        )

        assert stats.chunks_total >= 1
        assert stats.memories_created >= 1
        assert stats.memories_created <= stats.chunks_total

    @pytest.mark.asyncio
    async def test_empty_text_creates_no_memories(self, memory_service):
        """Empty or whitespace-only text should create no memories."""
        loader = KnowledgeLoader(memory_service)

        initial_count = await memory_service.memory_count()
        stats = await loader.load_text("   \n\n   ", domains=["test"])

        assert stats.memories_created == 0
        assert await memory_service.memory_count() == initial_count


class TestKnowledgeLoaderFile:
    @pytest.mark.asyncio
    async def test_load_markdown_file(self, memory_service):
        """Loading a markdown file should chunk by headings."""
        loader = KnowledgeLoader(memory_service)

        content = (
            "# Architecture\n\n"
            "The system uses microservices.\n\n"
            "## Database\n\n"
            "PostgreSQL with read replicas.\n\n"
            "## API\n\n"
            "REST endpoints versioned under /api/v2/.\n"
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            f.flush()
            tmp_path = f.name

        try:
            stats = await loader.load_file(tmp_path, domains=["arch"])
            assert stats.files_processed == 1
            assert stats.memories_created >= 1
            assert len(stats.errors) == 0

            # Sections should be searchable
            results = await memory_service.search("PostgreSQL replicas")
            assert len(results) >= 1
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_load_json_file(self, memory_service):
        """Loading a JSON file should create memories from entries."""
        loader = KnowledgeLoader(memory_service)

        data = [
            {"endpoint": "/users", "method": "GET", "description": "List all users"},
            {"endpoint": "/auth", "method": "POST", "description": "Login with credentials"},
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            tmp_path = f.name

        try:
            stats = await loader.load_file(tmp_path, domains=["api"])
            assert stats.files_processed == 1
            assert stats.memories_created >= 1
            assert len(stats.errors) == 0
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_load_csv_file(self, memory_service):
        """Loading a CSV file should preserve header context."""
        loader = KnowledgeLoader(memory_service)

        csv_content = (
            "endpoint,method,description\n"
            "/users,GET,List all users\n"
            "/users/{id},GET,Get user by ID\n"
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            tmp_path = f.name

        try:
            stats = await loader.load_file(tmp_path, domains=["api"])
            assert stats.files_processed == 1
            assert stats.memories_created >= 1
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_load_nonexistent_file(self, memory_service):
        """Loading a nonexistent file should return an error, not crash."""
        loader = KnowledgeLoader(memory_service)
        stats = await loader.load_file("/nonexistent/path/file.md", domains=["test"])
        assert stats.files_processed == 0
        assert stats.memories_created == 0
        assert len(stats.errors) >= 1

    @pytest.mark.asyncio
    async def test_load_html_file(self, memory_service):
        """Loading HTML should strip tags and store text content."""
        loader = KnowledgeLoader(memory_service)

        html_content = (
            "<html><body>"
            "<h1>API Documentation</h1>"
            "<p>The users endpoint returns a paginated list.</p>"
            "<script>console.log('ignored');</script>"
            "</body></html>"
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html_content)
            f.flush()
            tmp_path = f.name

        try:
            stats = await loader.load_file(tmp_path, domains=["docs"])
            assert stats.files_processed == 1
            assert stats.memories_created >= 1

            results = await memory_service.search("API Documentation users")
            assert len(results) >= 1
            # Script content should not appear
            for r in results:
                assert "console.log" not in r.memory.content
        finally:
            os.unlink(tmp_path)


class TestKnowledgeLoaderDirectory:
    @pytest.mark.asyncio
    async def test_load_directory(self, memory_service):
        """Loading a directory should process all supported files."""
        loader = KnowledgeLoader(memory_service)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a couple of files
            md_path = os.path.join(tmpdir, "arch.md")
            with open(md_path, "w") as f:
                f.write("# Architecture\n\nMicroservices with REST APIs.\n")

            txt_path = os.path.join(tmpdir, "notes.txt")
            with open(txt_path, "w") as f:
                f.write("The database uses PostgreSQL 15.\n")

            # Unsupported file should be skipped
            bin_path = os.path.join(tmpdir, "data.bin")
            with open(bin_path, "wb") as f:
                f.write(b"\x00\x01\x02")

            stats = await loader.load_directory(tmpdir, domains=["project"])
            assert stats.files_processed == 2  # .md and .txt, not .bin
            assert stats.memories_created >= 2
            assert len(stats.errors) == 0

    @pytest.mark.asyncio
    async def test_load_nonexistent_directory(self, memory_service):
        """Loading a nonexistent directory should return an error."""
        loader = KnowledgeLoader(memory_service)
        stats = await loader.load_directory("/nonexistent/dir/", domains=["test"])
        assert stats.files_processed == 0
        assert len(stats.errors) >= 1


class TestKnowledgeLoaderChunking:
    @pytest.mark.asyncio
    async def test_large_text_is_chunked(self, memory_service):
        """Text exceeding chunk_max_chars should be split into multiple memories."""
        loader = KnowledgeLoader(memory_service, chunk_max_chars=100)

        # Create text that's well over 100 chars
        paragraphs = [f"Paragraph {i}: " + "x" * 60 for i in range(5)]
        text = "\n\n".join(paragraphs)

        stats = await loader.load_text(text, domains=["test"])
        # Should create more than 1 memory due to chunking
        assert stats.memories_created > 1

    @pytest.mark.asyncio
    async def test_seed_and_search_workflow(self, memory_service):
        """Full workflow: seed knowledge, then search for it (Matrix download)."""
        loader = KnowledgeLoader(memory_service)

        # Seed architecture knowledge
        architecture_doc = (
            "The API gateway runs on Express.js behind an NGINX reverse proxy.\n\n"
            "PostgreSQL 15 with read replicas and connection pooling via PgBouncer.\n\n"
            "React 18 with TypeScript using React Query for server state management."
        )
        stats = await loader.load_text(
            architecture_doc,
            domains=["architecture", "platform"],
            source_agent="knowledge-loader",
            project="acme-platform",
        )
        assert stats.memories_created > 0

        # Now agents can search the seeded knowledge
        results = await memory_service.search("database connection pooling", domain="architecture")
        assert len(results) >= 1
        assert any("pgbouncer" in r.memory.content.lower() for r in results)
