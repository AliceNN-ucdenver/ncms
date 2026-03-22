# ADR-004: mongodb-memory-server for Unit Testing

## Status

Accepted

## Date

2026-02-15

## Deciders

IMDB Lite Engineering Lead, QA Lead

## Context

The IMDB Lite API uses MongoDB as its primary data store (ADR-002). Unit and integration tests need to validate data access logic, schema enforcement, Mongoose model behavior, and query correctness. Options considered:

1. **Mock MongoDB calls** — Fast but brittle; tests don't validate actual query behavior, aggregation pipelines, or index usage. Mock drift from real MongoDB semantics is a recurring source of false-positive tests.
2. **Shared test database** — Realistic but introduces test coupling, requires network access, and complicates CI parallelism. State leakage between test suites causes flaky tests.
3. **mongodb-memory-server** — Spins up a real MongoDB instance in-memory per test suite. No external dependencies, no state leakage, real MongoDB query engine. Supports replica sets for transaction testing.

## Decision

Use `mongodb-memory-server` for all unit and integration tests that interact with MongoDB.

### Testing patterns:
- **beforeAll**: Start MongoMemoryServer, connect Mongoose
- **afterAll**: Disconnect Mongoose, stop MongoMemoryServer
- **beforeEach**: Clear all collections (ensures test isolation)
- Each test suite gets its own in-memory MongoDB instance — no shared state

### Example setup:
```typescript
import { MongoMemoryServer } from 'mongodb-memory-server';
import mongoose from 'mongoose';

let mongoServer: MongoMemoryServer;

beforeAll(async () => {
  mongoServer = await MongoMemoryServer.create();
  await mongoose.connect(mongoServer.getUri());
});

afterAll(async () => {
  await mongoose.disconnect();
  await mongoServer.stop();
});

beforeEach(async () => {
  const collections = mongoose.connection.collections;
  for (const key in collections) {
    await collections[key].deleteMany({});
  }
});
```

### What to test with mongodb-memory-server:
- Mongoose model validation (required fields, types, enums)
- CRUD operations (create, read, update, delete)
- Query behavior (filters, projections, population)
- Aggregation pipelines (e.g., movie ratings, cast lookups)
- Index uniqueness constraints
- Middleware (pre/post hooks on save, validate)

### What to mock instead:
- External HTTP calls (OMDB API, image uploads)
- Authentication middleware (JWT verification — test separately)
- Rate limiting and caching layers

## Consequences

**Positive:**
- Tests validate real MongoDB behavior — no mock drift
- No external dependencies for CI — runs anywhere Node.js runs
- Test isolation via separate instances prevents flaky tests
- Supports replica sets if transaction testing is needed

**Negative:**
- ~2-3 second startup per test suite (acceptable for CI)
- Requires `mongodb-memory-server` as devDependency (~100MB binary download on first run, cached thereafter)
- Memory usage higher than pure mocks (~100MB per instance)

## References

- ADR-002: MongoDB Document Store for Movie Data
- mongodb-memory-server: https://github.com/nodkz/mongodb-memory-server
- IMDB Lite architecture: architecture/bar.arch.json

## Characteristics

reversibility: 5
cost: 5
risk: 2
complexity: 2
effort: 2

## Links

- depends-on: ADR-002
- related: ADR-003
