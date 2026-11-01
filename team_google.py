"""Per-member Google Calendar + one-organizer team meeting sync.

Do not reuse the legacy host OAuth grant. Every team member explicitly
connects their Google account and can revoke ZEQORO's stored authorization.
The organizer creates ONE event with all attendees, so collective meetings
share a single Meet link rather than generating independent conferences.
"""
from __future__ import annotations

import hmac
import hashlib
import os
import secrets
import time
from dataclasses import replace
from datetime import date, datetime, time as daytime, timedelta, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

import app as core
import google_integration as google
import team

UTC_NOW = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
TEAM_OAUTH_SCOPES = (*google.SCOPES, "https://www.googleapis.com/auth/calendar.calendarlist.readonly", "openid", "email")


class MemberGoogleConnection(core.Base):
    __tablename__ = "team_google_connections"
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), primary_key=True)
    google_subject: Mapped[str] = mapped_column(String(256), unique=True)
    verified_email: Mapped[str] = mapped_column(String(320), unique=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    calendar_id: Mapped[str] = mapped_column(String(320), default="primary")
    connected_utc: Mapped[datetime] = mapped_column(DateTime, default=UTC_NOW)


class TeamGoogleEventLink(core.Base):
    __tablename__ = "team_google_event_links"
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), primary_key=True)
    organizer_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"), nullable=True)
    organizer_google_subject: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attendee_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting_account")
    meet_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(320), nullable=True)
    synced_start_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_end_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_utc: Mapped[datetime] = mapped_column(DateTime, default=UTC_NOW)


def team_settings() -> google.Settings | None:
    settings = core.google_settings()
    if settings is None:
        return None
    return replace(settings, redirect_uri=core.PUBLIC_BASE_URL + "/team/google/callback")


def connection(db: Session, member_id: int) -> MemberGoogleConnection | None:
    return db.get(MemberGoogleConnection, member_id)


def require_connected() -> bool:
    """Opt-in rollout gate; default off preserves existing team links during setup."""
    return os.getenv("TEAM_GOOGLE_REQUIRE_CONNECTED", "false").strip().lower() == "true"


def member_busy(db: Session, member: team.Member, day: date) -> list[tuple[datetime, datetime]]:
    linked = connection(db, member.id)
    if not linked:
        if require_connected():
            raise HTTPException(503, "A team member has not connected Google Calendar yet.")
        # Legacy host calendar only applies to the original owner, never others.
        return core.calendar_busy(db, day) if member.role == "owner" else []
    try:
        settings = team_settings()
        if settings is None:
            raise google.GoogleIntegrationError("Calendar credentials are missing")
        # v1.0 reads ALL calendars selected by this member, not only primary.
        import team_calendar
        return team_calendar.multi_busy(db, member.id, day)
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, "A team member's calendar availability could not be verified.") from exc


def _assignees(db: Session, booking_id: int) -> list[team.Member]:
    rows = db.scalars(select(team.Member).join(team.BookingAssignee, team.BookingAssignee.member_id == team.Member.id)
                      .where(team.BookingAssignee.booking_id == booking_id).order_by(team.Member.id)).all()
    return list(rows)


def _attendees(db: Session, booking: core.Booking, members: list[team.Member], organizer_id: int) -> list[dict]:
    entries: list[dict] = []
    used: set[str] = set()
    for member in members:
        if member.id == organizer_id:
            continue
        linked = connection(db, member.id)
        email = (linked.verified_email if linked else member.email or "").strip().lower()
        if email and email not in used:
            entries.append({"email": email, "displayName": member.name})
            used.add(email)
    guest = (booking.guest_email or "").strip().lower()
    if guest and guest not in used:
        entries.append({"email": guest, "displayName": booking.guest_name})
    return entries


