# SPDX-License-Identifier: Apache-2.0
"""Tests for NCMSHttpClient."""

from __future__ import annotations

from nat.plugins.ncms.http_client import NCMSHttpClient


class TestHttpClientInit:
    def test_strips_trailing_slash(self):
        client = NCMSHttpClient(hub_url="http://localhost:9080/")
        assert client._base == "http://localhost:9080"

    def test_default_timeouts(self):
        client = NCMSHttpClient(hub_url="http://localhost:9080")
        assert client._client.timeout.connect == 10.0
        assert client._client.timeout.read == 60.0

    def test_custom_timeouts(self):
        client = NCMSHttpClient(
            hub_url="http://localhost:9080",
            connect_timeout=5.0,
            request_timeout=30.0,
        )
        assert client._client.timeout.connect == 5.0
        assert client._client.timeout.read == 30.0
