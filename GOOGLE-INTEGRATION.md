# ZEQORO — Google Calendar and Meet integration (v0.6 beta)

A new, independent FastAPI implementation based on Google's public REST APIs. **Do not put client secrets, encryption keys or refresh tokens in GitHub or the browser.** A host must explicitly connect Google; connecting a ChatGPT/Google Calendar app does not connect this self-hosted ZEQORO installation.

## Capabilities

- Google OAuth 2.0 **web-server flow**, state validation, PKCE-S256 and offline refresh tokens; authorization limited to `calendar.events` and `calendar.freebusy` scopes.
- The refresh token is encrypted at rest using a **Fernet key provided by the operator**. Short-lived access tokens are held in memory. No credentials are sent to guest browsers.
- Public slots check external Google Free/Busy ranges with *host-local* date boundaries and meeting buffers. When connected, an API or credential failure returns **503** and refuses to expose unverified slots (fail closed).
- New bookings create a Google Calendar event with a unique, deterministic-to-the-booking saved event ID and **one new Google Meet conference** requested with `conferenceDataVersion=1`. Guests are added as event attendees with Google's email notifications enabled.
- Guest rescheduling updates the same Google event, preserving its Meet conference; cancellation deletes the linked Google event with attendee updates.
- Sync jobs persist state (`pending`, `pending_meet`, `synced`, `error`, `deleted`), retry using stable provider event IDs, surface errors to the host and never invent a Google Meet link. A Meet URL is only displayed when Google actually returns one.
- Admin-only Integrations screen offers connect, disconnect, retry and bounded reconciliation (25 bookings per request). **Disconnection does not delete existing calendar events**.

## Local Docker setup

1. In [Google Cloud Console](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com), create/select a project and enable **Google Calendar API**.
2. Configure the **OAuth consent screen** and create an OAuth **Web application** client. Add `http://localhost:8000/admin/google/callback` as an authorized redirect URI. Google restricts test users and verification requirements; add your own Google account to the app's test users if applicable.
3. Copy `.env.example` to `.env`, set unique `APP_SECRET`, strong `ADMIN_PASSWORD`, and then set:
   ```dotenv
   GOOGLE_CLIENT_ID=your-google-oauth-web-client-id
   GOOGLE_CLIENT_SECRET=your-google-oauth-web-client-secret
   GOOGLE_REDIRECT_URI=http://localhost:8000/admin/google/callback
   GOOGLE_TOKEN_ENCRYPTION_KEY=generated-fernet-key
   PUBLIC_BASE_URL=http://localhost:8000
   ```
   Generate a Fernet key *once*:
   ```bash
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```
   Back up the key securely; changing it makes stored tokens unreadable.
4. `docker-compose up -d --build` and open `http://localhost:8000/admin/integrations` after logging in as host.
5. Click **Connect Google Calendar** and authorize access. Only the host/admin needs a Google account; guests do not.
6. Create a **test booking**, then confirm that (a) the Google event exists, (b) its Meet link shows in ZEQORO when ready, and (c) an external Google event hides overlapping ZEQORO slots. Test cancellation and rescheduling.

**Production:** set `APP_ENV=production`, HTTPS `PUBLIC_BASE_URL`, `COOKIE_SECURE=true`, a secure database deployment, and exact HTTPS OAuth redirect URI registered in Google Cloud. Keep `.env` and the Fernet key off the repository. Give each environment distinct keys and OAuth credentials. Use Redis-backed rate limits, PostgreSQL transactions, monitoring, scheduled reconciliation and durable backups before public launch. The current Docker volume is persistent on a single host, not a disaster recovery plan.

## Behavior and important limits

- This is **single-host, single-connected-Google-account** integration. A team/multi-tenant model is not implemented yet.
- Google events added/updated externally affect availability but **are not imported** as local ZEQORO booking records. Other linked Google calendars are not queried yet.
- Google Meet creation may be **asynchronous**. The host can retry an item in `pending_meet` from Integrations; do not assume a link exists immediately.
- A confirmed ZEQORO booking remains in the local database if Google event creation fails; the confirmation clearly says Google sync is pending. This is eventual consistency, **not an atomic distributed transaction** across Google Calendar and ZEQORO.
- A Google event can be changed in the external account between Free/Busy check and booking creation. There is no Google-side atomic slot hold; near-concurrent external edits can conflict. Network/credential errors fail closed, but this does not remove the race.
- Existing events persist after disconnect. Reconnection to a different Google account may recreate ZEQORO-generated events on the new calendar. Clean up the old account's events manually if needed.
- OAuth scopes may trigger Google's verification requirements before broad public distribution. Do not publish public access while the app is still unverified.
- Rate limiting remains **in-process** and 1-worker. Operational tasks such as token revocation workflows, staff calendars, backups, job queue and push notifications remain future work.

## API references

- [OAuth 2.0 web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Calendar FreeBusy query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)
- [Google Calendar create events](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [Google Calendar insert events](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)
- [Google Calendar event IDs](https://developers.google.com/workspace/calendar/api/v3/reference/events)
- [Calendar OAuth scopes](https://developers.google.com/workspace/calendar/api/auth)

## Independent open-source research references

| Reference | Relevant feature direction | Important note |
| --- | --- | --- |
| [Cal.com](https://github.com/calcom/cal.com) | Workflows, scheduling rules, calendar apps, teams | Review the exact license of files before borrowing; do not copy code or UI. |
| [Easy!Appointments](https://github.com/alextselegidis/easyappointments) | Real calendar sync, team calendars, appointment lifecycle | GPL-family: use as feature inspiration only unless complying with license. |
| [OpenCalendar](https://github.com/kudretyilmazz/opencalendar) | Independent scheduling, availability workflows | AGPL-3.0: use as feature inspiration only unless complying with license. |

No application code, logos or illustrations from those repositories are copied into ZEQORO.
