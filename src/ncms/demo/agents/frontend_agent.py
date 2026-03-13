"""Frontend Agent - knows about UI components and client-side patterns."""

from __future__ import annotations

from ncms.demo.agents.base_demo import DemoAgent


class FrontendAgent(DemoAgent):
    """Agent responsible for building UI components."""

    primary_domain = "frontend"
    knowledge_type = "code-snippet"
    trust_level = "observed"
    max_confidence = 0.90
    snapshot_confidence = 0.85
    snapshot_volatility = "changing"

    def declare_expertise(self) -> list[str]:
        return ["frontend", "ui:components", "ui:pages"]

    def declare_subscriptions(self) -> list[str]:
        return ["api", "api:user-service", "api:auth-service"]