def sync_team_booking(booking_id: int) -> str:
    """Persist one organizer/event id per team booking; never silently rotate hosts.

    Eventual sync, not a distributed transaction. Retry can recover after the
    Google API succeeds but local commit fails because event IDs are stable.
    """
    with core.SessionLocal() as db:
        meta = db.get(team.TeamBooking, booking_id)
        booking = db.get(core.Booking, booking_id)
        if not meta or not booking:
            return "not_team_booking"
        members = _assignees(db, booking_id)
        if not members:
            return "no_assignees"
        link = db.get(TeamGoogleEventLink, booking_id)
        if not link:
            link = TeamGoogleEventLink(booking_id=booking_id, event_id="zq" + secrets.token_hex(16), status="waiting_account")
            db.add(link)
            db.flush()
        if link.organizer_member_id is None:
            import team_calendar
            preferred_id = team_calendar.organizer_choice(db, booking, members)
            connected = [m for m in members if m.active and connection(db, m.id)]
            if preferred_id is not None:
                connected = [m for m in connected if m.id == preferred_id]
            if not connected:
                link.status = "waiting_account"
                link.last_error = "Selected organizer must connect Google" if preferred_id is not None else None
                link.updated_utc = UTC_NOW()
                db.commit()
                return link.status
            link.organizer_member_id = connected[0].id
            link.organizer_google_subject = connection(db, connected[0].id).google_subject
            db.commit()  # Durable organizer binding before any Google write.
        linked = connection(db, link.organizer_member_id)
        if linked is None:
            link.status = "waiting_account"
            link.updated_utc = UTC_NOW()
            db.commit()
            return link.status
        if linked.google_subject != link.organizer_google_subject:
            # A different Google account must NOT silently take over an old
            # organizer's event (would leave orphaned invitations/calendar data).
            link.status = "account_mismatch"
            link.last_error = "Organizer changed Google accounts; reconcile the old event manually"
            link.updated_utc = UTC_NOW()
            db.commit()
            return link.status
        try:
            settings = team_settings()
            if not settings:
                raise google.GoogleIntegrationError("Calendar credentials are missing")
            token = google.access_token(settings, linked.encrypted_refresh_token)
            if booking.status != "confirmed":
                google.delete_event(token, linked.calendar_id, link.event_id)
                link.status = "deleted"
                link.meet_url = None
            else:
                organizer = next(m for m in members if m.id == link.organizer_member_id)
                details = google.event_data(booking.event_type.title, organizer.name, booking.guest_name,
                                            booking.guest_email, booking.start_utc, booking.end_utc,
                                            link.event_id, create_meet=True)
                details["attendees"] = _attendees(db, booking, members, organizer.id)
                digest = hashlib.sha256("|".join(sorted(x["email"].lower() for x in details["attendees"])).encode()).hexdigest()
                if (link.synced_start_utc == booking.start_utc and link.synced_end_utc == booking.end_utc
                        and link.attendee_digest == digest):
                    try:
                        event = google.get_event(token, linked.calendar_id, link.event_id)
                    except google.GoogleEventMissing:
                        event = google.insert_event(token, linked.calendar_id, details)
                elif link.synced_start_utc is not None:
                    update = {k: v for k, v in details.items() if k not in {"id", "conferenceData"}}
                    try:
                        event = google.patch_event(token, linked.calendar_id, link.event_id, update)
                    except google.GoogleEventMissing:
                        event = google.insert_event(token, linked.calendar_id, details)
                else:
                    event = google.insert_event(token, linked.calendar_id, details)
                link.meet_url = google.meet_link(event)
                link.status = "synced" if link.meet_url else "pending_meet"
                link.synced_start_utc = booking.start_utc
                link.synced_end_utc = booking.end_utc
                link.attendee_digest = digest
            link.last_error = None
        except google.GoogleIntegrationError as exc:
            link.status = "error"
            link.last_error = str(exc)[:320]
        link.updated_utc = UTC_NOW()
        db.commit()
        return link.status


def _oauth_state(request: Request, actor: team.Member) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    request.session["team_google_oauth"] = {
        "state": state, "verifier": verifier,
        "member_id": actor.id, "created": int(time.time()),
    }
    return state, verifier


