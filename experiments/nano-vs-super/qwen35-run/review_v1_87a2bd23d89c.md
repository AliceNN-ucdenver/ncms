<!-- project_id: PRJ-963bc59e -->
# Design Review Report — Authentication patterns for identity services

**Status:** APPROVED at 84% after 3 round(s)
**Design Document:** 5802286f6aa0
**Review Rounds:** 3
**Quality Threshold:** 80%

---

## Architecture Review (Score: 92%)

SCORE: 92
SEVERITY: Low
COVERED: The design correctly aligns with ADR-001 by removing the phantom Identity Service and adopting a modular monolith structure within the Movie API. It adheres to ADR-002 by utilizing MongoDB and adding a RefreshToken collection for audit trails. It significantly enhances ADR-003 by switching to RS256 asymmetric signing for NIST 800-63-4 compliance, implementing Redis for the Token Revocation List (TRL), and adding health checks with TLS enforcement. The design includes essential quality attributes such as rate limiting, CSP headers, and non-root Docker execution.
MISSING: The design claims ADR-004 (Infrastructure) compliance but lacks a documented deployment topology diagram or specific CALM JSON artifacts proving the component boundaries. The refresh token logic in the controller implementation contains a race condition risk where the pipeline execution result is checked, but the subsequent DB write for the new token is not wrapped in the same atomic transaction as the revocation check, potentially allowing double-spend of refresh tokens under high concurrency. The React Frontend service is defined but the implementation of the `httpOnly` cookie handling on the frontend proxy is not detailed in the code snippet.
CHANGES:
1. Refactor the refresh token logic to use a Redis MULTI/EXEC transaction that atomically revokes the old token and stores the new active JTI in a single atomic operation before writing to MongoDB, ensuring no race conditions occur between Redis and DB states.
2. Update the RefreshToken MongoDB schema to store the Refresh Token JTI (not the Access Token string) to reduce storage size and ensure the audit log remains consistent with the Redis state.
3. Provide a concrete CALM model JSON file or a diagram artifact in the documentation that maps the `auth` and `users` modules to the `movie-api` service boundary as defined in ADR-001 to verify architectural compliance.
4. Add a specific middleware or configuration check in the frontend proxy to ensure `secure` cookies are only accepted over HTTPS and that the `sameSite` attribute is strictly enforced for the `refresh_token` cookie.

---

## Security Review (Score: 75%)

SCORE: 75
SEVERITY: High
COVERED: The design effectively addresses STRIDE threats THR-001 (Spoofing) by implementing RS256 asymmetric tokens and NIST 800-63-4 compliance. It mitigates THR-002 (Tampering) via strict input validation, parameterized queries (implied by schema), and MongoDB schema definition. Transport Security is strengthened with TLS 1.3 enforcement, HSTS headers, and secure cookie flags (HttpOnly, Secure, SameSite=Strict). Secrets management is improved by requiring RSA keys via environment variables or mounted volumes, removing hardcoded credentials. Supply chain risks are acknowledged with npm audit in CI. Atomicity in token rotation is addressed using Redis transactions to prevent race conditions (THR-003 Repudiation mitigation).

MISSING: The design contains critical flaws that leave specific threats unmitigated or introduce new high-severity risks. 1. Hardcoded secrets in Docker Compose: The `REDIS_PASSWORD` is explicitly set to a static string in the YAML file (`REDIS_PASSWORD=StrongRedisPassword123!`), violating the principle of secrets management and exposing credentials in version control or docker-compose files. 2. Security Critical Implementation Error: The code loads RSA private keys from a string in the environment variable (`config.RS256_PRIVATE_KEY`) but then attempts to pass the file path of the key to `https.createServer` in `server.ts` while simultaneously trying to use the env var for signing. The `server.ts` code assumes certificates are mounted at `/certs`, but the `token.util.ts` expects them as strings in `RS256_PRIVATE_KEY`. This mismatch means either the server won't start with valid TLS or the tokens won't be signed correctly in production. 3. Insecure Certificate Handling: The health check in the Dockerfile uses `rejectUnauthorized: false`, disabling TLS verification for internal health checks, which can mask certificate validity issues or allow MITM attacks on internal service-to-service comms. 4. Missing MFA: The compliance checklist shows MFA for privileged accounts as "not_started" despite the design claiming NIST 800-63-4 compliance, which mandates MFA. 5. Token Storage Risk: The `RefreshToken` model stores the full `accessToken` in the `token` field (line in `auth.controller.ts` comment `token: newTokens.accessToken`), which is unnecessary and increases the attack surface if the DB is compromised; it should store only the JTI or a hash. 6. Rate Limiting Bypass: The login rate limiter uses `req.ip`, but behind a load balancer or reverse proxy without `X-Forwarded-For` handling, all requests will appear from the proxy IP, allowing brute force attacks. 7. CSP Inconsistency: The CSP header allows `script-src 'self'` but the frontend logic relies on cookies which may trigger CSP issues if not carefully aligned with `connect-src` and `img-src` directives for the proxy.

CHANGES:
1. Remove hardcoded `REDIS_PASSWORD` from Docker Compose; use a secrets file or Kubernetes secrets, ensuring the password is generated dynamically or injected securely at runtime.
2. Standardize key handling: Either mount the private key as a file to the application container and load it via `fs.readFileSync` in `token.util.ts` to match the `server.ts` certificate loading pattern, or remove the file mount requirement and ensure `token.util.ts` loads the key from the environment variable correctly without expecting file paths.
3. Fix the health check in Docker to remove `rejectUnauthorized: false` and ensure the container has the proper root CA bundle installed to verify its own certificate if used internally.
4. Implement MFA for the `admin` and `reviewer` roles in the `auth.service` and update the `User` schema to store MFA status, aligning with NIST 800-63-4.
5. Change the `RefreshToken` model to store only the JTI or a hash of the token in the database, not the full access token string, to reduce data exposure.
6. Update the rate limiter configuration to extract the real client IP from `req.headers['x-forwarded-for']` or `req.socket.remoteAddress` to prevent bypass via load balancer IP.
7. Remove the explicit `token` field from the `RefreshToken` schema or ensure it stores only a hash/JTI, and update the `auth.controller.ts` logic to reflect this change.
8. Review CSP directives to ensure they align with the actual content served by the React frontend and any API calls made by the client, particularly regarding `connect-src` and `frame-ancestors`.

---

*Average Score: 84% | Threshold: 80% | Rounds: 3/5*
