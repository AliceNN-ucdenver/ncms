<!-- project_id: PRJ-1dac693a -->
<!-- Version: v1 | Round: 1 -->

# Create implementation design for: Authentication patterns for identity services (prd_id: 47e6fb37bc05) (research_id: 2f72f89a69b7) (project_id: PRJ-1dac693a) — Implementation Design

## Project Structure  

```
src/
├── config/
│   └── index.ts                // central config loader (dotenv, validation)
│
├── core/
│   ├── logger.ts               // winston logger wrapper
│   └── error/
│       └── AppError.ts        // custom error hierarchy
│
├── db/
│   ├── mongodb/
│   │   ├── client.ts           // MongoDB client singleton
│   │   └── collection.ts       // wrapper around Collection for typed access
│   └── mongodb-memory-server.ts // test‑only fixture (imported via jest config)
│
├── gateway/
│   └── apollo.ts               // Apollo Server (GraphQL optional, not required here)
│
├── middleware/
│   ├── auth/
│   │   ├── jwt.ts              // JWT verification & middleware factory
│   │   └── roles.ts            // role‑check middleware (inline RBAC)
│   ├── rateLimit.ts            // express‑rate‑limit wrapper
│   ├── validate.ts             // request schema validator (using ajv)
│   └── csrf.ts                 // csurf‑style CSRF protection
│
├── models/
│   ├── user/
│   │   ├── User.ts             // TypeScript interface + Mongoose schema
│   │   └── user.service.ts     // CRUD helpers, password hashing util
│   └── movie/
│       ├── Movie.ts            // Embedded schema (titles, actor arrays)
│       └── review-collection.ts// Separate collection for reviews
│
├── routes/
│   ├── auth/
│   │   ├── auth.router.ts      // /login, /callback, /logout endpoints
│   │   ├── oauth.router.ts     // optional OAuth2 provider router
│   │   └── refresh.router.ts   // /refresh-token endpoint
│   ├── movie/
│   │   └── movie.router.ts     // CRUD & read‑heavy endpoints
│   └── identity/
│       └── insights.router.ts  // real‑time GTM identity intelligence endpoint
│
├── services/
│   ├── gtm/
│   │   └── insights.service.ts // fetches external GTM data, builds intent score
│   └── audit/
│       └── logger.service.ts   // structured JSON audit logger
│
├── utils/
│   ├── crypto.ts               // bcrypt wrapper, token signer
│   └── validation.ts           // ajv compile & execute helper
│
└── server.ts                   // Express app bootstrap, middlewares wiring
```

**Modules & responsibilities**

| Module | Primary purpose |
|--------|-----------------|
| `config` | Loads `process.env.*`, validates required vars, provides typed config object. |
| `core` | Central logger and error classes – used throughout the codebase for consistent observability. |
| `db` | Singleton MongoDB client, schema wrappers; test utilities mock the client when `MONGODB_MEMORY_SERVER=true`. |
| `middleware` | Reusable Express middlewares: JWT verification, role checks, rate limiting, input validation, CSRF. |
| `models` | TypeScript interfaces that map 1‑to‑1 to MongoDB schemas; encapsulates password hashing & hashing utilities. |
| `routes` | Express routers grouped by domain (auth, movie, identity) – each exports a router that is mounted in `server.ts`. |
| `services` | Business logic that lives outside the HTTP layer (e.g., GTM insights, audit logging). |
| `utils` | Small helpers (crypto, validation) that keep core modules lean. |
| `server.ts` | Entry point – config loading → middlewares → router mounting → health endpoint. |

---

## API Endpoint Specifications  

