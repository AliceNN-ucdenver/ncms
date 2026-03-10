"""Domain exception hierarchy for NCMS."""


class NCMSError(Exception):
    """Base exception for all NCMS errors."""


class MemoryNotFoundError(NCMSError):
    """Raised when a memory ID does not exist."""

    def __init__(self, memory_id: str):
        self.memory_id = memory_id
        super().__init__(f"Memory not found: {memory_id}")


class AgentNotRegisteredError(NCMSError):
    """Raised when an agent is not registered on the Knowledge Bus."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(f"Agent not registered: {agent_id}")


class DomainNotFoundError(NCMSError):
    """Raised when no providers exist for a knowledge domain."""

    def __init__(self, domain: str):
        self.domain = domain
        super().__init__(f"No providers for domain: {domain}")


class SnapshotExpiredError(NCMSError):
    """Raised when a snapshot has exceeded its TTL."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(f"Snapshot expired for agent: {agent_id}")


class BusTimeoutError(NCMSError):
    """Raised when a Knowledge Bus ask times out."""

    def __init__(self, ask_id: str, timeout_ms: int):
        self.ask_id = ask_id
        self.timeout_ms = timeout_ms
        super().__init__(f"Ask {ask_id} timed out after {timeout_ms}ms")


class StorageError(NCMSError):
    """Raised for storage backend failures."""
