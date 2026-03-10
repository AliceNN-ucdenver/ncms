"""Entity extraction via regex heuristics — zero external dependencies.

Extracts candidate entity names from text for graph population and
spreading activation context. Used at two points in the pipeline:
1. Store-time: auto-extract entities from memory content
2. Query-time: extract entities from search query for spreading activation

This is a heuristic approach aligned with NCMS's "embedded first" philosophy.
No NLP models, no embeddings, no external services.
"""

from __future__ import annotations

import re

# ── Technology name catalog ──────────────────────────────────────────────
# Matched case-insensitively with word boundaries.
TECHNOLOGY_NAMES: frozenset[str] = frozenset({
    # Databases
    "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Cassandra",
    "DynamoDB", "CockroachDB", "MariaDB", "Neo4j", "Elasticsearch",
    # Web frameworks / runtimes
    "Express", "FastAPI", "Django", "Flask", "Rails", "Spring",
    "NestJS", "Koa", "Hono", "Fastify",
    # Frontend
    "React", "Vue", "Angular", "Svelte", "NextJS", "Nuxt", "Remix",
    "Astro", "Gatsby", "Vite", "Webpack",
    # Languages / runtimes
    "TypeScript", "JavaScript", "Python", "Rust", "Golang",
    "Node", "Deno", "Bun",
    # Infrastructure
    "NGINX", "Apache", "Caddy", "Docker", "Kubernetes",
    "Terraform", "Ansible", "Vercel", "Netlify",
    # Auth / security
    "JWT", "OAuth", "SAML", "LDAP", "Keycloak",
    # Messaging / queues
    "Kafka", "RabbitMQ", "NATS", "Celery",
    # Caching / pooling
    "PgBouncer", "Memcached", "Varnish",
    # Cloud
    "AWS", "GCP", "Azure", "CloudFront", "Lambda",
    # Libraries
    "Pydantic", "SQLAlchemy", "Prisma", "Drizzle", "Zustand",
    "TailwindCSS", "Tantivy", "NetworkX", "Pandas", "NumPy",
    # Data formats / protocols
    "GraphQL", "gRPC", "REST", "WebSocket", "SSE",
})

# Build case-insensitive pattern from technology names
_sorted_techs = sorted(TECHNOLOGY_NAMES, key=len, reverse=True)
_TECH_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _sorted_techs) + r")\b",
    re.IGNORECASE,
)

# ── SQL / programming keyword blocklist (false positives for UPPER_SNAKE) ─
_KEYWORD_BLOCKLIST: frozenset[str] = frozenset({
    "NOT", "AND", "OR", "NULL", "TRUE", "FALSE", "NONE",
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
    "DEFAULT", "PRIMARY", "KEY", "SET", "VARCHAR", "INT", "SERIAL",
    "TIMESTAMP", "NOW", "UNIQUE", "REFERENCES", "CREATE", "INSERT",
    "SELECT", "UPDATE", "INTO", "FROM", "WHERE", "ORDER", "BY",
    "LIMIT", "ASC", "DESC", "INDEX", "TABLE", "ALTER", "DROP",
    "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "GROUP", "HAVING",
    "COUNT", "SUM", "AVG", "MAX", "MIN", "LIKE", "BETWEEN",
    "EXISTS", "CASE", "WHEN", "THEN", "ELSE", "END", "BEGIN",
    "COMMIT", "ROLLBACK", "CASCADE", "RESTRICT", "FOREIGN",
    "TEXT", "BLOB", "REAL", "FLOAT", "DOUBLE", "BOOLEAN",
    "RETURNS", "RETURN", "IMPORT", "EXPORT", "CLASS", "FUNCTION",
    "CONST", "VAR", "LET", "NEW", "THIS", "SELF", "ASYNC", "AWAIT",
    "HTTP", "HTTPS", "URL", "URI", "API", "JSON", "XML", "HTML", "CSS",
    "TODO", "FIXME", "NOTE", "HACK", "WARNING", "ERROR", "INFO", "DEBUG",
})

# ── Regex patterns ───────────────────────────────────────────────────────

# API paths: /api/v2/users, /auth/login, /users/{id}
_API_PATH_RE = re.compile(r"(?:^|\s)(/[a-z][a-z0-9/_\-{}.*]*[a-z0-9}*])", re.MULTILINE)

# PascalCase identifiers: UserService, AuthTokenManager (2+ words)
_PASCAL_CASE_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]+)+)\b")

# Table references: "users table", "TABLE users", "table `events`"
_TABLE_REF_RE = re.compile(
    r"\b(?:table|TABLE)\s+[`\"']?(\w{2,})[`\"']?"
    r"|(\w{3,})\s+table\b",
    re.IGNORECASE,
)

# Qualified dotted names: react.query, shadcn.ui (2+ segments)
_DOTTED_NAME_RE = re.compile(r"\b([a-z]\w*(?:\.[a-z]\w*)+)\b")

# Max entities to return per extraction
_MAX_ENTITIES = 20


def extract_entity_names(text: str) -> list[dict[str, str]]:
    """Extract candidate entity names from text using regex heuristics.

    Returns a list of dicts with ``name`` and ``type`` keys, deduplicated
    by case-insensitive name, capped at 20 entities.

    Examples::

        >>> extract_entity_names("UserService calls GET /api/v2/users with JWT auth")
        [
            {"name": "UserService", "type": "component"},
            {"name": "/api/v2/users", "type": "endpoint"},
            {"name": "JWT", "type": "technology"},
        ]
    """
    if not text or len(text) < 2:
        return []

    seen: set[str] = set()  # lowercase names for dedup
    entities: list[dict[str, str]] = []

    def _add(name: str, entity_type: str) -> None:
        key = name.lower()
        if key not in seen and len(name) >= 2:
            seen.add(key)
            entities.append({"name": name, "type": entity_type})

    # 1. Technology names (high confidence — curated list)
    for match in _TECH_PATTERN.finditer(text):
        # Normalize to canonical casing from the catalog
        matched = match.group(1)
        canonical = _find_canonical_tech(matched)
        _add(canonical or matched, "technology")

    # 2. API paths
    for match in _API_PATH_RE.finditer(text):
        path = match.group(1)
        # Strip trailing punctuation that might have been captured
        path = path.rstrip(".,;:!?)")
        if len(path) >= 3:  # At least /xy
            _add(path, "endpoint")

    # 3. PascalCase identifiers
    for match in _PASCAL_CASE_RE.finditer(text):
        name = match.group(1)
        # Skip if it's already captured as a technology
        if name.lower() not in seen:
            _add(name, "component")

    # 4. Table references
    for match in _TABLE_REF_RE.finditer(text):
        name = match.group(1) or match.group(2)
        if name and name.upper() not in _KEYWORD_BLOCKLIST:
            _add(name, "table")

    # 5. Dotted names
    for match in _DOTTED_NAME_RE.finditer(text):
        name = match.group(1)
        if name.lower() not in seen:
            _add(name, "module")

    return entities[:_MAX_ENTITIES]


def _find_canonical_tech(matched: str) -> str | None:
    """Find the canonical casing for a matched technology name."""
    lower = matched.lower()
    for tech in TECHNOLOGY_NAMES:
        if tech.lower() == lower:
            return tech
    return None
