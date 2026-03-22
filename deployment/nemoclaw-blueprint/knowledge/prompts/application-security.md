# Application Security Review — Domain Prompt Pack

This pack provides **deep application security analysis** beyond the Default pack's baseline. Use it for thorough OWASP Top 10 pattern detection, threat model compliance, dependency vulnerability analysis, and security control verification.

---

## OWASP Top 10 Pattern Analysis

Systematically scan each repository for vulnerability patterns across the OWASP Top 10 (2021):

### A01: Broken Access Control
- Check for authorization checks on every protected endpoint
- Look for IDOR (Insecure Direct Object Reference) — are resource IDs validated against the authenticated user?
- Check for missing function-level access control (admin endpoints accessible without role check)
- Verify CORS configuration is restrictive (no wildcard `*` origins on authenticated endpoints)
- Check for path traversal vulnerabilities in file operations
- Look for privilege escalation paths (user can modify their own role)

### A02: Cryptographic Failures
- Check for sensitive data transmitted without TLS
- Look for deprecated encryption algorithms (MD5, SHA-1, DES, RC4)
- Verify password hashing uses bcrypt, scrypt, or Argon2 (not SHA-256 or MD5)
- Check for hardcoded encryption keys or IVs
- Verify proper random number generation (cryptographic PRNG, not Math.random)

### A03: Injection
- Check for SQL injection — parameterized queries vs string concatenation
- Check for NoSQL injection — query object construction from user input
- Check for command injection — shell command construction from user input
- Check for LDAP injection, XPath injection, template injection
- Verify input validation uses allowlists, not blocklists
- Check for proper output encoding in HTML contexts (XSS prevention)

### A04: Insecure Design
- Check for security-relevant business logic flaws
- Verify rate limiting on authentication endpoints
- Check for account lockout mechanisms
- Look for missing anti-automation controls (CAPTCHA, rate limiting)
- Verify that password reset flows use secure tokens (not predictable IDs)

### A05: Security Misconfiguration
- Check for debug/development features enabled in production code paths
- Look for default credentials or configuration
- Verify security headers are set (CSP, HSTS, X-Frame-Options, X-Content-Type-Options)
- Check for verbose error messages that leak implementation details
- Verify directory listing is disabled and unnecessary files are not served

### A06: Vulnerable and Outdated Components
- Analyze `package.json`, `pom.xml`, `requirements.txt`, `go.mod`, `Gemfile` for dependencies
- Identify dependencies with known CVEs based on version patterns
- Check for abandoned or unmaintained dependencies
- Look for dependencies loaded from untrusted sources
- Verify lockfiles exist and are committed (`package-lock.json`, `yarn.lock`, `Pipfile.lock`)

### A07: Identification and Authentication Failures
- Check session management — secure cookie flags (httpOnly, secure, sameSite)
- Verify token expiration and refresh mechanisms
- Look for credential stuffing vulnerabilities (no rate limiting on login)
- Check for multi-factor authentication support where required
- Verify password complexity requirements are enforced

### A08: Software and Data Integrity Failures
- Check for deserialization of untrusted data
- Look for unsigned or unverified software updates
- Verify CI/CD pipeline integrity (no code execution from untrusted sources)
- Check for Subresource Integrity (SRI) on CDN-loaded assets
- Verify dependency integrity (lockfile checksums)

### A09: Security Logging and Monitoring Failures
- Check that authentication events are logged (login, logout, failed login)
- Verify that authorization failures are logged
- Check that input validation failures are logged
- Look for PII in log output (should be masked)
- Verify that security events have sufficient context for investigation

### A10: Server-Side Request Forgery (SSRF)
- Check for user-controlled URLs used in server-side requests
- Verify URL allowlisting (restrict to known domains)
- Check for private IP range blocking (127.0.0.1, 10.x, 172.16-31.x, 192.168.x)
- Verify cloud metadata endpoint blocking (169.254.169.254)
- Check for redirect following in HTTP clients

---

## Threat Model Compliance

