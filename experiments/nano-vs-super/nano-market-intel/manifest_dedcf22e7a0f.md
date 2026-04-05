<!-- project_id: PRJ-e5e6bd5f -->
# Authentication patterns for identity services — Requirements Manifest

```json
{
  "endpoints": [
    {
      "method": "POST",
      "path": "/auth/login",
      "description": "Initiates authentication flow; validates credentials, issues short\u2011lived JWT and rotating refresh token."
    },
    {
      "method": "POST",
      "path": "/auth/refresh",
      "description": "Exchanges valid refresh token for new access and refresh tokens."
    },
    {
      "method": "POST",
      "path": "/auth/revoke",
      "description": "Invalidates the supplied refresh token immediately."
    },
    {
      "method": "GET",
      "path": "/movies",
      "description": "Returns list of movies; requires appropriate RBAC role."
    },
    {
      "method": "POST",
      "path": "/movies",
      "description": "Creates a new movie record."
    },
    {
      "method": "GET",
      "path": "/sso/marketplace",
      "description": "Provides access to SSOJet SDK and other third\u2011party integrations."
    }
  ],
  "security_requirements": [
    "token_revocation",
    "short_lived_jwt",
    "rotating_refresh_tokens",
    "bcrypt_password_hashing",
    "deny_by_default_rbac",
    "json_schema_validation",
    "immutable_audit_logging",
    "pii_masking",
    "static_key_rotation",
    "compliance_with_nist_sp_800_63b",
    "compliance_with_gdpr_articles_5_6"
  ],
  "technology_constraints": [
    "TypeScript",
    "Node.js",
    "Express",
    "MongoDB",
    "Mongoose",
    "mongodb-memory-server",
    "Redis",
    "JWT",
    "bcrypt",
    "HMAC-SHA256",
    "Calm"
  ],
  "quality_targets": {
    "latency_p99_ms": 200,
    "availability_99_9_percent": true,
    "test_coverage_percent": 80,
    "patent_filings_count": 2
  }
}
```