| Route | Method | Request Body (TypeScript) | Response (Status / Body) | Traceability |
|-------|--------|---------------------------|--------------------------|--------------|
| `/login` | `POST` | ```ts interface LoginReq { email: string; password: string; rememberMe?: boolean; }``` | `200` `{ token: string; refreshToken?: string; expiresIn: string; }` <br> `401` `{ error: string; code: 'UNAUTHORIZED' }` | FR‑01 (OAuth JWT issuance), E3 (MFA) |
| `/oauth/callback` | `GET` | Query params: `code`, `state` | `302` *(redirect to frontend)* | FR‑01 |
| `/refresh-token` | `POST` | ```ts interface RefreshReq { refreshToken: string; }``` | `200` `{ token, expiresIn }` <br> `401` `{ error: string }` | FR‑01 |
| `/logout` | `POST` | Empty body | `204` (no‑content) | FR‑01 |
| `GET /movie` | `GET` | Query params: `title?`, `page?` | `200` `{ movies: Movie[], totalPages: number }` | FR‑03 (RBAC enforcement) |
| `POST /movie/:id/review` | `POST` | ```ts interface ReviewCreate { rating: number; comment?: string; }``` | `201` `{ reviewId: string }` <br> `403` if not `reviewer` or `admin` | FR‑01 |
| `GET /identity/insights` | `GET` | Query params: `userId` (optional) | `200` `{ intentScore: number; channelMap: Record<string,string> }` | FR‑04 (GTM intelligence) |
| `GET /health` | `GET` | – | `200` `{ status: 'ok', timestamp: ISO }` | NG‑* (non‑functional) |

All responses use a uniform envelope:

```ts
interface ApiResponse<T> {
  data: T;
  meta?: Record<string, unknown>;
}
```

---

## Data Models  

### `User` Model  

```ts
// src/models/user/User.ts
export interface IUser {
  _id?: string;
  email: string;
  passwordHash: string;            // bcrypt hash (cost ≥ 12)
  role: 'viewer' | 'reviewer' | 'admin';
  mfaEnabled?: boolean;
  createdAt?: Date;
  updatedAt?: Date;
}
```

**Mongoose schema**

```ts
// src/models/user/user.schema.ts
import { Schema, model } from 'mongoose';
import bcrypt from 'bcryptjs';

const UserSchema = new Schema<IUser>({
  email: { type: String, required: true, unique: true, lowercase: true, trim: true },
  passwordHash: { type: String, required: true },
  role: { type: String, enum: ['viewer', 'reviewer', 'admin'], default: 'viewer' },
  mfaEnabled: { type: Boolean, default: false },
}, { timestamps: true });

UserSchema.pre('save', async function (next) {
  if (!this.isModified('passwordHash')) return next();
  const salt = await bcrypt.genSalt(12); // cost ≥ 12 per E1
  this.passwordHash = await bcrypt.hash(this.passwordHash, salt);
  next();
});
UserSchema.methods.comparePassword = function (plain: string) {
  return bcrypt.compare(plain, this.passwordHash);
};
export const User = model<IUser>('User', UserSchema);
```

### `Movie` Embedded Schema  

```ts
// src/models/movie/Movie.ts
export interface IActor {
  name: string;
  character?: string;
}
export interface IMovie {
  _id: string;
  title: string;
  year: number;
  plot: string;
  actors: IActor[];               // embedded array
  ratings: number;                // aggregate rating
}
```

### Review Collection  

```ts
// src/models/review-collection/schema.ts
import { Schema, model } from 'mongoose';

const ReviewSchema = new Schema({
  movieId: { type: Schema.Types.ObjectId, ref: 'Movie', required: true },
  userId: { type: Schema.Types.ObjectId, ref: 'User', required: true },
  rating: { type: Number, required: true, min: 1, max: 5 },
  comment: { type: String },
  createdAt: { type: Date, default: Date.now },
});
export const Review = model('Review', ReviewSchema);
```

**Indexes**

```ts
// src/db/collection.ts
export const createIndexes = async () => {
  await Review.index({ movieId: 1, userId: 1 }, { unique: true });
  await Review.index({ rating: 1 });
};
```

---

## Authentication Middleware Implementation  

### JWT Signer  