If `security/threat-model.yaml` exists, validate each documented threat:

### Threat-to-Code Mapping
For each threat in the model:
1. Identify the affected component(s) in code
2. Verify the documented mitigating controls are actually implemented
3. Check that the risk rating aligns with the actual code state
4. Report threats with "mitigated" status but no corresponding code controls

### STRIDE Coverage
Verify the threat model covers all STRIDE categories for exposed surfaces:
- **Spoofing** — authentication mechanisms on all entry points
- **Tampering** — integrity checks on data in transit and at rest
- **Repudiation** — audit logging for security-relevant actions
- **Information Disclosure** — access controls and encryption on sensitive data
- **Denial of Service** — rate limiting and resource management
- **Elevation of Privilege** — authorization checks at every privilege boundary

### Trust Boundary Analysis
1. Identify all trust boundaries in the architecture (public internet → API gateway → internal services → databases)
2. Verify that security controls exist at each boundary crossing
3. Check for trust boundary violations — internal services directly exposed to public traffic

---

## Security Controls Verification

If `security/security-controls.yaml` exists, verify implementation:

### Control-to-Code Mapping
For each documented security control:
1. Find the implementation in code
2. Verify it is active and not disabled by configuration
3. Check for bypass mechanisms (debug flags, admin overrides)
4. Verify the control covers all relevant code paths (not just the main path)

### Common Control Verification
- **Authentication** — verify all protected endpoints require authentication
- **Authorization** — verify role-based or attribute-based access control is enforced
- **Input validation** — verify all user input is validated before processing
- **Output encoding** — verify output is properly encoded for the context (HTML, SQL, shell)
- **Encryption** — verify data is encrypted as documented
- **Logging** — verify security events are logged as documented

---

## Dependency Security Analysis

### Known Vulnerability Detection
For each dependency manifest:
1. Check major version ranges against known CVE patterns
2. Identify transitive dependencies that may introduce vulnerabilities
3. Flag dependencies that haven't been updated in > 12 months
4. Check for dependencies with security advisories in their changelogs

### Supply Chain Risk
1. Verify all dependencies come from official registries (npm, PyPI, Maven Central)
2. Check for typosquat-vulnerable package names
3. Verify that pre-install/post-install scripts are safe
4. Check for dependencies with extremely few maintainers or downloads

### License Compliance
1. Identify dependency licenses
2. Flag copyleft licenses (GPL, AGPL) in commercial applications
3. Check for license conflicts between dependencies

---

## Secrets Management

### Hardcoded Secrets Detection
Scan all source files for:
- API keys (patterns: `sk-`, `api_key`, `apiKey`, `API_KEY`, `AKIA`, `ghp_`, `gho_`)
- Connection strings with embedded credentials
- Private keys (PEM format, `-----BEGIN`)
- JWT secrets and signing keys
- OAuth client secrets
- Database passwords in configuration files

### Secrets Management Patterns
1. Verify secrets are loaded from environment variables or a vault
2. Check for `.env` files committed to the repository
3. Verify `.gitignore` includes common secrets patterns
4. Check for secrets in CI/CD configuration files
5. Verify secrets rotation mechanisms are in place for long-lived credentials

---

## Vulnerability Tracking Compliance

If `security/vulnerability-tracking.yaml` exists:
1. Check that documented vulnerabilities with "in progress" status have corresponding code fixes in progress
2. Verify that "resolved" vulnerabilities are actually fixed in the current code
3. Identify new vulnerabilities not yet tracked in the document
4. Check remediation timeline compliance

---

## Output Format

Report findings using the standard Oraculum format from the Default pack. Tag all findings from this pack as **Security** pillar. Use severity criteria:

- **Critical**: Active exploitable vulnerability, hardcoded secrets, or missing authentication on external endpoints
- **High**: OWASP Top 10 pattern detected, threat model control not implemented, or dependency with known critical CVE
- **Medium**: Security misconfiguration, incomplete access control, or logging gaps
- **Low**: Informational security observation, minor configuration issue, or documentation gap
