"""Integration tests for the `ncms topics` CLI command group."""

from __future__ import annotations

from click.testing import CliRunner

from ncms.interfaces.cli.main import cli


class TestTopicsCLI:
    """Tests for the ncms topics set/list/clear commands."""

    def test_set_and_list_roundtrip(self, tmp_path):
        """Set labels and then list them back."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        # Set labels
        result = runner.invoke(
            cli, ["topics", "set", "api", "endpoint", "service", "protocol", "--db", db]
        )
        assert result.exit_code == 0
        assert "3 labels" in result.output
        assert "endpoint" in result.output

        # List specific domain
        result = runner.invoke(cli, ["topics", "list", "api", "--db", db])
        assert result.exit_code == 0
        assert "endpoint" in result.output
        assert "service" in result.output
        assert "protocol" in result.output

    def test_list_all_domains(self, tmp_path):
        """List all cached domains."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        # Set labels for two domains
        runner.invoke(cli, ["topics", "set", "api", "endpoint", "service", "--db", db])
        runner.invoke(cli, ["topics", "set", "db", "table", "column", "--db", db])

        # List all
        result = runner.invoke(cli, ["topics", "list", "--db", db])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "db" in result.output

    def test_list_empty(self, tmp_path):
        """Listing with no cached labels shows universal fallback."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        result = runner.invoke(cli, ["topics", "list", "--db", db])
        assert result.exit_code == 0
        assert "universal" in result.output.lower() or "Universal" in result.output

    def test_list_uncached_domain(self, tmp_path):
        """Listing a domain without cached labels shows fallback message."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        result = runner.invoke(cli, ["topics", "list", "finance", "--db", db])
        assert result.exit_code == 0
        assert "No cached labels" in result.output or "universal" in result.output.lower()

    def test_clear_removes_labels(self, tmp_path):
        """Clear should remove cached labels for a domain."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        # Set then clear
        runner.invoke(cli, ["topics", "set", "api", "endpoint", "service", "--db", db])
        result = runner.invoke(cli, ["topics", "clear", "api", "--db", db])
        assert result.exit_code == 0
        assert "Cleared" in result.output

        # Verify cleared
        result = runner.invoke(cli, ["topics", "list", "api", "--db", db])
        assert "No cached labels" in result.output or "universal" in result.output.lower()

    def test_set_overwrites_existing(self, tmp_path):
        """Setting labels for an existing domain should overwrite."""
        db = str(tmp_path / "test.db")
        runner = CliRunner()

        runner.invoke(cli, ["topics", "set", "api", "endpoint", "service", "--db", db])
        runner.invoke(cli, ["topics", "set", "api", "route", "handler", "--db", db])

        result = runner.invoke(cli, ["topics", "list", "api", "--db", db])
        assert result.exit_code == 0
        assert "route" in result.output
        assert "handler" in result.output
        # Old labels should be gone
        assert "endpoint" not in result.output
