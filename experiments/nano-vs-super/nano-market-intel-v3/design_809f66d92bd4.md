<!-- project_id: PRJ-b22ea710 -->
<!-- Version: v1 | Round: 1 -->

# Create implementation design for: Authentication patterns for identity services (prd_id: a40e54c0b4dc) (research_id: 2c84ac151710) (project_id: PRJ-b22ea710) — Implementation Design

---  

## 1. Project Structure  

```
src/
│
├─ api/               # Express / Fastify route definitions
│   ├─ v1/            # API version 1
│   │   ├─ auth.ts   # /login, /refresh, /logout endpoints
│   │   └─ users.ts  # /users/:id/search, /users/:id/reviews endpoints
│   └─ schema/        # Validation schemas (zod)
│
├─ middleware/        # Core middleware (auth, rate‑limit, error)
│   ├─ auth.ts        # JWT verification & RBAC
│   ├─ rateLimiter.ts # express-rate-limit wrapper
│   └─ errorHandler.ts
│
├─ services/          # Business logic (auth service, user service)
│   ├─ authService.ts
│   └─ userService.ts
│
├─ repositories/      # Data‑access layer (MongoDB) – DAO
│   ├─ user.dao.ts
│   └─ audit.dao.ts
│
├─ models/            # TypeScript interfaces that map to DB schemas
│   ├─ user.model.ts
│   └─ audit.model.ts
│
├─ utils/             # Helpers (jwt.ts, password.ts, logger.ts)
│
├─ config/            # Configuration loader (dotenv, joi schema)
│   └─ index.ts
│
├─ app.ts             # Express/Fastify app composition
├─ server.ts          # Entry point (listen)
└─ logger.ts          # Structured winston logger
```

**Responsibility Mapping**

| Folder | Responsibility |
|--------|-----------------|
| `api/` | Pure Express/Fastify handlers; thin controllers. |
| `middleware/` | Reusable middlewares (auth, rate limiting, error handling). |
| `services/` | Orchestrates use‑cases; contains domain logic. |
| `repositories/` | MongoDB queries; abstracted from services. |
| `models/` | Type‑safe interfaces representing rows and embedded docs. |
| `utils/` | Cryptography helpers, logger, etc. |
| `config/` | Centralised validation of `process.env`. |
| `logger/` | Consistent structured logs (JSON). |

---

## 2. API Endpoint Specifications  

| Method | Path | Description | Request Body (TS Interface) | Response (TS Interface) | Status |
|--------|------|-------------|-----------------------------|------------------------|--------|
| `POST` | `/api/v1/auth/login` | Issue access & refresh JWTs after MFA for `admin|reviewer` roles. | `LoginRequest` – `{ username: string; password: string; mfaToken?: string }` | `LoginResponse` – `{ accessToken: string; refreshToken: string; expiresIn: number }` | `200` |
| `POST` | `/api/v1/auth/refresh` | Rotate refresh token, revoke previous token. | `RefreshRequest` – `{ refreshToken: string }` | `RefreshResponse` – `{ accessToken: string; refreshToken: string; expiresIn: number }` | `200` |
| `POST` | `/api/v1/auth/logout` | Invalidate supplied refresh token. | `LogoutRequest` – `{ refreshToken: string }` | `LogoutResponse` – `{ success: boolean }` | `200` |
| `POST` | `/api/v1/users/:userId/search` | Search users (admin only). | `SearchQuery` – `{ q: string; limit?: number; skip?: number }` | `SearchResult` – `{ users: User[]; total: number }` | `200` |
| `GET` | `/api/v1/users/:userId/reviews` | Retrieve paginated reviews written by a user. | – | `ReviewPage` – `{ reviews: Review[]; pageInfo: { totalPages, currentPage } }` | `200` |
| `GET` | `/health` | Liveness probe. | – | `{ status: "ok" }` | `200` |

**Common Response Envelope**

```ts
interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: {
    code: string;
    message: string;
  };
}
```

All endpoints return `ApiResponse<T>` JSON and set `Content-Type: application/json`.

---

## 3. Data Models  

### 3.1 User Model (`src/models/user.model.ts`)

