"""API Agent - knows about REST endpoints and API contracts."""

from __future__ import annotations

from ncms.demo.agents.base_demo import DemoAgent


class ApiAgent(DemoAgent):
    """Agent responsible for building and maintaining API endpoints."""

    primary_domain = "api"
    knowledge_type = "interface-spec"
    trust_level = "authoritative"
    max_confidence = 0.95
    snapshot_confidence = 0.9
    snapshot_volatility = "changing"
    include_structured_in_snapshot = True
    include_references_in_response = True

    def declare_expertise(self) -> list[str]:
        return ["api", "api:user-service", "api:auth-service"]

    def declare_subscriptions(self) -> list[str]:
        return ["db", "db:user-schema", "config"]
