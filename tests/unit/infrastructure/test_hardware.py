"""Unit tests for the hardware-device resolver.

Pins the priority order (CUDA > MPS > CPU), env-var override
precedence (per-component > NCMS_DEVICE > auto), and the
invalid-value-falls-through behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ncms.infrastructure.hardware import resolve_device, summary


class TestResolveDevice:
    def test_no_env_defaults_to_auto(self, monkeypatch) -> None:
        monkeypatch.delenv("NCMS_DEVICE", raising=False)
        monkeypatch.delenv("NCMS_TEST_COMPONENT_DEVICE", raising=False)
        with patch("ncms.infrastructure.hardware._auto", return_value="cpu"):
            assert resolve_device("NCMS_TEST_COMPONENT_DEVICE") == "cpu"

    def test_component_env_beats_global(self, monkeypatch) -> None:
        monkeypatch.setenv("NCMS_DEVICE", "cpu")
        monkeypatch.setenv("NCMS_TEST_COMPONENT_DEVICE", "mps")
        assert resolve_device("NCMS_TEST_COMPONENT_DEVICE") == "mps"

    def test_global_used_when_component_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("NCMS_DEVICE", "cpu")
        monkeypatch.delenv("NCMS_TEST_COMPONENT_DEVICE", raising=False)
        assert resolve_device("NCMS_TEST_COMPONENT_DEVICE") == "cpu"

    def test_auto_env_falls_through(self, monkeypatch) -> None:
        monkeypatch.setenv("NCMS_DEVICE", "auto")
        with patch("ncms.infrastructure.hardware._auto", return_value="cuda"):
            assert resolve_device() == "cuda"

    @pytest.mark.parametrize("value", ["gpu", "amd", "tpu", ""])
    def test_invalid_env_values_fall_through(
        self, monkeypatch, value: str,
    ) -> None:
        monkeypatch.setenv("NCMS_DEVICE", value)
        with patch("ncms.infrastructure.hardware._auto", return_value="cpu"):
            assert resolve_device() == "cpu"

    def test_case_insensitive(self, monkeypatch) -> None:
        monkeypatch.setenv("NCMS_DEVICE", "MPS")
        assert resolve_device() == "mps"

    def test_whitespace_stripped(self, monkeypatch) -> None:
        monkeypatch.setenv("NCMS_DEVICE", "  cuda  ")
        assert resolve_device() == "cuda"


class TestAuto:
    def test_cuda_preferred_over_mps(self) -> None:
        with patch("ncms.infrastructure.hardware._auto", return_value="cuda"):
            assert resolve_device() == "cuda"

    def test_mps_preferred_over_cpu(self) -> None:
        with patch("ncms.infrastructure.hardware._auto", return_value="mps"):
            assert resolve_device() == "mps"


class TestSummary:
    def test_summary_keys(self) -> None:
        info = summary()
        assert "selected" in info
        assert "cuda_available" in info
        assert "mps_available" in info
        # selected must be a valid device string
        assert info["selected"] in {"cuda", "mps", "cpu"}