```ts
export interface IUser {
  _id: string;                // ObjectId (auto)
  username: string;           // Unique, lowercase alphanumeric, min 4 chars
  email: string;              // Unique, validated format
  passwordHash: string;       // bcrypt hash, cost >= 12
  role: 'admin' | 'reviewer' | 'viewer';
  mfaEnabled: boolean;        // true if MFA is configured
  // Optional MFA secret (for TOTP) – stored encrypted
  mfaSecret?: string;
  // timestamps
  createdAt: Date;
  updatedAt: Date;
}

/**
 * MongoDB schema – only fields required for auth flows are stored.
 */
import { Schema } from 'mongoose';

export const UserSchema = new Schema<IUser>({
  username: { type: String, required: true, unique: true, minlength: 4, lowercase: true, match: /^[a-z0-9]{4,}$/ },
  email:    { type: String, required: true, unique: true, lowercase: true, match: /^[\w.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/ },
  passwordHash: { type: String, required: true },
  role: { type: String, enum: ['admin', 'reviewer', 'viewer'], required: true, default: 'viewer' },
  mfaEnabled: { type: Boolean, default: false },
  mfaSecret: { type: String }, // encrypted; omitted from public fields
}, { timestamps: true });
```

### 3.2 Review Sub‑document (`src/models/user.model.ts` – embedded)

```ts
export interface IReview {
  reviewId: string;               // ObjectId as string
  title: string;
  rating: number;                 // 1‑5
  createdAt: Date;
  updatedAt: Date;
}

export const ReviewSubSchema = new Schema<IReview>({
  reviewId: { type: Schema.Types.ObjectId, required: true },
  title:    { type: String, required: true, minlength: 1, maxlength: 255 },
  rating:   { type: Number, required: true, min: 1, max: 5 },
}, { _id: false });

export const UserSchema = new Schema<IUser>({
  // …previous fields…
  reviews: { type: [ReviewSubSchema], default: [] },
});
```

*Indexes* (created in `src/repositories/user.dao.ts`):

```ts
UserSchema.index({ username: 1 });
UserSchema.index({ email: 1 });
UserSchema.index({ role: 1 });
```

---

## 4. Authentication Middleware Implementation  

### 4.1 JWT Utility (`src/utils/jwt.ts`)

```ts
import jwt from 'jsonwebtoken';
import { config } from '../config';
import { IUser } from '../models/user.model';

export interface JwtPayload {
  sub: string;            // user id
  role: IUser['role'];
  iat: number;
  exp: number;
}

/**
 * Signs an access token (short‑lived 15 min) and a refresh token (7 days).
 */
export function signAccessToken(user: IUser): string {
  return jwt.sign(
    { sub: user._id, role: user.role },
    config.jwtAccessSecret,
    { algorithm: 'RS256', expiresIn: config.jwtAccessExpiresIn } // e.g., '15m'
  );
}

export function signRefreshToken(user: IUser): string {
  return jwt.sign(
    { sub: user._id },
    config.jwtRefreshSecret,
    { algorithm: 'RS256', expiresIn: config.jwtRefreshExpiresIn } // e.g., '7d'
  );
}

/**
 * Public verification function used by middleware.
 */
export function verifyAccessToken(token: string): JwtPayload | null {
  try {
    const decoded = jwt.verify(token, config.jwtAccessSecret, { algorithms: ['RS256'] }) as JwtPayload;
    return decoded;
  } catch {
    return null;
  }
}
```

### 4.2 Auth Middleware (`src/middleware/auth.ts`)

```ts
import { FastifyReply, FastifyRequest } from 'fastify';
import { verifyAccessToken } from '../utils/jwt';
import { IUser } from '../models/user.model';
import { getUserById } from '../repositories/user.dao';

/**
 * Verifies access token, attaches user object to request, enforces RBAC.
 */
export async function authenticate(
  request: FastifyRequest,
  reply: FastifyReply
) {
  const authHeader = request.headers.authorization;
  if (!authHeader?.startsWith('Bearer ')) {
    return reply.code(401).send({ success: false, error: { code: 'UNAUTHORIZED', message: 'Missing token' } });
  }

  const token = authHeader.split(' ')[1];
  const payload = verifyAccessToken(token);
  if (!payload) {
    return reply.code(401).send({ success: false, error: { code: 'UNAUTHORIZED', message: 'Invalid token' } });
  }

  // fetch fresh user (optional revocation check)
  const user: IUser | null = await getUserById(payload.sub);
  if (!user || !user.isActive) {
    return reply.code(401).send({ success: false, error: { code: 'UNAUTHORIZED', message: 'User not found' } });
  }

  // store user on request for downstream handlers
  (request as any).user = user;

  // Simple RBAC guard – can be reused for route‑level protection
  if (request.routerPath?.startsWith('/api/v1/users/') && !['admin', 'reviewer'].includes(user.role)) {
    return reply.code(403).send({ success: false, error: { code: 'FORBIDDEN', message: 'Insufficient privileges' } });
  }

  return reply.send(); // continue
}
```

