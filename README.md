# ZEQORO — self-hosted personal booking (MVP)

A **new independent implementation** of a personal scheduling tool, inspired by the *category* of products such as Calendly and Kalendar.work. This project does not include their code, assets or branding.

## Features implemented

- Public host landing page with multiple bookable meeting types.
- Live available slots in the **host's** IANA timezone (`HOST_TZ`).
- Monday–Sunday configurable weekly working hours (one continuous block per day).
- 15-minute slot resolution, meeting duration, minimum notice, booking horizon and buffers.
- Conflict prevention **across all meeting types**, including concurrency-safe SQLite write locks.
- Guest booking, confirmation, private cancellation link and `.ics` calendar download.
- Protected admin login, meeting type creation and activation/hiding, booking cancellations.
- Optional confirmation email via SMTP; no email sent unless configured.
- SQLite persistence in a Docker volume, responsive frontend.

### Run with Docker Compose

```bash
cp .env.example .env
# Edit .env: at a minimum change APP_SECRET and ADMIN_PASSWORD.
docker compose up --build -d
# If your Docker uses the older command: docker-compose up --build -d
```

Visit <http://localhost:8000> and admin <http://localhost:8000/admin/login>.

**Important:** Compose binds only to `127.0.0.1`, so the site is local-only by default. For public deployment use a TLS reverse proxy (Caddy/Traefik/Nginx), set `APP_ENV=production`, `PUBLIC_BASE_URL=https://your-domain`, `COOKIE_SECURE=true`, and replace both credentials with strong random values. Do not just expose port 8000 to the Internet. Make database backups. Use a real transactional email service for email delivery.

### Run without Docker (Python 3.11+)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Change DATABASE_URL to sqlite:///./data/calendar.db or remove DATABASE_URL from .env.
# Change secrets in .env.
uvicorn app:app --reload
```

### Future roadmap

**v0.2**: Google Calendar OAuth (read busy slots and write bookings with explicit user consent), Google Meet links, cancellation confirmation emails, rescheduling, more than one time block per day, day-off exceptions, multilingual Arabic/English UI.

**v0.3**: Postgres-backed transactions + unique interval constraint, staff/team accounts, round-robin, custom domain, tenant isolation, bot/rate limits, audit logs, backup and deployment automation.

**v0.4**: Gmail follow-up, CRM/contact notes, AI scheduling assistant, public API, integrations with webhooks/MCP.

### Scope and limitations

- **Personal single-host beta**, not a fully secure production SaaS. Requires further hardening before public launch (rate-limiting, monitoring, privacy policy, account recovery, email delivery, backups, security review).
- The host timezone is fixed at the server configuration and times are shown in that timezone; the guest's device timezone is displayed as a hint. *Automatic guest-zone time display is not yet implemented.*
- There is no Google/Outlook calendar sync yet. Only bookings made in this app block times.
- One working-time interval per weekday. No per-date holidays or overrides yet.
- The admin sees the most recent 100 bookings; the upcoming counter is limited to this set.
- No webhooks, payments, Zoom, Google Meet auto-generation or authentication for guests.
- Cancellation links are bearer capabilities: anyone with the URL can view/cancel that booking; keep them private.
- SMTP errors are logged without disrupting a committed booking. SMTP is not set up by default.
- When changing credentials, restart the app. When changing the session signing key, old admin sessions expire.

### Tests

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
