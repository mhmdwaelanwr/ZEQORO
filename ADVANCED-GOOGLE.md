# ZEQORO v1.0 — Advanced Google Calendar Team Sync

**Status:** self-hosted beta. API tests use mock Google responses. Real multi-account OAuth and webhook delivery require an HTTPS deployment and consent-screen configuration. No account is connected automatically.

## Capabilities

- Each member connects their *own* Google account at `/team/google` using the existing OAuth+PKCE flow. **Reconnect** after deploying v1.0 to approve `calendar.calendarlist.readonly` (used to list selectable calendars) in addition to events/freebusy and OIDC email.
- Choose 1–10 readable calendars per member. All selected calendars are checked for Free/Busy; any Google API error causes a **503 fail-closed** result instead of advertising an unverified free slot. Existing v0.9 connections without a calendar catalog continue using `primary` until discovery.
- Organization admins select the **organizer for future Collective event bookings** in `/team/google`. Only active participating members can be selected. Round Robin bookings are organized by their assigned member. Already-created events retain the pinned Google organizer; changing preferences never transfers existing Meet links.
- Optional Google `events.watch` push channels for each selected calendar, registered through `/team/google`. Webhook: `POST /webhooks/google/calendar`. It verifies `X-Goog-Channel-ID`, `X-Goog-Resource-ID`, `X-Goog-Channel-Token`, and monotonic `X-Goog-Message-Number` against durable database state. Tokens are hashed, not stored in plaintext.
- Google notifications **do not include event details**. The separate worker consumes durable dirty flags using `events.list` and a persisted per-watch `syncToken` with pagination; Google `410 GONE` triggers a new full sync. If an organizer-owned event is changed/deleted externally, it is marked `external_review`, **not overwritten automatically**.
- Watch channels have expiration dates and are renewed during worker passes (within 24 h of expiry). Redis worker leases prevent simultaneous processing of the same channel. The original polling worker remains a safety net for failed Calendar-to-Google writes.

## Setup

1. Update code to the new branch and back up your DB. This is an **additive** schema change; no existing bookings or OAuth grants are deleted.
2. In Google Cloud enable Calendar API and add member OAuth callback `${PUBLIC_BASE_URL}/team/google/callback` as well as the original host callback. Provide verified HTTPS domain/consent screen per Google's requirements.
3. Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI=${PUBLIC_BASE_URL}/admin/google/callback`, `GOOGLE_TOKEN_ENCRYPTION_KEY`, `PUBLIC_BASE_URL` in `.env`. Keep tokens/keys server-side; do not commit them.
4. For push only, deploy **public HTTPS with a reliable reverse proxy**, no hostname of localhost or private IP. Your firewall must permit Google's outbound POST to `/webhooks/google/calendar`. Point `PUBLIC_BASE_URL` to the public origin.
5. Every member reconnects if prompted, clicks **Refresh my calendars**, selects calendars, then clicks **Enable / refresh push channels**.
6. Run the existing worker from `docker-compose.production.yml` with Redis. The worker polls for dirty channels and renews watches in addition to retrying outbound sync. Channel delivery is *near real-time* (worker cadence + provider delays), not guaranteed instantaneous or exactly-once.
7. If a Google event is flagged as externally edited, review manually before using **Retry sync**. The app intentionally does not apply a potentially destructive update in either direction without a review.

## Operations & guardrails

- **Google watch channels expire**. Ensure the worker runs continuously and worker leases are available. Lost webhook notifications can be recovered by re-registering channels; the code currently does not perform time-based full reconciliation of every calendar without a watch signal.
- Google may deliver duplicate or out-of-order notifications. The callback acknowledges verified duplicates without enqueuing another sync.
- Calendar selection and organizer choices are scoped to authenticated members/organization roles and CSRF-protected. The webhook has no CSRF cookie; it relies on Google channel headers and a random channel token.
- Selected Google calendars that have Free/Busy access but not events.list/watch permissions may be usable for conflict detection while push setup fails. Check shared-calendar permissions. No Google token is exposed in the dashboard or webhook logs.
- `Base.metadata.create_all` adds new tables automatically, but **does not provide versioned migrations**. Use tested database backups and plan Alembic before production schema upgrades.
- No external provider calls have been made in tests; verify with two real Google test accounts, including cross-account invites, Meet creation, a webhook delivered from Google and cancellation.

## Tests

```bash
python -m pytest -q
python -m compileall -q app.py team.py team_google.py team_calendar.py worker.py tests
node --check static/booking.js
node --check static/site.js
```

Google documentation: [Push notifications](https://developers.google.com/workspace/calendar/api/guides/push), [Events: watch](https://developers.google.com/workspace/calendar/api/v3/reference/events/watch), [CalendarList: list](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list), [Incremental synchronization](https://developers.google.com/workspace/calendar/api/guides/sync).
