<!-- project_id: PRJ-963bc59e -->
# Authentication patterns for identity services — Requirements Manifest

```json
{
  "endpoints": [
    {
      "method": "POST",
      "path": "/auth/login",
      "description": "Initiates OIDC or SAML authentication flow with JWT issuance."
    },
    {
      "method": "POST",
      "path": "/auth/refresh",
      "description": "Rotates short-lived access tokens using refresh tokens."
    },
    {
      "method": "DELETE",
      "path": "/auth/revoke",
      "description": "Adds token to Redis-based Token Revocation List (TRL)."
    },
    {
      "method": "GET",
      "path": "/dashboard/roi",
      "description": "Displays probability-weighted loss metrics for executive review."
    },
    {
      "method": "POST",
      "path": "/api/resource",
      "description": "Protected endpoint enforcing RBAC via JWT claims in Express Middleware."
    }
  ],
  "security_requirements": [
    "token_revocation",
    "short_lived_jwt",
    "rbac_embedded_claims",
    "input_validation",
    "parameterized_queries",
    "strong_password_hashing",
    "pii_masking",
    "mfa_availability"
  ],
  "technology_constraints": [
    "OIDC",
    "SAML",
    "JWT",
    "Express_Middleware",
    "Redis",
    "MongoDB",
    "bcrypt",
    "Argon2",
    "NIST_SP_800-63-4",
    "OWASP_A01_2021",
    "TypeScript"
  ],
  "quality_targets": {
    "latency_p99_ms": 200,
    "token_validation_latency_ms": 50,
    "revocation_check_latency_ms": 100,
    "concurrent_revocation_lookups": 10000,
    "authentication_success_rate": 99.5,
    "critical_vulnerabilities": 0,
    "token_expiration_minutes": 15,
    "hash_cost_factor": 12
  }
}
```