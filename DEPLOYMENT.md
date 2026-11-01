# ZEQORO v0.7 — PostgreSQL, Redis and sync worker

## Architecture / scope

- `docker-compose.yml`: **unchanged SQLite personal development** setup; existing `calendar_data` volume is retained.
- `docker-compose.production.yml`: separate production-oriented Postgres 17 + private Redis 7 + FastAPI + a **single retry worker**. Neither data service publishes host ports. Web stays bound to `127.0.0.1:8000` pending TLS reverse proxy.
- Postgres appointment creation/rescheduling serializes slot validation with `SELECT ... FOR UPDATE` on singleton `scheduling_lock` row. SQLite continues `BEGIN IMMEDIATE`. Both locks protect **all event types** against local double-booking.
- Redis uses atomic Lua sorted-set sliding-window rate limits across workers. An unavailable Redis service returns HTTP 503, **not an unprotected fallback**. Client IP is read from the direct connection; only configure trusted proxy forwarding at the infrastructure level.
- Worker retries failed Google Calendar sync after 10 minutes, incomplete Meet links after 3 minutes, and pending requests after 90 seconds. It skips ended confirmed bookings, retries cancellations, and uses a Redis lease per booking to avoid duplicate concurrent worker writes. The **same stable Google event ID** is reused. This is polling, not a full durable task queue.
- `GET /health` is a liveness probe; `GET /ready` requires a working DB and (when configured) Redis.

## Setup for an EMPTY production Postgres DB

1. Make an offline backup of the SQLite database and existing `.env` (do not commit either). **There is no automatic SQLite→Postgres migration or Alembic migration in v0.7.** Existing SQLite bookings WILL NOT appear in a new Postgres database. Migrate and verify them before any cutover.
2. Create `.env` with `POSTGRES_PASSWORD` (strong random secret), `DATABASE_URL=postgresql+psycopg://zeqoro:<URL-encoded password>@db:5432/zeqoro`, `APP_ENV=production`, a random 32+ byte `APP_SECRET`, strong `ADMIN_PASSWORD`, `COOKIE_SECURE=true`, `PUBLIC_BASE_URL=https://your-domain.example`, and the Google OAuth settings if needed.
3. Add an HTTPS reverse proxy in front of `127.0.0.1:8000` (domain, TLS, request-size limit, HTTPS proxy headers and security monitoring). Do not expose :8000 publicly. Do not open DB or Redis ports.
4. Run `docker compose -f docker-compose.production.yml config` to verify configuration without disclosing the expanded output; run `docker compose -f docker-compose.production.yml up -d --build` and `docker compose -f docker-compose.production.yml ps`.
5. Run `docker compose -f docker-compose.production.yml logs -f calendar worker` and `curl -fsS http://localhost:8000/ready` locally. Test a booking, Google Free/Busy, Meet creation, cancellation, rescheduling and worker retry with a non-production Google test account.
6. Schedule Postgres backups using `pg_dump` and **test restoring them**, and back up `.env` and the Fernet encryption key securely. Avoid exposing credentials in shell history/logs. Pin/scan container images and plan automatic security updates.

**Production limitations:** `Base.metadata.create_all()` is bootstrap-only and not a migration system. There is no monitored deployment, automated backups/restores, OAuth verification, shared Google access-token cache, multi-calendar read, or zero-downtime deployments. **Do not migrate existing real bookings or expose this system as a production SaaS until these gates are met.**

## v0.9 Team Google tables
The startup ORM creates two additional tables, team_google_connections and team_google_event_links. This is additive schema setup, **not an Alembic migration**. Back up data and the encryption key; register the extra team OAuth callback. See TEAM-GOOGLE.md.
