# ZEQORO v0.9: Individual Google Calendar & shared Google Meet

Each active member signs into /team (owner uses /admin/login) and connects *their own* Google account at /team/google.

## Google Cloud configuration

Enable the Google Calendar API and configure an OAuth **Web application** with BOTH exact callback URLs:
- http://localhost:8000/admin/google/callback (existing owner integration)
- http://localhost:8000/team/google/callback (new per-member integration)

For production, use matching HTTPS URLs. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI=<PUBLIC_BASE_URL>/admin/google/callback, GOOGLE_TOKEN_ENCRYPTION_KEY (stable Fernet key), PUBLIC_BASE_URL in a secret .env file. Do not commit credentials, destroy volumes or change database engines without migration. Google scopes: calendar.events, calendar.freebusy, openid and email. Each member consents independently. Google may require verification before public release.

## Booking behavior

- Personal bookings: check the member's external free/busy and invite guest on their Google Calendar.
- Round Robin: choose an eligible free member; create **one** Google Calendar event.
- Collective: require everyone free; choose one Google-connected organizer; invite the other assigned members and guest to one event with **one** unique Meet room. Google sends attendee notifications with sendUpdates=all.
- Reschedule and cancel: update/delete the organizer's event; attendees receive Google event updates.
- Credentials: encrypted refresh tokens keyed by member, OAuth session state bound to the authenticated member and limited to 10 minutes, PKCE. Never reuse the old host's grant for other members.
- Google organizer account subject and event ID are pinned; reconnecting a **different** account causes account_mismatch rather than duplicating calendar events.
- A member may disconnect their connection; previously created Google events persist until manually removed.
- TEAM_GOOGLE_REQUIRE_CONNECTED=false preserves existing self-hosted team bookings when members have not linked Google; those bookings show waiting_account. Set true **after** teammates connect to enforce Google authorization for scheduling. Round Robin skips unconnected candidates in strict mode.
- If Google free/busy fails for a connected account, return HTTP 503 (fail closed).
- Sync is eventually consistent; if provider creation fails local booking is still confirmed, and sync shows error or pending_meet. Team dashboard offers Retry sync; the production Redis worker retries with leases and backoff. The standard SQLite Compose lacks a worker.
- Booking availability is not a Google-side atomic hold. External edits between slot checking and event insertion could conflict. Invitations and Meet provisioning can be delayed and require attendee acceptance.
- Current beta supports one organization, one host timezone, and only the connected primary calendar per member. No Google push webhooks, full two-way import or multi-tenant security audit yet.

Test the complete flow using Google Cloud test users and sandbox calendars before production deployment.

Official docs:
https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query
https://developers.google.com/workspace/calendar/api/auth
