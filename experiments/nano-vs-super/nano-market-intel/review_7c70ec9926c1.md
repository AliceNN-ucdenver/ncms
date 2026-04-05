<!-- project_id: PRJ-e5e6bd5f -->
# Design Review Report — Authentication patterns for identity services

**Status:** APPROVED at 84% after 1 round(s)
**Design Document:** 26144b729334
**Review Rounds:** 1
**Quality Threshold:** 80%

---

## Architecture Review (Score: 82%)

SCORE: 82  
SEVERITY: Medium  
COVERED: The design correctly implements JWT‑bearer authentication with inline RBAC (ADR‑003), uses bcrypt hashing (ADR‑003), stores refresh tokens in a user collection (ADR‑002), and validates input via Zod schemas (aligns with ADR‑001’s schema‑validation focus). It registers the required API endpoints (/login, /refresh, /logout, /mfa/verify, /me) and provides middleware for rate limiting, token rotation, and revocation (fulfills security controls described in ADR‑003 and architectural governance). It also respects CALM service boundaries (ADR‑001) by defining “movie-api” and “react-frontend” nodes, and uses only permitted technologies (Node.js/Express/MongoDB) approved in the ADRs. The design satisfies performance expectations by keeping JWT issuance stateless and limiting database lookups to token validation.  

MISSING: 1) Only one primary authentication endpoint is defined (POST /api/auth/login); the PRD expects at least three distinct identity‑provider patterns (e.g., refresh‑token exchange, MFA verification, federated SSO) beyond the login flow. 2) No explicit support for additional identity patterns such as federated identity provider integration, session‑based fallback, or legacy protocol support (SNMP/LDAP) is present. 3) The design does not address scalability constraints for token revocation beyond short‑lived tokens and rotation; no explicit TTL or async revocation queue is defined. 4) No explicit compliance evidence for auditability (e.g., immutable audit logs of token issuance) or regulatory controls for multi‑tenant isolation. 5) Configuration for MFA (OTP via SMS/email) is mentioned but not modeled in CALM node relationships nor enforced by middleware. 6) The design assumes TypeORM but the earlier CALM model references MongoDB‑memory‑server; there is a mismatch in persistence layer assumption that could cause deployment drift.  

CHANGES:  
1. Add two additional authentication flow endpoints (e.g., `/api/auth/federated/callback` and `/api/auth/mfa/otp/request`) to match the PRD’s requirement for at least three distinct identity patterns.  
2. Extend the CALM model JSON to include `federated-idp` and `mfa-service` nodes and link them to the `movie-api` node to document new component relationships and containment hierarchies.  
3. Update the auth middleware to enforce MFA challenge issuance and verification via a dedicated service, and add a stateful revocation store for refresh token blacklisting that persists beyond 60 seconds.  
4. Introduce explicit configuration for secret management (e.g., secret rotation, audit logging) and ensure all token issuance events are written to an immutable audit log (e.g., Append‑Only Log service) for compliance traceability.  
5. Align the persistence layer definitions with the CALM model: either migrate all service definitions to the MongoDB‑based node set or explicitly document the coexistence of TypeORM (SQL) and MongoDB nodes as separate CALM components.  
6. Document Jasmyne authentication across the three user tiers (viewer, reviewer, admin) by adding role‑based endpoint protections and ensuring admin‑only operations trigger additional authorization checks in middleware.  
7. Add automated guardrails in CI to enforce that any new authentication endpoint must be documented in an ADR with appropriate risk assessment, severity labeling, and fitness‑function coverage (connection pooling, rate limiting, async revocation) before merge.  
8. Incorporate compliance checklists for data residency and retention periods for JWT claims and refresh token hashes, ensuring they meet organizational policy and regulatory standards.  
9. Add explicit error‑handling for MFA verification failures and revocation race conditions, and include unit tests covering these edge cases in the test suite.  
10. Update the Dockerfile and docker‑compose to include a secret‑management sidecar (e.g., HashiCorp Vault or AWS Secrets Manager) that injects JWT signing keys at runtime, preventing hard‑coded key references.  

These changes will close the identified gaps in authentication completeness, CALM compliance, security controls, and auditability while preserving the existing strengths of the design.

---

## Security Review (Score: 85%)

SCORE: 85
SEVERITY: Medium
COVERED: 
- The design implements JWT‑based authentication with short‑lived access tokens and refresh‑token rotation using a revocation list, directly addressing THR‑001 (spoofing) to protect against forged tokens and token replay.
- Input‑validation middleware uses Zod schemas for login payloads, which mitigates THR‑002 (tampering) by preventing NoSQL‑style injection attacks on the user search endpoint.
- Rate‑limiting (120 requests/minute per IP) and express‑rate‑limiter integration provide DoS mitigation, covering OWASP A05 (2023) Misconfiguration and STRIDE LtR‑003.
- All password storage uses bcrypt with a configurable cost factor ≥ 12, satisfying OWASP A02 (cryptographic failures) and NIST SP 800‑63B password‑hashing recommendations.
- The architecture enforces a deny‑by‑default RBAC model via the User.role enum and attaches the user object to `req.user` in `authMiddleware.ts`, fulfilling STRIDE LtI‑001 (authentication) and providing clear authorization boundaries.
- Defense‑in‑depth layers include CSRF protection for state‑changing POST forms (double‑submit token), TLS‑only communication with HSTS and Secure‑cookie flags, and audit‑log capture of security events. These correspond to STRIDE LtD‑005 (disruption), Threat‑model controls for Confidentiality and Data‑integrity, and OWASP A10 (logging).
- Configuration hardening is centralized in `env.ts` with schema validation, preventing accidental exposure of secrets and ensuring TLS certificates are validated via `config.JWT_PRIVATE_KEY`/`PUBLIC_KEY`.

MISSING: 
- The design does not address hard‑coded credentials or secret leakage. While `env.ts` validates JWT keys, it does not explicitly prevent accidental inclusion of secret values in source files or configuration snapshots, which could lead to credential exposure.
- No explicit mechanism for enforcing PCI‑DSS audit‑log retention periods or integration with a centralized SIEM; only placeholder logging is present.
- The design does not reference compliance checklists for GDPR/CCPA data‑subject rights beyond a generic mention of PII masking in logs, leaving a gap in regulatory alignment.
- STRIDE threat ID MAR‑IA‑II (Privilege Escalation) is referenced but not fully mitigated; privilege escalation could occur if role assignment is not validated on every request.

CHANGES: 
1. Remove any hard‑coded secret references; store all secrets (JWT private key, DB credentials) exclusively in environment variables or a secrets manager (e.g., AWS Secrets Manager) and validate them at startup using the existing `EnvSchema`.
2. Add enforcement of audit‑log retention (e.g., 90 days) by configuring log rotation and archival to a compliant storage system, addressing regulatory requirements.
3. Implement privilege‑escalation protection by validating the user’s role on every protected endpoint and ensuring that role changes are persisted and evaluated before granting access.
4. Provide explicit mappings for all STRIDE threat IDs (including LtI‑002, LtL‑004, LtD‑001, LtR‑002) with concrete mitigation steps in the threat‑mitigation table.
5. Incorporate a dedicated compliance checklist entry for GDPR data‑subject access request handling and logging of consent modifications. 
6. Update the README or design documentation to explicitly state that no secrets are committed to version control and to add pre‑commit hooks (e.g., git‑secret‑scan) to detect accidental secret leaks. 
7. Add integration tests that simulate token revocation revocation attacks to verify that `refreshTokenHash` removal works as intended and that no token can be reused after revocation.

---

*Average Score: 84% | Threshold: 80% | Rounds: 1/5*
