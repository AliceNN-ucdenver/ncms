# ADR-002: MongoDB Document Store for Movie Data

## Status

accepted

## Date

2026-02-23

## Deciders

IMDB Lite Engineering Lead, Data Architect

## Context

The IMDB Lite application needs a data store for movies, actors, characters, and user reviews. Movies contain nested cast arrays (actors playing characters), varying metadata fields, and poster image URLs. A relational model would require many join tables (movies, actors, characters, movie_cast, reviews) with complex queries for common operations like "get movie with full cast."

## Decision

Use MongoDB with Mongoose ODM. Movies are stored as self-contained documents with embedded actor/character arrays. Reviews are stored as separate documents referencing movies via ObjectId, enabling independent pagination and moderation. User accounts are a separate collection with bcrypt-hashed credentials.

## Consequences

Positive: Flexible schema accommodates varying movie metadata without migrations. Embedded cast arrays make single-document reads fast for the primary use case (movie detail page). Mongoose provides schema validation at the application layer. Natural fit for a read-heavy, public data workload.

Negative: No multi-document ACID transactions (acceptable for this use case — movie data is not financially sensitive). Must handle eventual consistency for aggregate rating calculations. Denormalized actor data across movie documents requires update fan-out when actor profiles change.

## Alternatives

1. PostgreSQL with JSONB — considered but adds ORM complexity for a document-oriented data model
2. DynamoDB — rejected due to vendor lock-in and complex query patterns for search
3. SQLite — too limited for concurrent access in a multi-user web application

## References

- IMDB Lite architecture: architecture/bar.arch.json
- ADR-001: Initial Architecture for IMDB Lite Application

## Characteristics

reversibility: 2
cost: 4
risk: 3
complexity: 3
effort: 3

## Links

- depends-on: ADR-001