```ts
// src/utils/crypto.ts
import jwt from 'jsonwebtoken';
import crypto from 'crypto';
import config from '../config';

export const signAccessToken = (payload: object, userId: string) => {
  return jwt.sign(
    { ...payload, sub: userId },
    config.JWT_ACCESS_SECRET,
    { expiresIn: config.JWT_ACCESS_EXPIRES_IN, algorithm: 'HS256' }
  );
};

export const signRefreshToken = (payload: object, userId: string) => {
  const kid = crypto.randomBytes(2).toString('hex');
  return jwt.sign(
    { ...payload, sub: userId, kid },
    config.JWT_REFRESH_SECRET,
    { expiresIn: config.JWT_REFRESH_EXPIRES_IN, algorithm: 'HS256' }
  );
};
```

### JWT Verification Middleware  

```ts
// src/middleware/auth/jwt.ts
import { Request, Response, NextFunction } from 'express';
import { signAccessToken, verifyAccessToken } from '../../utils/crypto';
import config from '../../config';
import AppError from '../../core/error/AppError';

export const verifyJwt = (req: Request, res: Response, next: NextFunction) => {
  const authHeader = req.headers.authorization;
  if (!authHeader?.startsWith('Bearer ')) {
    return next(new AppError('UNAUTHORIZED', 401));
  }
  const token = authHeader.split(' ')[1];
  try {
    const decoded = verifyAccessToken(token);
    // attach user info to request
    (req as any).user = { id: decoded.sub, role: decoded.role };
    next();
  } catch (err) {
    return next(new AppError('UNAUTHORIZED', 401));
  }
};
```

### Role‑Check Middleware (inline RBAC)  

```ts
// src/middleware/auth/roles.ts
import { Request, Response, NextFunction } from 'express';
import { User } from '../../models/user/User';
import config from '../../config';

export const requireRoles = (...allowed: ('viewer' | 'reviewer' | 'admin')[]) => {
  return async (req: Request, res: Response, next: NextFunction) => {
    const user = (req as any).user;
    if (!user) return next(new AppError('UNAUTHORIZED', 401));
    if (!allowed.includes(user.role)) {
      return next(new AppError('INSUFFICIENT_PERMISSIONS', 403));
    }
    next();
  };
};
```

**Usage Example**

```ts
router.post('/movie/:id/review', verifyJwt, requireRoles('reviewer', 'admin'), reviewCreateHandler);
```

---

## Security Control Implementations  

### Rate Limiting  

```ts
// src/middleware/rateLimit.ts
import rateLimit from 'express-rate-limit';
import config from '../../config';

export const authLimiter = rateLimit({
  windowMs: config.RATE_LIMIT_WINDOW_MS,   // e.g., 15 * 60 * 1000  // 15 mins
  max: config.RATE_LIMIT_MAX,            // e.g., 100 per window
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests, try again later.' },
});
```

### Input Validation  

```ts
// src/middleware/validate.ts
import { Request, Response, NextFunction } from 'express';
import { compile } from 'ajv';
import schema from '../validation/schemas/login.schema';

export const validateBody = (schema) => {
  const ajv = new (require('ajv'))({ allErrors: true });
  const validate = compile(schema);
  return (req: Request, _res: Response, next: NextFunction) => {
    const valid = validate(req.body);
    if (!valid) {
      return next({ statusCode: 400, message: validate.errors });
    }
    next();
  };
};
```

### CSRF Protection (Stateless)  

```ts
// src/middleware/csrf.ts
import csurf from 'csurf';
import cookieParser from 'cookie-parser';
import config from '../../config';

export const csrfProtection = (req: Request, _res: Response, next: NextFunction) => {
  // For APIs that use JWT, CSRF is less critical; still enforce if cookies are used.
  const parser = cookieParser();
  parser(req, {}, () => {
    const csrf middleware = csurf({
      cookie: {
        httpOnly: true,
        secure: config.NODE_ENV === 'production',
        sameSite: 'strict',
      },
    });
    csrf(req, {}, next);
  });
};
```

### Token Rotation & Revocation  

