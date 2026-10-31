# Product roadmap (ZEQORO)

## Vision
A personally owned booking/calendar application that can grow into a client relationship and sponsorship workflow hub. Keep the public booking experience minimal, with a separate private workspace.

## Immediate foundation (v0.1 included)
- Single user / host, unlimited meeting types, shared non-overlapping availability.
- Weekly schedule, minimum notice, booking buffers, future booking limits.
- Public booking link, private confirmation/cancellation, calendar .ics export.
- Secure-enough local admin, state persisted in SQLite, Docker Compose, SMTP optional.

## Next iteration: shipping to invitees
1. OAuth consent flow for Google Calendar read (free/busy) + event write; use least-privilege scopes and encrypted token persistence.
2. Google Meet creation, reschedule, appointment cancellation email delivery and queue.
3. Day-specific overrides and multiple daily shifts, time zone selector and automatic guest local conversion.
4. Arabic/English UI and Arabic calendar-friendly formatting, custom public domain and branding.
5. Rate limiting, privacy policy, secure reverse proxy, offsite backups, logs and monitoring.

## Differentiator: sponsor relationship workflows
- Each booked sponsor conversation links to a contact + company record.
- Meeting summary and follow-up checklist after call.
- Pending/confirmed/replied sponsor stages and notes.
- Gmail integration for opt-in follow-up drafts and reminders.
- Export records as CSV; explicit approval before sending outreach emails.

## Stack notes
The prototype uses FastAPI because it keeps the first runnable release tiny. A long-lived implementation can move to Ktor for the backend, PostgreSQL for relational storage, and Compose Multiplatform for mobile/admin applications, while keeping lightweight web booking pages friendly to SEO and accessibility. Define a versioned JSON API before moving language/runtime so the UI is not coupled to template rendering.

## Licensing
Built independently; do not copy the source code, trade dress, logo or branding of Kalendar.work. Verify third-party licenses and the intended license for this project before publicly distributing source.
