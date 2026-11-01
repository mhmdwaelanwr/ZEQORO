# Team Scheduling v0.8 — operator and reviewer guide

## What is implemented

This adds a **single seeded organization workspace** (default `/o/zeqoro-team`) and personal member pages (`/u/{slug}`). It is an incremental self-hosted team beta, **not multi-tenant SaaS**.

- Owner logs in using the existing `/admin/login` credentials, then visits `/team`.
- Owner/admin creates one-time invitation links for a member or another admin. Invites are hashed in the database, expire after 72 hours, and are displayed only once. Share using a trusted private channel. **Sending invitation emails is not yet automated.**
- Invitees accept with a new password (PBKDF2-SHA256, salted), then sign in at `/team/login`.
- Members can edit their display name, bio, weekly hours, days off, and personal meeting types. Members only see bookings assigned to them in the team dashboard.
- Owners/admins see organization-wide team bookings and the combined seven-day calendar, invite members, and create organization meeting links.
- **Round Robin:** choose the available participant with the fewest upcoming team assignments, with a stable tie-break. Members can receive simultaneous meetings only if they are *different* people.
- **Collective:** all participants must be available before a slot can be booked; all are assigned to one booking.
- Manual internal appointments can be saved directly from the workspace for one or more members, after validating their hours and conflicts. Members can schedule only themselves; admins can choose any active team member. Existing assignments are displayed in the seven-day calendar.
- A guest booking creates a ZEQORO confirmation/cancellation link. Rescheduling validates the *original assigned members* and keeps assignment links unchanged.

### URLs

| URL | Purpose |
|---|---|
| `/team` | Team administration / member dashboard (authenticated) |
| `/team/login` | Sign in as a member; owner uses existing admin login |
| `/o/zeqoro-team` | Public organization profile |
| `/u/{member-slug}` | Public personal profile |
| `/o/zeqoro-team/book/{event}` | Public group event booking |
| `/u/{member-slug}/book/{event}` | Public personal booking |

## Data safety and upgrade strategy

- All new organization, member, availability, event association and assignment structures live in **new** database tables. Existing `bookings`, `event_types`, Google connection and their tokens are not dropped or rewritten. Startup `create_all` creates new tables and seeds one owner with demo personal and team links if absent.
- Existing SQLite `docker-compose.yml` remains unchanged. Continue to use the existing Compose volume and avoid `down -v`.
- **This is not a tested SQLite → PostgreSQL migration.** Do not switch `DATABASE_URL` without an export/restore rehearsal, schema validation, and backups. `create_all` is not an Alembic migration strategy.
- v0.7 `write_serialize` remains the locking primitive on SQLite and PostgreSQL when confirming and rescheduling team bookings or manually creating appointments.
- `TEAM_ORG_SLUG` and `ADMIN_USER` should be treated as **stable identifiers** once used. Only one owner per app is fully supported in this beta.

## Google Calendar / Meet boundaries

- v0.6's Google OAuth connection belongs to the original **legacy host**, not every team member. It is used as an additional busy-time check for the owner only.
- **Team booking events do not currently create/update Google Calendar events or automatic Google Meet links.** Do not treat the legacy token as permission to write onto other members' calendars. Google OAuth for each team user, attendee invitations and Google Meet for collective events are separate planned features.
- A Google failure while checking owner availability prevents new slots rather than permitting double-bookings. When Google is not connected, only ZEQORO records and working hours count as busy.

## Roles and access

| Ability | Owner | Admin | Member | Guest |
|---|---|---|---|---|
| Organization dashboard | All team bookings | All team bookings | Assigned bookings only | No |
| Invite members/admins | Yes | Yes | No | No |
| Create organization meeting links | Yes | Yes | No | No |
| Manage own profile/hours/personal links | Yes | Yes | Yes | No |
| Add internal meeting for multiple people | Yes | Yes | Self only | No |
| Public booking with valid link | Yes | Yes | Yes | Yes |

All authenticated write forms require session-bound CSRF tokens. Guest booking/cancellation/reschedule endpoints use opaque bearer links and rate limits; do not post private booking URLs in public channels. Team invites are one-time bearer credentials and expire. Session cookies require `COOKIE_SECURE=true` and HTTPS in production.

## Known limitations before production

This release does **not** include multi-org self-signup or organization provisioning, member-initiated Google OAuth, real-time invite email dispatch, granular manager permissions beyond admin/member, calendar push notifications, sync reconciliation across member calendars, advanced timezone-per-member scheduling, externally shareable calendar feeds, or billing. A production rollout still needs auditing, Alembic migrations, monitored background jobs, backup/restore drills, GDPR/privacy compliance and a true Google Cloud end-to-end OAuth test.

### Test locally

```bash
python -m pytest -q
python -m compileall -q app.py team.py tests
node --check static/booking.js
```

Tests cover public booking, roles/CSRF/invitations, Round Robin vs Collective, per-member time off, manual scheduling, rescheduling, and legacy booking compatibility. PostgreSQL/Redis integration checks from v0.7 remain in CI.
