"""Per-member Google Calendar OAuth, free/busy, and shared team Meet invitations."""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import replace
from datetime import date, datetime, time as daytime, timedelta, timezone
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column
import app as core
import google_integration as google
import team

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

SCOPES = (*google.SCOPES, "openid", "email")

class MemberGoogleConnection(core.Base):
    __tablename__ = "team_google_connections"
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), primary_key=True)
    google_subject: Mapped[str] = mapped_column(String(256), unique=True)
    verified_email: Mapped[str] = mapped_column(String(320), unique=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    calendar_id: Mapped[str] = mapped_column(String(320), default="primary")
    connected_utc: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

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
    updated_utc: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

def team_settings() -> google.Settings | None:
    settings = core.google_settings()
    return replace(settings, redirect_uri=core.PUBLIC_BASE_URL + "/team/google/callback") if settings else None

def connection(db: Session, member_id: int) -> MemberGoogleConnection | None:
    return db.get(MemberGoogleConnection, member_id)

def require_connected() -> bool:
    return os.getenv("TEAM_GOOGLE_REQUIRE_CONNECTED", "false").lower().strip() == "true"

def member_busy(db: Session, member: team.Member, day: date):
    linked = connection(db, member.id)
    if not linked:
        if require_connected():
            raise HTTPException(503, "A team member has not connected Google Calendar yet.")
        return core.calendar_busy(db, day) if member.role == "owner" else []
    try:
        settings = team_settings()
        if not settings:
            raise google.GoogleIntegrationError("Calendar credentials are missing")
        token = google.access_token(settings, linked.encrypted_refresh_token)
        start = datetime.combine(day, daytime.min, tzinfo=core.HOST_TZ)
        end = datetime.combine(day + timedelta(days=1), daytime.min, tzinfo=core.HOST_TZ)
        return google.freebusy(token, linked.calendar_id, start, end)
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, "A team member's calendar availability could not be verified.") from exc

def _assignees(db: Session, booking_id: int):
    return list(db.scalars(select(team.Member).join(
        team.BookingAssignee, team.BookingAssignee.member_id == team.Member.id
    ).where(team.BookingAssignee.booking_id == booking_id).order_by(team.Member.id)).all())

def _attendees(db: Session, booking: core.Booking, members: list[team.Member], organizer_id: int):
    result, seen = [], set()
    for member in members:
        if member.id == organizer_id:
            continue
        linked = connection(db, member.id)
        email = (linked.verified_email if linked else (member.email or "")).lower().strip()
        if email and email not in seen:
            result.append({"email": email, "displayName": member.name})
            seen.add(email)
    email = (booking.guest_email or "").strip().lower()
    if email and email not in seen:
        result.append({"email": email, "displayName": booking.guest_name})
    return result

def sync_team_booking(booking_id: int) -> str:
    """One organizer creates ONE event; stable event ID permits retry after network loss."""
    with core.SessionLocal() as db:
        booking = db.get(core.Booking, booking_id)
        meta = db.get(team.TeamBooking, booking_id)
        if not booking or not meta:
            return "not_team_booking"
        members = _assignees(db, booking_id)
        if not members:
            return "no_assignees"
        link = db.get(TeamGoogleEventLink, booking_id)
        if not link:
            link = TeamGoogleEventLink(booking_id=booking_id, event_id="zq"+secrets.token_hex(16))
            db.add(link)
            db.flush()
        if link.organizer_member_id is None:
            candidates = [m for m in members if m.active and connection(db, m.id)]
            if not candidates:
                link.status, link.updated_utc = "waiting_account", utcnow()
                db.commit()
                return link.status
            link.organizer_member_id = candidates[0].id
            link.organizer_google_subject = connection(db, candidates[0].id).google_subject
            db.commit()
        linked = connection(db, link.organizer_member_id)
        if not linked:
            link.status, link.updated_utc = "waiting_account", utcnow()
            db.commit()
            return link.status
        if linked.google_subject != link.organizer_google_subject:
            link.status = "account_mismatch"
            link.last_error = "Organizer changed Google accounts; reconcile the old event manually"
            link.updated_utc = utcnow()
            db.commit()
            return link.status
        try:
            settings = team_settings()
            if not settings:
                raise google.GoogleIntegrationError("Calendar credentials are missing")
            token = google.access_token(settings, linked.encrypted_refresh_token)
            if booking.status != "confirmed":
                google.delete_event(token, linked.calendar_id, link.event_id)
                link.status, link.meet_url = "deleted", None
            else:
                organizer = next(m for m in members if m.id == link.organizer_member_id)
                details = google.event_data(booking.event_type.title, organizer.name, booking.guest_name,
                    booking.guest_email, booking.start_utc, booking.end_utc, link.event_id, create_meet=True)
                details["attendees"] = _attendees(db, booking, members, organizer.id)
                digest = hashlib.sha256("|".join(sorted(
                    a["email"].lower() for a in details["attendees"]
                )).encode()).hexdigest()
                try:
                    if link.synced_start_utc == booking.start_utc and link.synced_end_utc == booking.end_utc and link.attendee_digest == digest:
                        event = google.get_event(token, linked.calendar_id, link.event_id)
                    elif link.synced_start_utc is not None:
                        update = {k:v for k,v in details.items() if k not in {"id","conferenceData"}}
                        event = google.patch_event(token, linked.calendar_id, link.event_id, update)
                    else:
                        event = google.insert_event(token, linked.calendar_id, details)
                except google.GoogleEventMissing:
                    event = google.insert_event(token, linked.calendar_id, details)
                link.meet_url = google.meet_link(event)
                link.status = "synced" if link.meet_url else "pending_meet"
                link.synced_start_utc, link.synced_end_utc = booking.start_utc, booking.end_utc
                link.attendee_digest = digest
            link.last_error = None
        except google.GoogleIntegrationError as exc:
            link.status, link.last_error = "error", str(exc)[:320]
        link.updated_utc = utcnow()
        db.commit()
        return link.status