def install(web: FastAPI) -> None:
    @web.get("/team/google", response_class=HTMLResponse)
    def status(request: Request):
        with core.SessionLocal() as db:
            member = team._need_member(db, request)
            linked = connection(db, member.id)
            configured = team_settings() is not None
            others = db.execute(select(team.Member.name, MemberGoogleConnection.verified_email)
                .join(MemberGoogleConnection, MemberGoogleConnection.member_id == team.Member.id)
                .where(team.Member.organization_id == member.organization_id, team.Member.active.is_(True))
                .order_by(team.Member.name)).all() if member.role in {"admin", "owner"} else []
            import team_calendar
            calendars = db.scalars(select(team_calendar.MemberCalendar)
                .where(team_calendar.MemberCalendar.member_id == member.id).order_by(team_calendar.MemberCalendar.display_name)).all()
            watches = db.scalars(select(team_calendar.CalendarWatch).where(
                team_calendar.CalendarWatch.member_id == member.id,
                team_calendar.CalendarWatch.active.is_(True))).all()
            collective = db.scalars(select(team.GroupEvent).where(
                team.GroupEvent.organization_id == member.organization_id,
                team.GroupEvent.mode == "collective")).all() if member.role in {"owner", "admin"} else []
            choices = {g.group_id: g.member_id for g in db.scalars(select(team_calendar.GroupOrganizer))}
            attendees = {g.id: db.scalars(select(team.Member).join(team.GroupParticipant,
                team.GroupParticipant.member_id == team.Member.id).where(
                team.GroupParticipant.group_event_id == g.id, team.Member.active.is_(True))).all() for g in collective}
            return core.page(request, "team_google.html", identity=member, linked=linked, configured=configured,
                connected_members=others, csrf_token=request.session.get("csrf") if request.session.get("admin") else request.session.get("team_csrf"),
                strict=require_connected(), calendars=calendars, watches=watches,
                collective=collective, organizer_choices=choices, collective_members=attendees,
                push_https=core.PUBLIC_BASE_URL.startswith("https://"))

    @web.post("/team/google/connect")
    def connect(request: Request, token: str = Form(...)):
        core.throttle(request, "team_google_connect", limit=15, window=3600)
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            settings = team_settings()
            if settings is None:
                raise HTTPException(503, "Google OAuth must be configured before connecting accounts")
            state, verifier = _oauth_state(request, actor)
            url = google.authorization_url(settings, state, verifier, scopes=TEAM_OAUTH_SCOPES)
            return RedirectResponse(url, 303)

    @web.get("/team/google/callback")
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        saved = request.session.pop("team_google_oauth", None)  # single use, even on a failure
        if not isinstance(saved, dict) or not state or not hmac.compare_digest(state, saved.get("state", "")):
            raise HTTPException(403, "Invalid Google connection state")
        if int(time.time()) - int(saved.get("created", 0)) > 600 or int(time.time()) < int(saved.get("created", 0)):
            raise HTTPException(403, "Google connection has expired")
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            if actor.id != saved.get("member_id"):
                raise HTTPException(403, "Google connection belongs to a different team member")
            if error or not code:
                return RedirectResponse("/team/google?result=denied", 303)
            settings = team_settings()
            if settings is None:
                raise HTTPException(503, "Google OAuth settings missing")
            try:
                data = google.exchange_code(settings, code, saved["verifier"])
                grants = set(data.get("scope", "").split())
                if grants and not set(google.SCOPES).issubset(grants):
                    raise google.GoogleIntegrationError("Google did not grant the required Calendar permissions")
                identity = google.account_identity(data["access_token"])
                subject = identity["sub"]
                verified_email = identity["email"].lower()
                existing_subject = db.scalar(select(MemberGoogleConnection).where(
                    MemberGoogleConnection.google_subject == subject, MemberGoogleConnection.member_id != actor.id))
                existing_email = db.scalar(select(MemberGoogleConnection).where(
                    MemberGoogleConnection.verified_email == verified_email, MemberGoogleConnection.member_id != actor.id))
                if existing_subject or existing_email:
                    raise HTTPException(409, "This Google account is already linked to another member")
                existing = connection(db, actor.id)
                refresh = data.get("refresh_token")
                if not refresh and (not existing or existing.google_subject != subject):
                    raise google.GoogleIntegrationError("Google did not return a refresh token; revoke app access and reconnect")
                if existing is None:
                    existing = MemberGoogleConnection(member_id=actor.id, google_subject=subject,
                                verified_email=verified_email, encrypted_refresh_token="")
                    db.add(existing)
                existing.google_subject = subject
                existing.verified_email = verified_email
                if refresh:
                    existing.encrypted_refresh_token = google.encrypt_secret(refresh, settings)
                existing.connected_utc = UTC_NOW()
                # Refresh future invitations when an attendee connects a new account.
                affected = db.scalars(select(TeamGoogleEventLink).join(
                    team.BookingAssignee, team.BookingAssignee.booking_id == TeamGoogleEventLink.booking_id)
                    .join(core.Booking, core.Booking.id == TeamGoogleEventLink.booking_id)
                    .where(team.BookingAssignee.member_id == actor.id,
                           core.Booking.status == "confirmed", core.Booking.start_utc > UTC_NOW())).all()
                for event_link in affected:
                    event_link.status = "pending"
                    event_link.updated_utc = UTC_NOW() - timedelta(minutes=5)
                db.commit()
            except google.GoogleIntegrationError as exc:
                # Don't expose provider response tokens or internal state to the user.
                raise HTTPException(502, str(exc)) from exc
        return RedirectResponse("/team/google?result=connected", 303)

    @web.post("/team/google/disconnect")
    def disconnect(request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            linked = connection(db, actor.id)
            if linked:
                import team_calendar
                for watch in db.scalars(select(team_calendar.CalendarWatch).where(
                        team_calendar.CalendarWatch.member_id == actor.id,
                        team_calendar.CalendarWatch.active.is_(True))).all():
                    watch.active = False
                db.delete(linked)
                # Mark future invitations for reconciliation; the original host's
                # event is not removed automatically when their grant is revoked.
                affected = db.scalars(select(TeamGoogleEventLink).join(
                    team.BookingAssignee, team.BookingAssignee.booking_id == TeamGoogleEventLink.booking_id)
                    .join(core.Booking, core.Booking.id == TeamGoogleEventLink.booking_id)
                    .where(team.BookingAssignee.member_id == actor.id,
                           core.Booking.status == "confirmed", core.Booking.start_utc > UTC_NOW())).all()
                for event_link in affected:
                    event_link.status = "waiting_account" if event_link.organizer_member_id == actor.id else "pending"
                    event_link.updated_utc = UTC_NOW()
                db.commit()
        # Existing events can't be edited/deleted until the same account reconnects.
        return RedirectResponse("/team/google?result=disconnected", 303)

    @web.post("/team/google/retry/{booking_id}")
    def retry(booking_id: int, request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            meta = db.get(team.TeamBooking, booking_id)
            allowed = db.scalar(select(team.BookingAssignee.id).where(team.BookingAssignee.booking_id == booking_id,
                           team.BookingAssignee.member_id == actor.id)) is not None
            if not meta or meta.organization_id != actor.organization_id or not (allowed or actor.role in {"owner", "admin"}):
                raise HTTPException(404)
        sync_team_booking(booking_id)
        return RedirectResponse("/team/google", 303)