*The middleware attaches the authenticated `user` to `request` and aborts with `403` for unauthorized roles when accessing `/api/v1/users/*` endpoints.*

---

## 5. Security Control Implementations  

### 5.1 Rate Limiting (`src/middleware/rateLimiter.ts`)

```ts
import rateLimit from '@fastify/rate-limit';
import { config } from '../config';

export const apiRateLimiter = rateLimit({
  max: config.rateLimit.max,               // e.g., 120 requests per minute per IP
  timeWindow: '1 minute',
  errorResponseBuilder: (req, context) => ({
    success: false,
    error: {
      code: 'TOO_MANY_REQUESTS',
      message: 'Rate limit exceeded. Try again later.',
    },
  }),
});
```

### 5.2 Input Validation (`src/api/v1/schema/user.schema.ts`)

```ts
import { z } from 'zod';

export const loginSchema = z.object({
  username: z.string().min(4).max 30,
  password: z.string().min(8),
  mfaToken: z.string().optional(),
});

export const searchQuerySchema = z.object({
  q: z.string().min(1).max 100,
  limit: z.number().optional().default(20),
  skip: z.number().optional().default(0),
});
```

Used in route handlers:

```ts
await loginSchema.parseAsync(req.body);
```

### 5.3 CSRF Protection (Stateless JWT Approach)

Because we are using **stateless JWTs**, classic CSRF tokens are not required for `POST /login` and `POST /refresh`. However, for any **state‑changing** endpoints that rely on cookies, we can enforce a double‑submit cookie strategy:

```ts
// In Fastify plugin registration
fastify.register(csrf, {
  checkOrigin: true,
  key: (req) => req.headers['csrf-token'] ?? '',
});
```

*Implementation note:* CSRF protection is optional; we rely on **CORS** (`origin: config.corsOrigin`) and **Authorization header** instead.

### 5.4 Token Rotation (Refresh Token Rotation)

```ts
// src/services/auth.service.ts (excerpt)
async function rotateRefreshToken(oldRefresh: string): Promise<{ accessToken: string; refreshToken: string }> {
  const payload = jwt.decode(oldRefresh) as { sub: string } | null;
  if (!payload) throw new Error('Invalid refresh token');

  // Find user, ensure token is still stored in DB (revocation list)
  const stored = await redisGet(oldRefresh); // stored token ID if not revoked
  if (!stored) throw new Error('Refresh token revoked');

  // Invalidate old token on Redis
  await redisDel(oldRefresh);

  // Issue new pair
  const user = await getUserById(payload.sub);
  const newAccess = signAccessToken(user);
  const newRefresh = signRefreshToken(user);
  await redisSet(newRefresh, '1', 'EX', 7 * 24 * 60 * 60); // 7‑day TTL

  return { accessToken: newAccess, refreshToken: newRefresh };
}
```

### 5.5 Session Management (Revocation List)

We use **Redis** as an in‑memory revocation store.

```ts
// src/utils/redis.ts
import Redis from 'ioredis';
export const redis = new Redis(config.redisUrl);

export async function redisSet(key: string, value: string, opts?: { EX?: number }) {
  await redis.set(key, value, 'EX', opts?.EX ?? 0);
}
export async function redisGet(key: string): Promise<string | null> {
  return await redis.get(key);
}
export async function redisDel(key: string) {
  await redis.del(key);
}
```

When a refresh token is used or revoked, its identifier (`jti`) is stored in Redis with a short TTL to prevent replay.

---

## 6. Configuration and Environment Variables  

`src/config/index.ts`

