"""ZEQORO Teams v0.8 — opt-in workspace, personal & organization scheduling.

New relational tables only; existing booking tables remain unchanged. The legacy
host Google connection is NOT repurposed as an arbitrary team member's calendar.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, exists, func, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

import app as core


class Organization(core.Base):
    __tablename__ = "team_organizations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(70), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(400), default="")


class Member(core.Base):
    __tablename__ = "team_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("team_organizations.id"), index=True)
    slug: Mapped[str] = mapped_column(String(70), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    bio: Mapped[str] = mapped_column(String(400), default="")
    role: Mapped[str] = mapped_column(String(12), default="member")
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    invite_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    invite_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    organization: Mapped[Organization] = relationship()
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_team_member_email"),)


class MemberHours(core.Base):
    __tablename__ = "team_member_hours"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("member_id", "weekday", name="uq_team_hours_day"),)


class MemberDayOff(core.Base):
    __tablename__ = "team_member_days_off"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("member_id", "day", name="uq_team_day_off"),)


class PersonalEvent(core.Base):
    __tablename__ = "team_personal_events"
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.id"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    event_type: Mapped[core.EventType] = relationship()
    member: Mapped[Member] = relationship()


class GroupEvent(core.Base):
    __tablename__ = "team_group_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.id"), unique=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("team_organizations.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="round_robin")
    event_type: Mapped[core.EventType] = relationship()
    organization: Mapped[Organization] = relationship()


class GroupParticipant(core.Base):
    __tablename__ = "team_group_participants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_event_id: Mapped[int] = mapped_column(ForeignKey("team_group_events.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    __table_args__ = (UniqueConstraint("group_event_id", "member_id", name="uq_group_participant"),)


class TeamBooking(core.Base):
    __tablename__ = "team_booking_metadata"
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("team_organizations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))


class BookingAssignee(core.Base):
    __tablename__ = "team_booking_assignees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    member: Mapped[Member] = relationship()
    __table_args__ = (UniqueConstraint("booking_id", "member_id", name="uq_booking_assignee"),)


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def valid_slug(v: str, length: int = 60) -> str:
    v = v.strip().lower()
    if not 2 <= len(v) <= length or not _SLUG.fullmatch(v):
        raise HTTPException(400, "Use a lowercase slug (letters, digits and hyphens).")
    return v


def seed() -> None:
    """Called after legacy schema creation; safe to run repeatedly."""
    org_slug = os.getenv("TEAM_ORG_SLUG", "zeqoro-team").strip().lower()
    owner_slug = re.sub(r"[^a-z0-9-]", "-", core.ADMIN_USER.strip().lower()).strip("-")[:60] or "owner"
    if not _SLUG.fullmatch(org_slug) or len(org_slug) > 70:
        raise RuntimeError("TEAM_ORG_SLUG is invalid")
    with core.SessionLocal() as db:
        org = db.scalar(select(Organization).where(Organization.slug == org_slug))
        if org is None:
            org = Organization(slug=org_slug, name=os.getenv("TEAM_ORG_NAME", "ZEQORO Team")[:160])
            db.add(org)
            db.flush()
        owner = db.scalar(select(Member).where(Member.organization_id == org.id, Member.role == "owner"))
        if owner is None:
            slug = owner_slug
            if db.scalar(select(Member.id).where(Member.slug == slug)):
                slug = "owner-" + secrets.token_hex(3)
            owner = Member(organization_id=org.id, slug=slug, name=core.HOST_NAME, role="owner", active=True)
            db.add(owner)
            db.flush()
            _default_hours(db, owner.id)
        if not db.scalar(select(PersonalEvent.event_type_id).where(PersonalEvent.member_id == owner.id).limit(1)):
            event = core.EventType(slug=f"p-{owner.slug}-intro"[:80], title=f"Meet with {owner.name}", description="Choose a time for a personal meeting.", duration_min=30)
            db.add(event)
            db.flush()
            db.add(PersonalEvent(member_id=owner.id, event_type_id=event.id))
        if not db.scalar(select(GroupEvent.id).where(GroupEvent.organization_id == org.id).limit(1)):
            event = core.EventType(slug=f"o-{org.slug}-intro"[:80], title="Talk to our team", description="Book time with a member of our organization.", duration_min=30)
            db.add(event)
            db.flush()
            group = GroupEvent(event_type_id=event.id, organization_id=org.id, mode="round_robin")
            db.add(group)
            db.flush()
            db.add(GroupParticipant(group_event_id=group.id, member_id=owner.id))
        db.commit()


def _default_hours(db: Session, member_id: int) -> None:
    db.add_all([MemberHours(member_id=member_id, weekday=i, start_min=9*60, end_min=17*60) for i in range(5)])


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 450_000)
    return f"pbkdf2-sha256$450000${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        alg, iterations, salt, digest = encoded.split("$")
        if alg != "pbkdf2-sha256" or int(iterations) > 1_000_000:
            return False
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(value, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def _identity(db: Session, request: Request) -> Member | None:
    if request.session.get("admin"):
        return db.scalar(select(Member).where(Member.role == "owner", Member.active.is_(True)))
    member_id = request.session.get("team_member")
    if not isinstance(member_id, int):
        return None
    member = db.get(Member, member_id)
    return member if member and member.active and member.password_hash else None


def _need_member(db: Session, request: Request, roles: tuple[str, ...] | None = None) -> Member:
    member = _identity(db, request)
    if not member:
        raise HTTPException(303, headers={"Location": "/team/login"})
    if roles and member.role not in roles:
        raise HTTPException(403, "Your team role does not allow this action.")
    return member


def _csrf(request: Request, raw: str) -> None:
    session_key = "csrf" if request.session.get("admin") else "team_csrf"
    expected = request.session.get(session_key)
    if not expected or not hmac.compare_digest(str(raw), str(expected)):
        raise HTTPException(403, "Invalid CSRF token")


def _valid_member(db: Session, slug: str) -> Member | None:
    return db.scalar(select(Member).where(Member.slug == slug, Member.active.is_(True)))


def _public_personal(db: Session, member_slug: str, event_slug: str):
    member = _valid_member(db, member_slug)
    if not member:
        raise HTTPException(404, "Member not available")
    event = db.scalar(select(core.EventType).join(PersonalEvent, PersonalEvent.event_type_id == core.EventType.id)
                      .where(PersonalEvent.member_id == member.id, core.EventType.slug == f"p-{member.slug}-{event_slug}", core.EventType.is_active.is_(True)))
    if not event:
        raise HTTPException(404, "Meeting type not available")
    return member, event


def _public_group(db: Session, org_slug: str, event_slug: str):
    org = db.scalar(select(Organization).where(Organization.slug == org_slug))
    if not org:
        raise HTTPException(404, "Organization not found")
    result = db.execute(select(GroupEvent, core.EventType).join(core.EventType, GroupEvent.event_type_id == core.EventType.id)
         .where(GroupEvent.organization_id == org.id, core.EventType.slug == f"o-{org.slug}-{event_slug}", core.EventType.is_active.is_(True))).first()
    if not result:
        raise HTTPException(404, "Team meeting not available")
    group, event = result
    members = db.scalars(select(Member).join(GroupParticipant, GroupParticipant.member_id == Member.id)
                      .where(GroupParticipant.group_event_id == group.id, Member.active.is_(True), or_(Member.password_hash.is_not(None), Member.role == "owner"))).all()
    if not members:
        raise HTTPException(404, "No available team members")
    return org, group, event, members


def _bookings_for_member(db: Session, member: Member, start: datetime, end: datetime, ignore_booking_id: int | None = None) -> list[core.Booking]:
    query = select(core.Booking).join(BookingAssignee, BookingAssignee.booking_id == core.Booking.id).where(
        BookingAssignee.member_id == member.id, core.Booking.status == "confirmed",
        core.Booking.start_utc <= end, core.Booking.end_utc >= start)
    if ignore_booking_id is not None:
        query = query.where(core.Booking.id != ignore_booking_id)
    results = list(db.scalars(query).all())
    if member.role == "owner":
        # Legacy single-host bookings count against the original owner's capacity.
        results.extend(db.scalars(select(core.Booking).where(core.Booking.status == "confirmed", core.Booking.start_utc <= end,
                  core.Booking.end_utc >= start, ~exists(select(TeamBooking.booking_id).where(TeamBooking.booking_id == core.Booking.id)))).all())
    return results


def _member_free(db: Session, member: Member, event: core.EventType, start: datetime,
                 busy_bookings: list[core.Booking], blocked: bool, hours: MemberHours | None,
                 external_busy: list[tuple[datetime, datetime]]) -> bool:
    if blocked or not hours:
        return False
    minute = start.hour * 60 + start.minute
    if minute < hours.start_min or minute + event.duration_min > hours.end_min:
        return False
    a = core.utc_naive(start) - timedelta(minutes=event.buffer_before_min)
    b = core.utc_naive(start + timedelta(minutes=event.duration_min)) + timedelta(minutes=event.buffer_after_min)
    for existing in busy_bookings:
        old_start = existing.start_utc - timedelta(minutes=existing.event_type.buffer_before_min)
        old_end = existing.end_utc + timedelta(minutes=existing.event_type.buffer_after_min)
        if a < old_end and b > old_start:
            return False
    if any(a < y and b > x for x, y in external_busy):
        return False
    return True


def team_slots(db: Session, event: core.EventType, members: list[Member], day: date,
               mode: Literal["personal", "round_robin", "collective"], ignore_booking_id: int | None = None) -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    current_day = now.astimezone(core.HOST_TZ).date()
    if day < current_day or day > current_day + timedelta(days=event.max_advance_days):
        return []
    low = core.utc_naive(datetime.combine(day - timedelta(days=1), time.min, tzinfo=core.HOST_TZ))
    high = core.utc_naive(datetime.combine(day + timedelta(days=2), time.min, tzinfo=core.HOST_TZ))
    calendars = {}
    for member in members:
        hours = db.scalar(select(MemberHours).where(MemberHours.member_id == member.id, MemberHours.weekday == day.weekday()))
        blocked = db.scalar(select(MemberDayOff.id).where(MemberDayOff.member_id == member.id, MemberDayOff.day == day.isoformat())) is not None
        if member.role == "owner":
            blocked = blocked or db.scalar(select(core.BlockedDate.id).where(core.BlockedDate.day == day.isoformat())) is not None
        current = _bookings_for_member(db, member, low, high, ignore_booking_id=ignore_booking_id)
        # Only the existing owner can use the legacy single-host Google calendar connection.
        google_busy = core.calendar_busy(db, day) if member.role == "owner" else []
        calendars[member.id] = (hours, blocked, current, google_busy)
    result = []
    for minute in range(0, 24 * 60 - event.duration_min + 1, 15):
        naive = datetime.combine(day, time(minute // 60, minute % 60))
        start = naive.replace(tzinfo=core.HOST_TZ)
        end = start + timedelta(minutes=event.duration_min)
        if start.astimezone(timezone.utc).astimezone(core.HOST_TZ).replace(tzinfo=None) != naive:
            continue
        if end.astimezone(timezone.utc).astimezone(core.HOST_TZ).replace(tzinfo=None) != end.replace(tzinfo=None):
            continue
        start_aware = start.astimezone(timezone.utc)
        if start_aware < now + timedelta(hours=event.min_notice_hours) or start_aware > now + timedelta(days=event.max_advance_days):
            continue
        flags = []
        for member in members:
            hrs, blocked, busy, google_busy = calendars[member.id]
            flags.append(_member_free(db, member, event, start, busy, blocked, hrs, google_busy))
        okay = all(flags) if mode == "collective" else any(flags)
        if okay:
            result.append({"start": start.isoformat(), "label": start.strftime("%I:%M %p").lstrip("0")})
    return result


def _assign(db: Session, group: GroupEvent | None, members: list[Member], event: core.EventType,
            chosen: datetime) -> list[Member]:
    if group is None or group.mode == "collective":
        return members
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counts = dict(db.execute(select(BookingAssignee.member_id, func.count())
                  .join(core.Booking, core.Booking.id == BookingAssignee.booking_id)
                  .where(core.Booking.status == "confirmed", core.Booking.start_utc >= now)
                  .group_by(BookingAssignee.member_id)).all())
    return [min(members, key=lambda member: (counts.get(member.id, 0), member.id))]


def _selected_members(db: Session, members: list[Member], event: core.EventType, day: date,
                      iso_start: str, group: GroupEvent | None) -> list[Member]:
    # Check slot eligibility PER MEMBER inside one serializable booking transaction.
    available = []
    for member in members:
        if iso_start in {s["start"] for s in team_slots(db, event, [member], day, "personal")}:
            available.append(member)
    if group and group.mode == "collective":
        return members if len(available) == len(members) else []
    return _assign(db, group, available, event, datetime.fromisoformat(iso_start)) if available else []


def _render_booking(request: Request, db: Session, event: core.EventType, members: list[Member],
                    mode: Literal["personal", "round_robin", "collective"], host_name: str,
                    slots_url: str, action: str, back: str):
    today = datetime.now(core.HOST_TZ).date()
    first = today
    for offset in range(min(14, event.max_advance_days + 1)):
        candidate = today + timedelta(days=offset)
        if team_slots(db, event, members, candidate, mode):
            first = candidate
            break
    return core.page(request, "book.html", event=event, page_host=host_name, slots_url=slots_url,
        booking_action=action, back_url=back, today=today.isoformat(), default_day=first.isoformat(),
        max_date=(today + timedelta(days=event.max_advance_days)).isoformat(),
        meeting_label=("ALL PARTICIPANTS" if mode == "collective" else "TEAM BOOKING"))


def install(web: FastAPI) -> None:
    """Register team endpoints after existing application endpoints are defined."""
    @web.get("/team/login", response_class=HTMLResponse)
    def login(request: Request):
        if request.session.get("admin") or request.session.get("team_member"):
            return RedirectResponse("/team", 303)
        return core.page(request, "team_login.html", error="")

    @web.post("/team/login")
    def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
        core.throttle(request, "team_login", limit=8, window=900)
        with core.SessionLocal() as db:
            member = db.scalar(select(Member).where(Member.email == email.strip().lower(), Member.active.is_(True)))
            if member and password_matches(password, member.password_hash):
                request.session.clear()
                request.session["team_member"] = member.id
                request.session["team_csrf"] = secrets.token_urlsafe(32)
                return RedirectResponse("/team", 303)
        return core.page(request, "team_login.html", error="Invalid email or password.")

    @web.post("/team/logout")
    def logout(request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            _need_member(db, request)
            _csrf(request, token)
        request.session.clear()
        return RedirectResponse("/", 303)

    @web.get("/u/{member_slug}", response_class=HTMLResponse)
    def personal_profile(member_slug: str, request: Request):
        with core.SessionLocal() as db:
            member = _valid_member(db, member_slug)
            if not member:
                return core.public_error(request, "Profile not found.", 404)
            events = db.scalars(select(core.EventType).join(PersonalEvent, PersonalEvent.event_type_id == core.EventType.id)
                        .where(PersonalEvent.member_id == member.id, core.EventType.is_active.is_(True)).order_by(core.EventType.duration_min)).all()
            return core.page(request, "team_profile.html", member=member, organization=member.organization, events=events, kind="personal")

    @web.get("/o/{org_slug}", response_class=HTMLResponse)
    def org_profile(org_slug: str, request: Request):
        with core.SessionLocal() as db:
            org = db.scalar(select(Organization).where(Organization.slug == org_slug))
            if not org:
                return core.public_error(request, "Organization not found.", 404)
            events = db.scalars(select(core.EventType).join(GroupEvent, GroupEvent.event_type_id == core.EventType.id)
                       .where(GroupEvent.organization_id == org.id, core.EventType.is_active.is_(True)).order_by(core.EventType.duration_min)).all()
            members = db.scalars(select(Member).where(Member.organization_id == org.id, Member.active.is_(True), or_(Member.password_hash.is_not(None), Member.role == "owner")).order_by(Member.name)).all()
            return core.page(request, "team_profile.html", organization=org, events=events, members=members, kind="organization")

    @web.get("/u/{member_slug}/book/{event_slug}", response_class=HTMLResponse)
    def personal_booking(member_slug: str, event_slug: str, request: Request):
        with core.SessionLocal() as db:
            member, event = _public_personal(db, member_slug, event_slug)
            return _render_booking(request, db, event, [member], "personal", member.name,
                   f"/api/team/slots/u/{member_slug}/{event_slug}", f"/u/{member_slug}/book/{event_slug}", f"/u/{member_slug}")

    @web.get("/o/{org_slug}/book/{event_slug}", response_class=HTMLResponse)
    def org_booking(org_slug: str, event_slug: str, request: Request):
        with core.SessionLocal() as db:
            org, group, event, members = _public_group(db, org_slug, event_slug)
            return _render_booking(request, db, event, members, group.mode, org.name,
                   f"/api/team/slots/o/{org_slug}/{event_slug}", f"/o/{org_slug}/book/{event_slug}", f"/o/{org_slug}")

    @web.get("/api/team/slots/u/{member_slug}/{event_slug}")
    def personal_slots(member_slug: str, event_slug: str, day: str):
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(400, "Use YYYY-MM-DD") from exc
        with core.SessionLocal() as db:
            member, event = _public_personal(db, member_slug, event_slug)
            return {"slots": team_slots(db, event, [member], parsed, "personal"), "timezone": core.TZ_NAME}

    @web.get("/api/team/slots/o/{org_slug}/{event_slug}")
    def org_slots(org_slug: str, event_slug: str, day: str):
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(400, "Use YYYY-MM-DD") from exc
        with core.SessionLocal() as db:
            org, group, event, members = _public_group(db, org_slug, event_slug)
            return {"slots": team_slots(db, event, members, parsed, group.mode), "timezone": core.TZ_NAME}

    def perform_booking(request: Request, scope: str, owner_slug: str, event_slug: str, name: str, email: str, selected_raw: str, notes: str):
        core.throttle(request, "team_booking", limit=12, window=3600)
        from email_validator import validate_email, EmailNotValidError
        name, notes = name.strip(), notes.strip()
        if not 2 <= len(name) <= 160 or len(notes) > 2000:
            return core.public_error(request, "Please enter a valid name and a note under 2000 characters.")
        try:
            email = validate_email(email.strip(), check_deliverability=False).normalized
            selected, day = core.parse_selected_start(selected_raw)
        except (EmailNotValidError, ValueError):
            return core.public_error(request, "Please choose a valid slot and email address.")
        with core.SessionLocal() as db:
            core.write_serialize(db)
            if scope == "u":
                member, event = _public_personal(db, owner_slug, event_slug)
                org_id, members, group = member.organization_id, [member], None
            else:
                org, group, event, members = _public_group(db, owner_slug, event_slug)
                org_id = org.id
            chosen = _selected_members(db, members, event, day, selected.isoformat(), group)
            if not chosen:
                db.rollback()
                return core.public_error(request, "That slot is no longer available. Please choose another.", 409)
            booking = core.Booking(event_type_id=event.id, guest_name=name, guest_email=email, notes=notes,
                     start_utc=core.utc_naive(selected), end_utc=core.utc_naive(selected + timedelta(minutes=event.duration_min)),
                     status="confirmed", cancel_token=secrets.token_urlsafe(32))
            db.add(booking)
            db.flush()
            db.add(TeamBooking(booking_id=booking.id, organization_id=org_id,
                    kind="personal" if scope == "u" else group.mode))
            db.add_all([BookingAssignee(booking_id=booking.id, member_id=m.id) for m in chosen])
            db.commit()
            return RedirectResponse(f"/confirmed/{booking.cancel_token}", 303)

    @web.post("/u/{member_slug}/book/{event_slug}")
    def personal_book(member_slug: str, event_slug: str, request: Request, guest_name: str = Form(...), guest_email: str = Form(...), start: str = Form(...), notes: str = Form("")):
        return perform_booking(request, "u", member_slug, event_slug, guest_name, guest_email, start, notes)

    @web.post("/o/{org_slug}/book/{event_slug}")
    def org_book(org_slug: str, event_slug: str, request: Request, guest_name: str = Form(...), guest_email: str = Form(...), start: str = Form(...), notes: str = Form("")):
        return perform_booking(request, "o", org_slug, event_slug, guest_name, guest_email, start, notes)

    @web.get("/team", response_class=HTMLResponse)
    def dashboard(request: Request):
        with core.SessionLocal() as db:
            member = _need_member(db, request)
            org = member.organization
            members = db.scalars(select(Member).where(Member.organization_id == org.id).order_by(Member.id)).all()
            group_events = db.scalars(select(GroupEvent).where(GroupEvent.organization_id == org.id)).all()
            personal_events = db.scalars(select(PersonalEvent).where(PersonalEvent.member_id == member.id)).all()
            query = (select(core.Booking, BookingAssignee.member_id)
                    .join(TeamBooking, TeamBooking.booking_id == core.Booking.id)
                    .join(BookingAssignee, BookingAssignee.booking_id == core.Booking.id)
                    .where(TeamBooking.organization_id == org.id))
            if member.role == "member":
                query = query.where(BookingAssignee.member_id == member.id)
            rows = db.execute(query.order_by(core.Booking.start_utc.desc()).limit(150)).all()
            bookings = list({b.id: b for b, _ in rows}.values())
            assigned = {b.id: [a.member.name for a in db.scalars(select(BookingAssignee).where(BookingAssignee.booking_id == b.id)).all()] for b in bookings}
            today = datetime.now(core.HOST_TZ).date()
            next_week = [{"date": (today + timedelta(days=i)).isoformat(), "label": (today + timedelta(days=i)).strftime("%a %d")}
                         for i in range(7)]
            day_bookings = {d["date"]: [] for d in next_week}
            for b in bookings:
                day_label = b.start_utc.replace(tzinfo=timezone.utc).astimezone(core.HOST_TZ).date().isoformat()
                if day_label in day_bookings and b.status == "confirmed":
                    day_bookings[day_label].append(b)
            blocks = db.scalars(select(MemberDayOff).where(MemberDayOff.member_id == member.id).order_by(MemberDayOff.day)).all()
            hours = {h.weekday: h for h in db.scalars(select(MemberHours).where(MemberHours.member_id == member.id)).all()}
            return core.page(request, "team_dashboard.html", identity=member, organization=org, members=members,
                   group_events=group_events, personal_events=personal_events, bookings=bookings,
                   assignments=assigned, csrf_token=request.session.get("csrf") if request.session.get("admin") else request.session.get("team_csrf"),
                   days=core.DAYS, hours=hours, days_off=blocks, today=today.isoformat(), next_week=next_week,
                   day_bookings=day_bookings, is_admin=member.role in ("owner", "admin"))

    @web.post("/team/members/invite")
    def invite(request: Request, token: str = Form(...), name: str = Form(...), slug: str = Form(...), email: str = Form(...), role: str = Form("member")):
        from email_validator import validate_email, EmailNotValidError
        with core.SessionLocal() as db:
            actor = _need_member(db, request, ("owner", "admin"))
            _csrf(request, token)
            slug, name = valid_slug(slug, 60), name.strip()
            if not 2 <= len(name) <= 160 or role not in {"member", "admin"}:
                raise HTTPException(400, "Invalid member")
            try:
                email = validate_email(email, check_deliverability=False).normalized.lower()
            except EmailNotValidError as exc:
                raise HTTPException(400, "Invalid email") from exc
            if db.scalar(select(Member.id).where(or_(Member.slug == slug, (Member.organization_id == actor.organization_id) & (Member.email == email)))):
                raise HTTPException(409, "Member slug or email already exists")
            invite_token = secrets.token_urlsafe(32)
            member = Member(organization_id=actor.organization_id, slug=slug, name=name, email=email,
                     role=role, active=True, invite_hash=hashlib.sha256(invite_token.encode()).hexdigest(),
                     invite_expires=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=72))
            db.add(member)
            db.flush()
            _default_hours(db, member.id)
            db.commit()
            # Display the token only once. It is never stored in plaintext.
            return core.page(request, "team_invite_created.html", invite_url=f"{core.PUBLIC_BASE_URL}/team/invite/{invite_token}", display_name=name)

    @web.get("/team/invite/{invite_token}", response_class=HTMLResponse)
    def invite_page(invite_token: str, request: Request):
        with core.SessionLocal() as db:
            member = _invite_member(db, invite_token)
            if not member:
                return core.public_error(request, "Invitation expired or invalid.", 404)
            return core.page(request, "team_accept.html", display_name=member.name, invite_token=invite_token, error="")

    @web.post("/team/invite/{invite_token}")
    def accept_invite(invite_token: str, request: Request, password: str = Form(...), confirm_password: str = Form(...)):
        core.throttle(request, "accept_invitation", limit=12, window=3600)
        with core.SessionLocal() as db:
            member = _invite_member(db, invite_token)
            if not member:
                raise HTTPException(404, "Invalid invitation")
            if not (12 <= len(password) <= 256) or password != confirm_password:
                return core.page(request, "team_accept.html", display_name=member.name, invite_token=invite_token, error="Use a matching password of at least 12 characters.")
            member.password_hash = password_hash(password)
            member.invite_hash = None
            member.invite_expires = None
            db.commit()
            request.session.clear()
            request.session["team_member"] = member.id
            request.session["team_csrf"] = secrets.token_urlsafe(32)
            return RedirectResponse("/team", 303)

    @web.post("/team/events/personal")
    def create_personal(request: Request, token: str = Form(...), slug: str = Form(...), title: str = Form(...), duration_min: int = Form(30)):
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, token)
            slug = valid_slug(slug, 45)
            title = title.strip()
            if not 2 <= len(title) <= 160 or duration_min not in {15, 30, 45, 60, 90, 120}:
                raise HTTPException(400, "Invalid event settings")
            full_slug = f"p-{actor.slug}-{slug}"
            if len(full_slug) > 80:
                raise HTTPException(400, "Personal booking link is too long")
            if db.scalar(select(core.EventType.id).where(core.EventType.slug == full_slug)):
                raise HTTPException(409, "Event link already exists")
            event = core.EventType(slug=full_slug, title=title, duration_min=duration_min)
            db.add(event)
            db.flush()
            db.add(PersonalEvent(event_type_id=event.id, member_id=actor.id))
            db.commit()
            return RedirectResponse("/team#personal-events", 303)

    @web.post("/team/events/organization")
    async def create_group(request: Request):
        form = await request.form()
        with core.SessionLocal() as db:
            actor = _need_member(db, request, ("owner", "admin"))
            _csrf(request, str(form.get("token", "")))
            slug = valid_slug(str(form.get("slug", "")), 40)
            title = str(form.get("title", "")).strip()
            mode = str(form.get("mode", ""))
            try:
                duration = int(str(form.get("duration_min", "")))
                ids = {int(x) for x in form.getlist("member_ids")}
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, "Invalid members or duration") from exc
            if not 2 <= len(title) <= 160 or mode not in {"collective", "round_robin"} or duration not in {15, 30, 45, 60, 90, 120} or not ids:
                raise HTTPException(400, "Invalid group meeting settings")
            allowed = db.scalars(select(Member).where(Member.id.in_(ids), Member.organization_id == actor.organization_id, Member.active.is_(True), or_(Member.password_hash.is_not(None), Member.role == "owner"))).all()
            if len(allowed) != len(ids):
                raise HTTPException(403, "Only active members of your organization may join a meeting")
            full_slug = f"o-{actor.organization.slug}-{slug}"
            if len(full_slug) > 80 or db.scalar(select(core.EventType.id).where(core.EventType.slug == full_slug)):
                raise HTTPException(409, "Meeting URL already exists or is too long")
            event = core.EventType(slug=full_slug, title=title, duration_min=duration)
            db.add(event)
            db.flush()
            group = GroupEvent(organization_id=actor.organization_id, event_type_id=event.id, mode=mode)
            db.add(group)
            db.flush()
            db.add_all([GroupParticipant(group_event_id=group.id, member_id=member.id) for member in allowed])
            db.commit()
            return RedirectResponse("/team#group-events", 303)

    @web.post("/team/availability")
    async def update_availability(request: Request):
        form = await request.form()
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, str(form.get("token", "")))
            values = []
            for i in range(7):
                if form.get(f"enabled_{i}"):
                    try:
                        start_min = core.parse_hhmm(str(form.get(f"start_{i}", "")))
                        end_min = core.parse_hhmm(str(form.get(f"end_{i}", "")))
                    except ValueError as exc:
                        raise HTTPException(400, "Invalid hours") from exc
                    if start_min >= end_min:
                        raise HTTPException(400, "End time must be after start time")
                    values.append(MemberHours(member_id=actor.id, weekday=i, start_min=start_min, end_min=end_min))
            db.query(MemberHours).filter(MemberHours.member_id == actor.id).delete(synchronize_session=False)
            db.add_all(values)
            db.commit()
        return RedirectResponse("/team#my-availability", 303)

    @web.post("/team/days-off")
    def day_off(request: Request, token: str = Form(...), day: str = Form(...)):
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, token)
            try:
                parsed = date.fromisoformat(day)
            except ValueError as exc:
                raise HTTPException(400, "Invalid date") from exc
            if parsed < datetime.now(core.HOST_TZ).date() or parsed > datetime.now(core.HOST_TZ).date() + timedelta(days=730):
                raise HTTPException(400, "Date must be within the next two years")
            if not db.scalar(select(MemberDayOff.id).where(MemberDayOff.member_id == actor.id, MemberDayOff.day == day)):
                db.add(MemberDayOff(member_id=actor.id, day=day))
                db.commit()
        return RedirectResponse("/team#my-availability", 303)

    @web.post("/team/days-off/{block_id}/remove")
    def remove_day_off(block_id: int, request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, token)
            block = db.get(MemberDayOff, block_id)
            if not block or block.member_id != actor.id:
                raise HTTPException(404)
            db.delete(block)
            db.commit()
        return RedirectResponse("/team#my-availability", 303)

    @web.post("/team/bookings/manual")
    async def create_manual_booking(request: Request):
        form = await request.form()
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, str(form.get("token", "")))
            title = str(form.get("title", "")).strip()
            start_raw = str(form.get("start", ""))
            notes = str(form.get("notes", "")).strip()
            try:
                duration = int(str(form.get("duration_min", "30")))
                ids = {int(i) for i in form.getlist("member_ids")}
                naive = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, "Use a valid time, duration and selected members") from exc
            if not ids:
                ids = {actor.id}
            if actor.role not in ("admin", "owner") and ids != {actor.id}:
                raise HTTPException(403, "Members can only add meetings to their own calendars")
            if not 2 <= len(title) <= 160 or len(notes) > 2000 or duration not in {15, 30, 45, 60, 90, 120}:
                raise HTTPException(400, "Invalid meeting title or duration")
            members = db.scalars(select(Member).where(Member.id.in_(ids), Member.organization_id == actor.organization_id,
                     Member.active.is_(True), or_(Member.password_hash.is_not(None), Member.role == "owner"))).all()
            if len(members) != len(ids):
                raise HTTPException(403, "Choose valid members of this organization")
            aware = naive.replace(tzinfo=core.HOST_TZ)
            if aware.astimezone(timezone.utc).astimezone(core.HOST_TZ).replace(tzinfo=None) != naive:
                raise HTTPException(400, "This local time does not exist in the configured timezone")
            core.write_serialize(db)
            # Create an inactive internal event type, never displayed as a public booking link.
            event = core.EventType(slug="internal-" + secrets.token_hex(16), title=title, duration_min=duration,
                     min_notice_hours=0, max_advance_days=365, buffer_before_min=0, buffer_after_min=10, is_active=False)
            valid = {s["start"] for s in team_slots(db, event, members, naive.date(), "collective")}
            if aware.isoformat() not in valid:
                db.rollback()
                raise HTTPException(409, "At least one member is unavailable at that time")
            db.add(event)
            db.flush()
            booking = core.Booking(event_type_id=event.id, guest_name=title, guest_email="", notes=notes,
                     start_utc=core.utc_naive(aware), end_utc=core.utc_naive(aware + timedelta(minutes=duration)),
                     status="confirmed", cancel_token=secrets.token_urlsafe(32))
            db.add(booking)
            db.flush()
            db.add(TeamBooking(booking_id=booking.id, organization_id=actor.organization_id, kind="internal"))
            db.add_all([BookingAssignee(booking_id=booking.id, member_id=m.id) for m in members])
            db.commit()
        return RedirectResponse("/team#team-bookings", 303)

    @web.post("/team/profile")
    def update_profile(request: Request, token: str = Form(...), name: str = Form(...), bio: str = Form("")):
        name, bio = name.strip(), bio.strip()
        if not 2 <= len(name) <= 160 or len(bio) > 400:
            raise HTTPException(400, "Invalid profile")
        with core.SessionLocal() as db:
            actor = _need_member(db, request)
            _csrf(request, token)
            actor.name, actor.bio = name, bio
            db.commit()
        return RedirectResponse("/team#profile", 303)


def _invite_member(db: Session, token: str) -> Member | None:
    if len(token) > 200:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    member = db.scalar(select(Member).where(Member.invite_hash == digest, Member.active.is_(True)))
    if not member or member.password_hash or not member.invite_expires or member.invite_expires < datetime.now(timezone.utc).replace(tzinfo=None):
        return None
    return member


def reschedule_slots(db: Session, booking: core.Booking, day: date) -> list[dict[str, str]]:
    """Keep the original assignees when a private team booking is moved."""
    meta = db.get(TeamBooking, booking.id)
    if not meta:
        raise HTTPException(404, "Not a team booking")
    members = [a.member for a in db.scalars(select(BookingAssignee).where(BookingAssignee.booking_id == booking.id)).all()]
    if not members or not all(m.active for m in members):
        return []
    return team_slots(db, booking.event_type, members, day, "collective", ignore_booking_id=booking.id)
