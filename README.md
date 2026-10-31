# ZEQORO · Personal scheduling, independently built

ZEQORO is a **self-hostable single-host booking application** implemented independently with FastAPI, Jinja, SQLAlchemy and SQLite. Inspired by the scheduling **product category**, not a code fork or visual copy of any commercial service.

## v0.2 features

- Public booking page with multiple meeting types, live available slots, no required guest account.
- Host-local timezone with device-local time hints, DST-aware dates and times.
- Conflict prevention across all meeting types, configurable buffers, notice and advance windows; SQLite single-writer transaction serialization.
- Private confirmation, `.ics` invitation, **reschedule**, and cancellation links.
- Admin dashboard: meeting creation, activation/hiding, **editing scheduling rules**, weekly hours, **whole-day blocks** (holidays/time off), CSV export and booking cancellation.
- Session-protected admin actions with CSRF tokens; private-page no-cache/no-index and referrer protection.
- Optional SMTP guest confirmation/update email.
- Responsive visual design, keyboard focus and reduced-motion styles.
- Eight automated API and regression tests; GitHub Actions CI.

## Quick start (Docker)

```bash
cp .env.example .env
# Set APP_SECRET to a securely generated random value (32+ characters).
# Set ADMIN_PASSWORD to a unique strong password.
docker compose up --build -d
# Older Compose versions: docker-compose up --build -d
```

Public page: `http://localhost:8000`. Admin: `http://localhost:8000/admin/login`.

Compose binds **127.0.0.1 only**: LAN/internet access is intentionally disabled by default. Do not simply open port 8000 to the internet.

### Non-Docker Python 3.11+

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Remove DATABASE_URL from .env (or set to an appropriate local SQLite path).
uvicorn app:app --reload
```

### Production prerequisites (not yet met)

Before public deployment, configure reverse-proxy TLS, `APP_ENV=production`, a HTTPS `PUBLIC_BASE_URL`, `COOKIE_SECURE=true`, strong secrets, persistent database backups, SMTP delivery, rate limiting at the reverse proxy, monitoring, privacy policy, vulnerability/dependency scanning, and operational recovery. This starter is **not production-ready SaaS**.

## Booking rules

- Each day supports one contiguous working-hours interval and 15-minute slot starts.
- Admin meeting settings apply immediately to new bookings. Existing bookings retain stored start/end times; their private links remain stable.
- Blocking a date closes **new** slots but does not cancel any meetings already confirmed. Cancel or contact existing attendees separately.
- A reschedule locks the database before rechecking availability, excludes its own current slot, preserves its private token, and saves the new interval atomically.
- The admin CSV contains all bookings (including cancelled); untrusted guest text is escaped to reduce spreadsheet formula injection.
- Private booking URLs are bearer capabilities. Anyone holding a URL can manage that booking; never share the link publicly.
- No Google Calendar / Outlook free-busy integration yet. Only bookings in ZEQORO are considered busy.
- Admin bookings table shows latest 100 records. Export contains all records; headline upcoming count is computed from the complete database.

## Run the tests

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## Open-source research and attribution

We examined, but **did not copy or integrate**, the source code of these booking products:

- [Cal.diy](https://github.com/calcom/cal.diy): MIT, community-maintained personal self-hosting; its maintainers discourage production use.
- [OpenCalendar](https://github.com/kudretyilmazz/opencalendar): AGPL-3.0, scheduling/teams/webhooks workflows.
- [Easy!Appointments](https://github.com/alextselegidis/easyappointments): GPL-3.0, provider and appointment management.

Reusing source from these projects requires complying with their specific license obligations, and copying branded assets/trademarks is not permitted. This repository's ownership and distribution/license choices should be deliberately established before wider reuse; **no third-party source code has been copied into this v0.2 change**.

See [PRODUCT-ROADMAP.md](PRODUCT-ROADMAP.md) for staged future work.