```ts
import { config as loadDotenv } from 'dotenv';
loadDotenv();

export const config = {
  port: Number(process.env.PORT) || 3000,
  corsOrigin: process.env.CORS_ORIGIN || '*',
  jwtAccessSecret: process.env.JWT_ACCESS_SECRET!,
  jwtRefreshSecret: process.env.JWT_REFRESH_SECRET!,
  jwtAccessExpiresIn: process.env.JWT_ACCESS_EXPIRES_IN || '15m',
  jwtRefreshExpiresIn: process.env.JWT_REFRESH_EXPIRES_IN || '7d',
  rateLimit: {
    max: Number(process.env.RATE_LIMIT_MAX) || 120,
  },
  redisUrl: process.env.REDIS_URL || 'redis://localhost:6379',
  // Redis TTLs for revocation
  revocationTTL: Number(process.env.REVOCATION_TTL_SECONDS) || 300, // 5 min
  // Password hashing
  bcryptCost: Number(process.env.BCRYPT_COST) || 12,
  // Logging
  logLevel: process.env.LOG_LEVEL || 'info',
};
```

All env vars are validated using **joi** in a separate validation script that runs on service start‑up; missing required variables cause a graceful exit with an error message.

---

## 7. Error Handling Patterns  

### 7.1 Custom Error Classes (`src/utils/error.ts`)

```ts
export class ApiError extends Error {
  public readonly code: string;
  public readonly status: number;
  public readonly details?: any;

  constructor(code: string, status: number, message: string, details?: any) {
    super(message);
    this.code = code;
    this.status = status;
    this.details = details;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}
```

### 7.2 Global Error Handler (`src/middleware/errorHandler.ts`)

```ts
import { FastifyReply, FastifyRequest } from 'fastify';
import { ApiError } from '../utils/error';

export function errorHandler(err: Error, _req: FastifyRequest, reply: FastifyReply) {
  if (err instanceof ApiError) {
    return reply
      .code(err.status)
      .send({ success: false, error: { code: err.code, message: err.message, details: err.details } });
  }

  // Unexpected errors – log and return generic 500
  logger.error(err);
  return reply
    .code(500)
    .send({ success: false, error: { code: 'INTERNAL_ERROR', message: 'Something went wrong.' } });
}
```

### 7.3 Example Usage in a Handler

```ts
async function loginHandler(request: FastifyRequest, reply: FastifyReply) {
  try {
    const { username, password, mfaToken } = request.body as LoginRequest;
    const user = await findUserByUsername(username);
    if (!user) throw new ApiError('USER_NOT_FOUND', 401, 'Invalid credentials');

    // verify password hash
    const isValid = await bcrypt.compare(password, user.passwordHash);
    if (!isValid) throw new ApiError('INVALID_PASSWORD', 401, 'Invalid credentials');

    // enforce MFA for privileged roles
    if (['admin', 'reviewer'].includes(user.role) && !user.mfaEnabled) {
      throw new ApiError('MFA_REQUIRED', 401, 'MFA required for this account');
    }

    // issue tokens
    const access = signAccessToken(user);
    const refresh = signRefreshToken(user);

    await redisSet(refresh, '1', 'EX', config.revocationTTL);
    return reply.send({
      success: true,
      data: { accessToken: access, refreshToken: refresh, expiresIn: config.jwtAccessExpiresIn },
    });
  } catch (e) {
    return reply.throw(e); // forwarded to errorHandler
  }
}
```

---

## 8. Testing Strategy with Example Test Cases  

### 8.1 Unit Tests (Jest + mongodb‑memory‑server)

`src/__tests__/auth.service.test.ts`

```ts
import { jest } from '@jest/globals';
import { signAccessToken, signRefreshToken } from '../utils/jwt';
import { getUserById } from '../repositories/user.dao';
import { authenticate } from '../middleware/auth';
import { FastifyRequest, FastifyReply } from 'fastify';

jest.mock('../repositories/user.dao');

describe('authenticate middleware', () => {
  const mockUser = { _id: 'u123', role: 'admin', isActive: true } as any;

  beforeEach(() => {
    (getUserById as jest.Mock).mockResolvedValue(mockUser);
  });

  it('should reject request without bearer token', async () => {
    const req = { headers: { authorization: undefined } } as unknown as FastifyRequest;
    const reply = {} as FastifyReply;
    const result = await authenticate(req, reply);
    expect(reply.code).toBe(401);
  });

  it('should attach user and allow admin routes', async () => {
    const req = {
      headers: { authorization: `Bearer ${signAccessToken(mockUser)}` },
      routerPath: '/api/v1/users/123/reviews',
    } as unknown as FastifyRequest;
    const reply = {} as FastifyReply;
    await authenticate(req, reply);
    expect((req as any).user).toEqual(mockUser);
  });
});
```