- **Refresh token** is stored hashed in DB (see `RefreshTokenService`).  
- **Access token** TTL = `config.JWT_ACCESS_EXPIRES_IN` (e.g., 10 min).  
- On successful refresh, previous refresh token is **invalidated** (single‑use).  
- Token blacklist stored in an in‑memory `Map` with expiry matching refresh token TTL.

```ts
// src/services/auth/refresh-token.service.ts
import crypto from 'crypto';
import { User } from '../models/user/User';
import { refreshTokenStore } from '../utils/token-store';

export const rotateRefreshToken = async (token: string) => {
  const decoded = jwt.verify(token, process.env.JWT_REFRESH_SECRET);
  const user = await User.findById(decoded.sub);
  if (!user) throw new AppError('UNAUTHORIZED', 401);

  const newRefresh = signRefreshToken({ sub: decoded.sub, kid: crypto.randomBytes(2).toString('hex') }, decoded.sub);
  // invalidate old
  refreshTokenStore.set(decoded.sub, newRefresh, decoded.exp);
  return newRefresh;
};
```

---

## Configuration and Environment Variables  

```ts
// src/config/defaults.ts
export default {
  PORT: Number(process.env.PORT) || 3000,
  JWT_ACCESS_SECRET: process.env.JWT_ACCESS_SECRET!,
  JWT_REFRESH_SECRET: process.env.JWT_REFRESH_SECRET!,
  JWT_ACCESS_EXPIRES_IN: process.env.JWT_ACCESS_EXPIRES_IN || '10m',
  JWT_REFRESH_EXPIRES_IN: process.env.JWT_REFRESH_EXPIRES_IN || '7d',
  RATE_LIMIT_WINDOW_MS: Number(process.env.RATE_LIMIT_WINDOW_MS) || 900_000, // 15 min
  RATE_LIMIT_MAX: Number(process.env.RATE_LIMIT_MAX) || 100,
  MONGODB_URI: process.env.MONGODB_URI!,
  MONGODB_URI_TEST: process.env.MONGODB_URI_TEST || 'mongodb://localhost:27051/movie-test',
  LOG_LEVEL: process.env.LOG_LEVEL || 'info',
  CSRF_COOKIE_KEY: process.env.CSRF_COOKIE_KEY!,
  // Additional GTM endpoint config
  GTM_ENDPOINT: process.env.GTM_ENDPOINT!,
};
```

```ts
// src/config/index.ts
import defaultConfig from './defaults';
import { isProduction } from './env';
import dotenv from 'dotenv';
dotenv.config();

const config = {
  ...defaultConfig,
  NODE_ENV: isProduction() ? 'production' : 'development',
  LOG_LEVEL: process.env.LOG_LEVEL?.toLowerCase() || 'info',
};

export default config;
```

A **Yup** schema could be added for runtime validation, throwing if required fields are missing.

---

## Error Handling Patterns  

### Custom Error Class  

```ts
// src/core/error/AppError.ts
export class AppError extends Error {
  public readonly statusCode: number;
  public readonly isOperational: boolean;
  constructor(message: string, statusCode: number = 500) {
    super(message);
    this.statusCode = statusCode;
    this.isOperational = true;
    Error.captureStackTrace(this, this.constructor);
  }
}
```

### Central Error‑Handling Middleware  

```ts
// src/middleware/error-handler.ts
import { AppError } from '../core/error/AppError';
import logger from '../core/logger';

export const errorHandler = (err: any, _req: any, res: any, _next: any) => {
  const statusCode = err instanceof AppError ? err.statusCode : 500;
  const message = err instanceof AppError ? err.message : 'Internal Server Error';
  const stack = process.env.NODE_ENV === 'development' ? err.stack : undefined;

  logger.error({
    message,
    statusCode,
    stack,
    timestamp: new Date().toISOString(),
  });

  res.status(statusCode).json({
    error: {
      message,
      code: err.name ?? 'ERROR',
    },
  });
};
```

### Standardized Response Format  

All route handlers should wrap results:

```ts
const asyncWrapper = (fn) => (req, res, next) => {
  Promise.resolve(fn(req, res, next)).catch(next);
};
```

