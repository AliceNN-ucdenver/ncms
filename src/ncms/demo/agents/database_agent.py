"""Database Agent - knows about schemas, migrations, and data models."""

from __future__ import annotations

from ncms.demo.agents.base_demo import DemoAgent


class DatabaseAgent(DemoAgent):
    """Agent responsible for database schemas and migrations."""

    primary_domain = "db"
    knowledge_type = "interface-spec"
    trust_level = "authoritative"
    max_confidence = 0.95
    snapshot_confidence = 0.95
    snapshot_volatility = "stable"

    def declare_expertise(self) -> list[str]:
        return ["db", "db:user-schema", "db:auth-schema", "db:migrations"]

    def declare_subscriptions(self) -> list[str]:
        return ["api", "config"]
