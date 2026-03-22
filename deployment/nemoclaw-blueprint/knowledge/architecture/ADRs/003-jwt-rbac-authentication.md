# ADR-003: JWT with Inline RBAC for Authentication and Authorization

## Status

accepted

## Date

2026-02-23

## Deciders

IMDB Lite Engineering Lead, Security Architect

## Context

The application needs authentication (who is the user?) and authorization (what can they do?). Three user tiers exist: anonymous viewers who can browse movies, authenticated reviewers who can post reviews, and administrators who manage the movie catalog. Options considered: external identity provider (Auth0/Cognito), session-based authentication with cookies, or JWT with role-based access control.

## Decision

Implement JWT bearer tokens issued by the Movie API. Three roles are defined: `viewer` (default anonymous read access), `reviewer` (authenticated users who can post reviews and ratings), and `admin` (content administrators who can create/edit/delete movies, actors, and characters). Roles are stored as claims in the JWT payload and validated by Express middleware on each request. Passwords are hashed with bcrypt (cost factor 12).

## Consequences

Positive: Stateless authentication eliminates the need for a session store. JWT claims carry role information, enabling middleware-based RBAC without database lookups on every request. Simple implementation appropriate for a lightweight application. Clear separation of concerns between authentication (JWT issuance) and authorization (middleware role checks).

Negative: Token revocation requires short expiration windows plus refresh token rotation. JWT payload size increases slightly with role claims. Would need to migrate to an external identity provider (Auth0, Cognito) if the application scales to production with social login or SSO requirements.

## Alternatives

1. Auth0/Cognito — rejected as over-engineered for a lite application; adds external dependency and cost
2. Session-based with Redis — considered but adds infrastructure (Redis) for session storage
3. OAuth2 + OIDC — full specification is heavyweight for three simple roles

## References

- IMDB Lite architecture: architecture/bar.arch.json
- ADR-001: Initial Architecture for IMDB Lite Application
- ADR-002: MongoDB Document Store for Movie Data

## Characteristics

reversibility: 2
cost: 4
risk: 3
complexity: 3
effort: 3

## Links

- depends-on: ADR-001
- related: ADR-002