### 8.2 Integration Test (Full Stack)

```ts
import Fastify from 'fastify';
import { server } from '../server';
import { redis } from '../utils/redis';

afterAll(async () => {
  await redis.flushall();
  await server.close();
});

test('POST /api/v1/auth/login returns tokens', async () => {
  const response = await fastify.inject({
    method: 'POST',
    url: '/api/v1/auth/login',
    payload: {
      username: 'admin01',
      password: 'StrongPass!23',
      mfaToken: '123456', // generated by TOTP app
    },
  });
  expect(response.statusCode).toBe(200);
  const body = response.json() as any;
  expect(body.success).toBe(true);
  expect(body.data.accessToken).toBeDefined();
  expect(body.data.refreshToken).toBeDefined();
});
```

**Mocking Strategy**

* **MongoDB**: Use `mongodb-memory-server` to spin up an in‑memory replica set; seed with test users.
* **Redis**: Use `ioredis-mock` to simulate revocation list without a real Redis server.
* **External services** (email, MFA provider) – mock functions in `utils/mfa.ts`.

**Test Coverage Goal**: ≥ 80 % line coverage across `src/` modules; all security‑related paths exercised (invalid token, revoked token, MFA failure).

---

## 9. Deployment Configuration  

### 9.1 Dockerfile  

```dockerfile
# ---- Build stage ----
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
RUN npm run lint && npm run build   # tsc compilation

# ---- Production stage ----
FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/dist ./dist
COPY package*.json ./
RUN npm ci --only=production
EXPOSE 3000
CMD ["node", "dist/server.js"]
```

### 9.2 Docker‑Compose (Local dev)  

```yaml
version: '3.8'
services:
  api:
    build: .
    ports: [{ "target": 3000, "published": 3000, "mode": "host" }]
    environment:
      - PORT=3000
      - JWT_ACCESS_SECRET=${JWT_ACCESS_SECRET}
      - JWT_REFRESH_SECRET=${JWT_REFRESH_SECRET}
      - REDIS_URL=redis://redis:6379
      - BCRYPT_COST=12
      - RATE_LIMIT_MAX=120
    depends_on: [redis, mongo]
  mongo:
    image: mongo:6
    ports: [{ "target": 27017, "published": 27017, "mode": "host" }]
    volumes: [{ source: mongo-data, target: /data/db }]
  redis:
    image: redis:7
    ports: [{ "target": 6379, "published": 6379, "mode": "host" }]
  # health‑check endpoint exposed via /health
```

### 9.3 Health Checks  

* **Liveness**: `GET /health` returns `{ "status": "ok" }` – returns **200** within 5 seconds.  
* **Readiness**: Fastify hook that verifies Redis connectivity and can query a single document from MongoDB; returns **200** only if both succeed.

```ts
fastify.register(function (fastify, opts, done) {
  fastify.get('/ready', async (req, reply) => {
    try {
      await redis.ping();
      await fastify.db.collection('users').findOne({});
      return reply.send({ status: 'ready' });
    } catch (e) {
      return reply.code(503).send({ status: 'unready' });
    }
  });
  done();
});
```

---

### **Traceability Summary**

| Element | Premises Covered |
|---------|------------------|
| Architecture (ADR‑001, ADR‑003, ADR‑005) | R4, R5, expert E1 |
| NIST‑compliant MFA & token rotation | R6, E2 |
| OWASP ASVS access‑control & input validation | R7, expert E4 |
| Performance target p95 < 200 ms (ADR‑006) | R2, E5 |
| Security controls (rate limiting, revocation, logging) | R9, expert E4 |

All sections above are **actionable**; a developer can clone the repository, run `npm install`, populate `.env` from the provided template, start the services via `docker‑compose up -d`, and immediately begin coding the authentication flows described.

---
**Completeness Check:** PRD requirement may not be covered: 'Traced to **R2** (market growth & latency target), **E2** (s'; PRD requirement may not be covered: 'Traced to **R9** (breach statistics), **E3** (MFA & bcrypt),'; PRD requirement may not be covered: 'Traced to **R3** (market demand for reliability), **E1** (se'

---
## Guardrail Review Notes

*The following items were flagged by automated guardrails for human review:*

- [WARN] compliance: Possible secret in document: Possible hardcoded password

*These may be false positives in a design document.*