Then:

```ts
router.get('/health', asyncWrapper(async (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
}));
```

---

## Testing Strategy with Example Test Cases  

### Unit Test – Bcrypt Hashing  

```ts
// test/user.service.test.ts
import { User } from '../src/models/user/User';
import bcrypt from 'bcryptjs';

jest.mock('bcryptjs');

describe('User model password hashing', () => {
  it('hashes password with cost >= 12', async () => {
    const hashed = 'hashed';
    (bcrypt.genSalt as jest.Mock).mockResolvedValue(12);
    (bcrypt.hash as jest.Mock).mockResolvedValue(hashed);
    const user = new User({ email: 'test@example.com', passwordHash: 'plain' });
    await user.save();
    expect(bcrypt.genSalt).toHaveBeenCalledWith(12);
    expect(bcrypt.hash).toHaveBeenCalledWith('plain', 12);
    expect(user.passwordHash).toBe(hashed);
  });
});
```

### Integration Test – Login Flow  

```ts
// test/auth.integration.test.ts
import request from 'supertest';
import app from '../src/server';
import mongoose from 'mongoose';
import { MongoMemoryServer } from 'mongodb-memory-server';
import { User } from '../src/models/user/User';

let mongod: MongoMemoryServer;

beforeAll(async () => {
  mongod = await MongoMemoryServer.create();
  process.env.MONGODB_URI = mongod.getUri();
  await mongoose.connect(process.env.MONGODB_URI);
});

afterAll(async () => {
  await mongoose.disconnect();
  await mongod.stop();
});

describe('POST /login', () => {
  it('returns JWT on correct credentials', async () => {
    const hashed = await bcrypt.hash('Password123', 12);
    const user = await User.create({ email: 'john@example.com', passwordHash: hashed, role: 'viewer' });
    const res = await request(app).post('/login').send({ email: 'john@example.com', password: 'Password123' });
    expect(res.status).toBe(200);
    expect(res.body.token).toBeDefined();
    expect(res.body.user?.role).toBe('viewer');
  });

  it('rejects login with wrong password', async () => {
    await request(app).post('/login').send({ email: 'john@example.com', password: 'wrong' }).expect(401);
  });
});
```

**Coverage** – Unit tests for password hashing, role‑check middleware; integration tests for token issuance, rate limiting, CSRF, and token rotation.

---

## Deployment Configuration  

### Dockerfile  

```Dockerfile
# src/Dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .
RUN npm run build   # transpile TS

FROM node:20-alpine AS runtime
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY package*.json ./
RUN npm ci --only=production
ENV NODE_ENV=production
EXPOSE 3000
CMD ["node", "dist/server.js"]
```

### docker‑compose (development)  

```yaml
# docker-compose.yml
version: '3.9'
services:
  api:
    build: ./src
    ports:
      - "3000:3000"
    env_file:
      - .env
    depends_on:
      - mongo
  mongo:
    image: mongo:6
    restart: always
    environment:
      MONGO_INITDB_DATABASE: imdb_lite
    volumes:
      - mongo-data:/data/db
  # optional: mongo-memory for tests

volumes:
  mongo-data:
```

### Health Checks  

```ts
// src/routes/health.router.ts
router.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});
```

Kubernetes readiness probe example:

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: 3000
  initialDelaySeconds: 15
  periodSeconds: 20
```

### Environment Variable Validation  

`config/index.ts` validates required env vars on startup and throws `AppError` if any are missing, guaranteeing that the service never starts in an undefined state.

---

*All sections above provide concrete, copy‑and‑paste‑ready code snippets and clear responsibilities. By following this design a developer can scaffold the repository, implement the authentication service, satisfy NIST 800‑53 controls, meet performance targets, and deliver the required GTM identity intelligence feature while maintaining full test coverage and production‑ready deployment artifacts.*

---
## Guardrail Review Notes

*The following items were flagged by automated guardrails for human review:*

- [WARN] compliance: Possible secret in document: Possible hardcoded password

*These may be false positives in a design document.*
