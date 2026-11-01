# ZEQORO v0.9 — Individual Google Calendar accounts & team invitations

Team members each connect **their own** Google Calendar from `/team/google` (after signing in at `/team/login`, or `/admin/login` for the original organization owner). The legacy `/admin/integrations` grant is separate and is not used for other team members.

## Google Cloud setup

1. Enable **Google Calendar API** in your Google Cloud project and configure the Google OAuth consent screen (test users first).
2. Create an **OAuth client ID → Web application**, register both *exact* redirect URIs for your own origin:
   - `http://localhost:8000/admin/google/callback`
   - `http://localhost:8000/team/google/callback`
   - For HTTPS deployment, register both analogous HTTPS URLs, then set `PUBLIC_BASE_URL` to that origin.
3. In `.env`, set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI=<PUBLIC_BASE_URL>/admin/google/callback`, and a **stable** random `GOOGLE_TOKEN_ENCRYPTION_KEY` (Fernet). Never commit `.env` or copy secrets into issues/PRs.
4. Start Docker with `docker compose up -d --build` (or use the separately documented PostgreSQL/Redis Compose after provisioning a fresh database). Do not delete volumes or switch from SQLite to PostgreSQL without a tested migration.
5. Each team member signs into ZEQORO and visits `/team/google` → **Connect my Google Calendar** → authorizes Google. The Google account is identified via OIDC verified email and a stable Google subject ID, not a user-supplied email field.
6. Test a **personal**, **round-robin** and **collective** booking on test calendars; verify a single event, Google Meet link, calendar invitations, update on reschedule, deletion/cancellation update, and no duplicate conference after retries.

OAuth uses PKCE-S256, one-use short-lived session state bound to the authenticated member, and Google Calendar event/free-busy plus `openid email` scopes. The refresh token is encrypted using the app's Fernet key and stored **per member** in `team_google_connections`. Google OAuth must be approved for broader distribution if required by Google's sensitive-scope verification rules.

## How group meetings behave

- **Personal:** that member becomes the organizer; Google Free/Busy excludes occupied slots.
- **Round Robin:** from available members, ZEQORO chooses the member with the fewest upcoming assignments; only that member becomes organizer, with the guest invited by Google.
- **Collective:** ZEQORO requires all members to be available; it writes **one** Google event to a connected organizer's calendar, includes other assigned members and the guest in its `attendees`, and requests **one** new Google Meet conference. Google handles email invitations and attendee calendar delivery; attendee acceptance and third-party notifications cannot be guaranteed by ZEQORO.
- **Internal meeting:** when an assigned member has a Google connection, a single organizer event invites the other selected members. No fake guest email is used.
- **Reschedule or cancel:** the original organizer event is patched/deleted with `sendUpdates=all`; the same event ID and Meet room are reused where possible. The app never writes separate duplicate Meet events on every account.

The event ID, Google organizer's member ID **and Google subject** are pinned in `team_google_event_links` before provider calls. If an organizer reconnects a **different** Google account, sync reports `account_mismatch` instead of silently creating a replacement and leaving the old event behind. Resolve the old event manually; do not auto-rotate its organizer. Other attendees' changed connected emails cause attendee-list reconciliation on the next sync.

## Availability & failure semantics

- A connected member's Google busy times are checked **before** a slot is shown and **again under the booking transaction lock** at booking time. If Google Free/Busy fails, the API returns HTTP 503 (fail closed), not a falsely free slot.
- `TEAM_GOOGLE_REQUIRE_CONNECTED=false` (default) preserves existing v0.8 team bookings for members who have not authorized Google; it checks only ZEQORO hours and existing bookings for those members. Such bookings are saved as `waiting_account` until an eligible organizer connects. This is **not** a guarantee of external calendar availability.
- For organizations requiring conflict prevention against everyone's Google Calendar, set `TEAM_GOOGLE_REQUIRE_CONNECTED=true` **after everyone connects**. Personal and Collective scheduling will require connected calendars; Round Robin skips disconnected candidates if another authorized participant is available.
- Provider sync is **eventually consistent**. Local bookings remain confirmed if creating/updating Google fails. Booking confirmation and the `/team` dashboard show `waiting_account`, `pending`, `pending_meet`, `error`, `account_mismatch`, `synced`, or `deleted` as applicable.
- The admin/member can manually click **Retry sync** on the `/team` dashboard for accessible assignments. The Redis-backed worker polls incomplete team Google links with backoff and a per-booking lease. The default SQLite-only Compose does **not** include the retry worker.
- When a member disconnects, ZEQORO removes their stored authorization; existing Google events are *not* automatically revoked or deleted from Google. Subsequent changes to an organizer's event require that **same** Google account to reconnect. As with any external service, a Google event may change between Free/Busy query and event creation; there is no Google-side atomic reservation.

## Security / operations checklist

- Secure `PUBLIC_BASE_URL=https://…`, `COOKIE_SECURE=true`, long admin secrets and authentication, valid trusted reverse-proxy configuration and distinct Google test/production OAuth clients.
- Don't expose Google refresh tokens, OAuth session state, guest emails, event IDs, or private booking URLs in logs or public documentation.
- Back up the database and encryption key securely, verify restores; set up migrations before scaling to multiple organizations.
- OAuth scopes might need Google verification. Use test accounts and sandbox calendars before production rollout.
- The current product is a **single-organization** self-hosted beta: no multi-tenant isolation certification, no Google push webhook/real-time import, and all member working hours use the configured host timezone. A true multi-calendar organizer, resource calendar selection, complete OAuth revocation and end-to-end production monitoring remain future work.

References: [Google Calendar insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert), [FreeBusy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query), [OAuth scopes](https://developers.google.com/workspace/calendar/api/auth).


## v1.0 update
See [ADVANCED-GOOGLE.md](ADVANCED-GOOGLE.md) for selectable calendars, Collective organizer preference and push webhook sync. Reconnect member OAuth to grant calendar list permission.