def install(web: FastAPI) -> None:
    @web.get("/team/google", response_class=HTMLResponse)
    def status(request: Request):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            conn = connection(db, actor.id)
            others = []
            if actor.role in {"owner","admin"}:
                others = db.execute(select(team.Member.name, MemberGoogleConnection.verified_email)
                    .join(MemberGoogleConnection, MemberGoogleConnection.member_id == team.Member.id)
                    .where(team.Member.organization_id == actor.organization_id, team.Member.active.is_(True))
                    .order_by(team.Member.name)).all()
            return core.page(request, "team_google.html", identity=actor, linked=conn,
                configured=team_settings() is not None, connected_members=others,
                strict=require_connected(),
                csrf_token=request.session.get("csrf") if request.session.get("admin") else request.session.get("team_csrf"))

    @web.post("/team/google/connect")
    def connect(request: Request, token: str = Form(...)):
        core.throttle(request, "team_google_connect", limit=15, window=3600)
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            settings = team_settings()
            if settings is None:
                raise HTTPException(503, "Google OAuth must be configured before connecting accounts")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            request.session["team_google_oauth"] = {"state":state, "verifier":verifier,
                "member_id":actor.id, "created":int(time.time())}
            return RedirectResponse(google.authorization_url(settings, state, verifier, scopes=SCOPES), 303)

    @web.get("/team/google/callback")
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        saved = request.session.pop("team_google_oauth", None)
        if not isinstance(saved, dict) or not state or not hmac.compare_digest(state, saved.get("state","")):
            raise HTTPException(403, "Invalid Google connection state")
        created = int(saved.get("created",0))
        if int(time.time()) - created > 600 or int(time.time()) < created:
            raise HTTPException(403, "Google authorization expired")
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            if actor.id != saved.get("member_id"):
                raise HTTPException(403, "OAuth belongs to a different member")
            if error or not code:
                return RedirectResponse("/team/google?result=denied",303)
            settings = team_settings()
            if settings is None:
                raise HTTPException(503, "Google OAuth settings missing")
            try:
                result = google.exchange_code(settings, code, saved["verifier"])
                grants = set(result.get("scope","").split())
                if grants and not set(google.SCOPES).issubset(grants):
                    raise google.GoogleIntegrationError("Google did not grant the required Calendar permissions")
                identity = google.account_identity(result["access_token"])
                subject, email = identity["sub"], identity["email"].lower()
                collision = db.scalar(select(MemberGoogleConnection).where(
                    MemberGoogleConnection.member_id != actor.id,
                    (MemberGoogleConnection.google_subject == subject) |
                    (MemberGoogleConnection.verified_email == email)))
                if collision:
                    raise HTTPException(409, "Google account already used by another member")
                conn = connection(db, actor.id)
                refresh = result.get("refresh_token")
                if not refresh and (conn is None or conn.google_subject != subject):
                    raise google.GoogleIntegrationError("Refresh token missing; revoke access and reconnect")
                if conn is None:
                    conn = MemberGoogleConnection(member_id=actor.id, google_subject=subject,
                        verified_email=email, encrypted_refresh_token="")
                    db.add(conn)
                conn.google_subject, conn.verified_email, conn.connected_utc = subject,email,utcnow()
                if refresh:
                    conn.encrypted_refresh_token = google.encrypt_secret(refresh, settings)
                affected = db.scalars(select(TeamGoogleEventLink).join(team.BookingAssignee,
                    team.BookingAssignee.booking_id == TeamGoogleEventLink.booking_id
                ).join(core.Booking, core.Booking.id == TeamGoogleEventLink.booking_id).where(
                    team.BookingAssignee.member_id == actor.id,
                    core.Booking.status == "confirmed", core.Booking.start_utc > utcnow())).all()
                for event_link in affected:
                    event_link.status = "pending"
                    event_link.updated_utc = utcnow() - timedelta(minutes=5)
                db.commit()
            except google.GoogleIntegrationError as exc:
                raise HTTPException(502, str(exc)) from exc
        return RedirectResponse("/team/google?result=connected",303)

    @web.post("/team/google/disconnect")
    def disconnect(request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            linked = connection(db, actor.id)
            if linked:
                db.delete(linked)
                events = db.scalars(select(TeamGoogleEventLink).join(
                    team.BookingAssignee, team.BookingAssignee.booking_id == TeamGoogleEventLink.booking_id
                ).join(core.Booking, core.Booking.id == TeamGoogleEventLink.booking_id).where(
                    team.BookingAssignee.member_id == actor.id,
                    core.Booking.status == "confirmed", core.Booking.start_utc > utcnow())).all()
                for event_link in events:
                    event_link.status = "waiting_account" if event_link.organizer_member_id == actor.id else "pending"
                    event_link.updated_utc = utcnow()
                db.commit()
        return RedirectResponse("/team/google?result=disconnected",303)

    @web.post("/team/google/retry/{booking_id}")
    def retry(booking_id: int, request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request)
            team._csrf(request, token)
            meta = db.get(team.TeamBooking,booking_id)
            assigned = db.scalar(select(team.BookingAssignee.id).where(
                team.BookingAssignee.booking_id == booking_id,
                team.BookingAssignee.member_id == actor.id))
            if not meta or meta.organization_id != actor.organization_id or not (
                assigned or actor.role in {"owner","admin"}):
                raise HTTPException(404)
        sync_team_booking(booking_id)
        return RedirectResponse("/team/google",303)
