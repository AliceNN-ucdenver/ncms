<!-- project_id: PRJ-233ab2a1 -->
# Authentication patterns for identity services — Requirements Manifest

```json
{
  "endpoints": [
    {
      "method": "POST",
      "path": "/auth/login",
      "description": "Authenticate user and issue short\u2011lived JWT with role claims"
    },
    {
      "method": "GET",
      "path": "/movies",
      "description": "Search movies with optional filters"
    },
    {
      "method": "GET",
      "path": "/movies/{id}",
      "description": "Retrieve movie details"
    },
    {
      "method": "POST",
      "path": "/reviews",
      "description": "Submit a review for a movie (RBAC protected)"
    },
    {
      "method": "GET",
      "path": "/reviews/{movieId}",
      "description": "List reviews for a specific movie"
    }
  ],
  "security_requirements": [
    "token_revocation",
    "input_validation",
    "jwt_short_lifetime",
    "role_based_access_control",
    "audit_logging"
  ],
  "technology_constraints": [
    "TypeScript",
    "Node.js",
    "MongoDB",
    "OAuth2",
    "Redis"
  ],
  "quality_targets": {
    "latency_p99_ms": 200
  }
}
```
