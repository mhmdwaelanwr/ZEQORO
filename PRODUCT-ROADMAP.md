# ZEQORO · Product roadmap

## Direction
Private, self-hostable scheduling with elegant guest booking, then workflows for project introductions, sponsor conversations and client follow-ups. Designed mobile-first, WCAG-aware, and respectful of guest privacy.

## v0.2 — implemented
- [x] Private rescheduling with atomic collision checks and stable booking links.
- [x] Whole-day holiday/time-off exceptions; new slots immediately blocked.
- [x] Meeting editor for title, description, duration, buffers, notice and horizon.
- [x] CSV export with spreadsheet formula injection hardening.
- [x] Accurate all-bookings upcoming count, private-page security headers.
- [x] Dashboard settings, navigation, keyboard focus and responsive styling.
- [x] Guest-local time hints while preserving the host time zone as scheduling truth.
- [x] Automated API and regression tests.

## v0.3 — secure hosted beta
- [ ] Google Calendar OAuth2 with offline token rotation, encryption at rest, least-privilege scopes and explicit user consent; free/busy check before committing.
- [ ] Google Meet creation through Google Calendar events, token revocation and sync reconciliation.
- [ ] Email queue with host/guest notifications, idempotency, cancellation and reschedule alerts.
- [ ] Reverse-proxy protection: login/booking rate limits, bot controls, CSP cleanup and CSRF across every action.
- [ ] Robust scheduling with multiple intervals/day, one-off availability overrides, timezone picker and DST transitions tests.
- [ ] PostgreSQL, transactional exclusion constraint for conflicting intervals, Alembic migrations; full backup/restore.
- [ ] Separate guest privacy notice, data retention controls and audit log.

## v0.4 — polished product
- [ ] Arabic and English across the entire interface including RTL, formatted dates, emails and accessibility.
- [ ] Public branded profiles, organizer-level routes, theming, calendar grid and pagination/filtering.
- [ ] Add/change timezone preferences, buffer visualization, list/calendar dashboard views.
- [ ] Calendar notifications, 2-way cancellation/rescheduling sync, webhooks and public API.
- [ ] Automated visual regression, accessibility tests and load tests.

## v0.5 — team/CRM/automation (requires a new architecture review)
- [ ] Accounts, roles, teams, owner isolation, round-robin and pooled collective booking.
- [ ] Sponsorship CRM: companies, contacts, stages, notes, follow-up tasks and calendar links.
- [ ] Opt-in Gmail drafts and manual approval before sending; no unsolicited automatic outreach.
- [ ] AI scheduling helper with clear limits, privacy controls and graceful no-AI fallback.
- [ ] Optional Ktor service and Compose Multiplatform client once the API contracts are stable.

## UX design constraints
- Public booking has one primary action per step; never force a guest account.
- Accessible forms with persistent labels, error feedback and keyboard support; target WCAG 2.2 AA.
- On mobile keep booking actions at least 44px high and content single column.
- Use limited accent hues, deliberate empty states and visual hierarchy in the dashboard.

## License research
- Cal.diy: MIT; code reuse may be possible with copyright/notice compliance, but the upstream project warns it's intended primarily for personal non-production use.
- OpenCalendar: AGPL-3.0; network service source provision obligations apply to derived covered code.
- Easy!Appointments: GPL-3.0; redistribution is subject to GPL terms.

Current code has been authored independently with no copied upstream source; evaluate third-party dependencies/licenses before a commercial release. See README for source links.
