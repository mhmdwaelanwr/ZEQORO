# ZEQORO — UI design system v0.5

The **Figma source of truth** is [ZEQORO Product Experience v0.3](https://www.figma.com/design/i9mN6o8dcSrayygjYBMVkf). The dashboard and booking flow are translated into semantic, responsive Jinja templates. `static/unified.css` extends the tokens to the rest of the website.

## Core foundation

| Token | Value | Purpose |
|---|---|---|
| `--z-bg` | `#0B1020` | Canvas and page background |
| `--z-panel` | `#111A2F` | Cards and main panels |
| `--z-surface` | `#152039` | Elevated areas and side panels |
| `--z-stroke` | `#25314A` | Structural borders and separators |
| `--z-accent` | `#755BFF` | Primary controls, selections |
| `--z-mint` | `#A5FCDB` | Focus and informative highlights |
| `--z-text` | `#F9FAFF` | Primary copy |
| `--z-muted` | `#9CAAC3` | Secondary copy |

Typography: Inter with platform fallback, a scale anchored in 13–15px body and 22–75px headings. Layout uses ~8px spacing multiples, responsive cards, 44–48px or larger controls, visible focus rings, and a reduced-motion media query.

## Shared layout and routes

- `/` — public landing, real meeting types and availability links
- `/how-it-works` — process guide and FAQ with explicit integration limitations
- `/book/{slug}` — public Figma booking interface, backend-validated slots and guest form
- `/reschedule/{token}` — same scheduling components as booking, existing guest identity preserved
- `/confirmed/{token}` — private appointment details and .ics download
- `/cancel/{token}` — cancel-or-reschedule decision screen
- `/admin/login` — host sign-in
- `/admin` — host dashboard and management forms
- `/book/invalid` and other application errors — consistent error state

The admin dashboard has an independent sidebar (while sharing fonts, surfaces, semantic colors and controls with public pages). Guests never see admin-only controls.

## Component guidelines

- **Primary button**: `primary-btn`, purple fill; use for the next primary action.
- **Secondary button**: `secondary-btn`, dark panel and stroke; use for alternatives.
- **Text input**: `text-input`; requires a visible associated `label`.
- **Information card**: panels with solid borders and radii, no faux depth.
- **Date/times**: tile and slot buttons have pressed and disabled states with keyboard-accessible labels.
- **State page**: shared `state-page`, `state-card`, success/error icon and actions.
- **Privacy**: pages containing a booking token are noindex and no-store; guest management links should never be posted publicly.

## Quality gate

1. `python -m pytest -q` — backend/UI regression coverage.
2. `node --check static/booking.js && node --check static/site.js` — JavaScript syntax.
3. Browser visual QA at 1440px and 390px: eight screens with no horizontal overflow or browser runtime errors; the booking slots are mocked for offline visual verification, not for functional production testing.
4. Confirm `.env` secrets and SQLite volume persistence before local Docker deployment.

**Scope**: this is a visual, navigation and interaction consistency release. It does not imply Google Calendar OAuth, public cloud durability, SaaS team accounts, or payment integration.
