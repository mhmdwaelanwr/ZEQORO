"""ZEQORO v1.0: member calendar selection, organizer preference and safe Google push.

All schema changes are additive; v0.9 connections and private booking URLs remain valid.
Google notifications are change hints, NEVER a trusted event payload. Durable dirty
flags are processed by the worker through incremental events.list sync tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import date, datetime, time as clock_time, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

import app as core
import google_integration as google
import team

LOG = logging.getLogger("zeqoro.calendar.push")
UTC_NOW = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
MAX_CALENDARS = 10
MAX_SYNC_PAGES = 20


class MemberCalendar(core.Base):
    """Catalog of the member's Google calendars; selection controls Free/Busy."""
    __tablename__ = "team_google_calendars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    calendar_id: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    readable: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("member_id", "calendar_id", name="uq_team_google_calendar_member"),)


class GroupOrganizer(core.Base):
    """Future bookings of a collective group use this explicitly chosen member."""
    __tablename__ = "team_google_group_organizers"
    group_id: Mapped[int] = mapped_column(ForeignKey("team_group_events.id"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))


class CalendarWatch(core.Base):
    __tablename__ = "team_google_calendar_watches"
    channel_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    calendar_id: Mapped[str] = mapped_column(String(320))
    resource_id: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_utc: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_number: Mapped[int] = mapped_column(BigInteger, default=0)
    last_push_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_delta_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(320), nullable=True)


def _settings_token(db: Session, member_id: int) -> str:
    import team_google
    connection = team_google.connection(db, member_id)
    if not connection:
        raise HTTPException(409, "Connect your Google account first")
    settings = team_google.team_settings()
    if not settings:
        raise HTTPException(503, "Google Calendar integration is not configured")
    try:
        return google.access_token(settings, connection.encrypted_refresh_token)
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, "Google authorization failed; reconnect your account") from exc


def list_calendars(access_token: str) -> list[dict]:
    """List all Google calendars with pagination, no arbitrary user URLs."""
    page = None
    calendars = []
    for _ in range(MAX_SYNC_PAGES):
        params = {"maxResults": 250, "showHidden": "true"}
        if page:
            params["pageToken"] = page
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(google.CALENDAR_API + "/users/me/calendarList", params=params,
                                      headers={"Authorization": "Bearer " + access_token})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise google.GoogleIntegrationError("Could not list Google calendars; reconnect to grant calendar list access") from exc
        for item in data.get("items", []):
            calendar_id = item.get("id", "")
            if not isinstance(calendar_id, str) or not (1 <= len(calendar_id) <= 320) or item.get("deleted"):
                continue
            calendars.append({"id": calendar_id, "name": str(item.get("summary", calendar_id))[:200],
                              "readable": item.get("accessRole", "none") not in {"none"},
                              "primary": bool(item.get("primary"))})
        page = data.get("nextPageToken")
        if not page:
            return calendars
    raise google.GoogleIntegrationError("Google calendar listing exceeds supported page limit")


def refresh_catalog(db: Session, member_id: int, calendars: list[dict]) -> int:
    """Keep previous choices, safely fall back to primary on first discovery."""
    rows = {c.calendar_id: c for c in db.scalars(select(MemberCalendar).where(MemberCalendar.member_id == member_id))}
    seen: set[str] = set()
    for entry in calendars:
        cal_id = entry["id"]
        if cal_id in seen:
            continue
        seen.add(cal_id)
        found = rows.get(cal_id)
        if not found:
            found = MemberCalendar(member_id=member_id, calendar_id=cal_id, display_name=entry["name"],
                                   selected=bool(entry.get("primary")) if not rows else False)
            db.add(found)
        else:
            found.display_name = entry["name"]
        found.readable = bool(entry["readable"])
        if not found.readable:
            found.selected = False
    for cal_id, row in rows.items():
        if cal_id not in seen:
            row.selected = False
            row.readable = False
    return len(seen)


def chosen_calendars(db: Session, member_id: int, *, primary_id: str = "primary") -> list[str]:
    """Legacy v0.9 members have no catalog and remain on their primary calendar."""
    rows = db.scalars(select(MemberCalendar).where(MemberCalendar.member_id == member_id)).all()
    if not rows:
        return [primary_id]
    return [r.calendar_id for r in rows if r.selected and r.readable]


def multi_busy(db: Session, member_id: int, day: date) -> list[tuple[datetime, datetime]]:
    import team_google
    linked = team_google.connection(db, member_id)
    if not linked:
        return []
    ids = chosen_calendars(db, member_id, primary_id=linked.calendar_id)
    if not ids:
        # Explicitly selecting nothing is unsafe; do not silently grant availability.
        raise HTTPException(503, "Select at least one readable Google calendar to check availability")
    token = _settings_token(db, member_id)
    start = datetime.combine(day, clock_time.min, tzinfo=core.HOST_TZ)
    end = datetime.combine(day + timedelta(days=1), clock_time.min, tzinfo=core.HOST_TZ)
    try:
        intervals = []
        for calendar_id in ids:
            intervals.extend(google.freebusy(token, calendar_id, start, end))
        return intervals
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, "Availability could not be verified for all selected Google calendars") from exc


def organizer_choice(db: Session, booking: core.Booking, members: list[team.Member]) -> int | None:
    group = db.scalar(select(team.GroupEvent).where(team.GroupEvent.event_type_id == booking.event_type_id))
    if not group or group.mode != "collective":
        return None
    chosen = db.get(GroupOrganizer, group.id)
    if chosen and any(m.id == chosen.member_id and m.active for m in members):
        return chosen.member_id
    return None


def _require_public_https() -> str:
    base = core.PUBLIC_BASE_URL
    from urllib.parse import urlsplit
    from ipaddress import ip_address
    parts = urlsplit(base)
    host = parts.hostname or ""
    try:
        local_ip = not ip_address(host).is_global
    except ValueError:
        local_ip = False
    if parts.scheme != "https" or not host or host in {"localhost"} or host.endswith(".localhost") or local_ip or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise HTTPException(503, "Google push notifications require a public HTTPS hostname")
    return base + "/webhooks/google/calendar"


def _google_watch_call(access_token: str, method: str, endpoint: str, payload: dict) -> dict:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.request(method, google.CALENDAR_API + endpoint, json=payload,
                                      headers={"Authorization": "Bearer " + access_token})
        response.raise_for_status()
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise google.GoogleIntegrationError("Google Calendar watch registration failed") from exc


def register_watch(db: Session, member_id: int, calendar_id: str) -> CalendarWatch:
    """Register renewal before deactivating old channel; never persist plaintext token."""
    address = _require_public_https()
    selected = chosen_calendars(db, member_id)
    if calendar_id not in selected:
        raise HTTPException(403, "Calendar must be selected before enabling notifications")
    token = _settings_token(db, member_id)
    channel_id = str(uuid.uuid4())
    channel_token = secrets.token_urlsafe(36)
    result = _google_watch_call(token, "POST", f"/calendars/{quote(calendar_id, safe='')}/events/watch", {
        "id": channel_id, "type": "web_hook", "address": address,
        "token": channel_token, "params": {"ttl": "604800"},
    })
    expires_ms = result.get("expiration")
    expires = datetime.fromtimestamp(int(expires_ms)/1000, timezone.utc).replace(tzinfo=None) if expires_ms else UTC_NOW() + timedelta(days=6)
    resource_id = str(result.get("resourceId", ""))
    if not resource_id or expires <= UTC_NOW():
        raise google.GoogleIntegrationError("Google returned an incomplete notification channel")
    # Copy the prior cursor if renewing a channel for the same calendar.
    previous = db.scalar(select(CalendarWatch).where(CalendarWatch.member_id == member_id,
        CalendarWatch.calendar_id == calendar_id, CalendarWatch.active.is_(True)).order_by(CalendarWatch.expires_utc.desc()))
    new = CalendarWatch(channel_id=channel_id, member_id=member_id, calendar_id=calendar_id,
                        resource_id=resource_id, token_hash=hashlib.sha256(channel_token.encode()).hexdigest(),
                        expires_utc=expires, active=True, dirty=True,
                        sync_token=previous.sync_token if previous else None)
    db.add(new)
    db.flush()
    if previous:
        previous.active = False
    db.commit()
    if previous:
        try:
            _google_watch_call(token, "POST", "/channels/stop", {
                "id": previous.channel_id, "resourceId": previous.resource_id})
        except google.GoogleIntegrationError:
            LOG.warning("Old Google channel could not be stopped after renewal member_id=%s", member_id)
    return new


def incoming_push(db: Session, headers) -> bool:
    channel_id = headers.get("x-goog-channel-id", "")
    resource_id = headers.get("x-goog-resource-id", "")
    token = headers.get("x-goog-channel-token", "")
    state = headers.get("x-goog-resource-state", "")
    sequence = headers.get("x-goog-message-number", "")
    if not channel_id or not token or not resource_id or state not in {"sync", "exists", "not_exists"}:
        raise HTTPException(403, "Invalid Google notification")
    try:
        message_no = int(sequence)
        if message_no < 1:
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(403, "Invalid Google notification number") from exc
    watch = db.get(CalendarWatch, channel_id)
    if not watch or not watch.active or watch.expires_utc < UTC_NOW() or not hmac.compare_digest(watch.resource_id, resource_id):
        # Google can send an initial sync before watch registration returns.
        # Ignoring that early harmless sync does not authenticate any mutation.
        if state == "sync" and not watch:
            return False
        raise HTTPException(403, "Unknown Google channel")
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), watch.token_hash):
        raise HTTPException(403, "Invalid Google notification token")
    if message_no <= watch.last_message_number:
        return False
    watch.last_message_number = message_no
    watch.last_push_utc = UTC_NOW()
    if state != "sync":
        watch.dirty = True
    db.commit()
    return state != "sync"


def _fetch_delta(access_token: str, calendar_id: str, sync_token: str | None) -> tuple[list[dict], str, bool]:
    """Incremental events.list, with 410 recovery and bounded pagination."""
    path = google.CALENDAR_API + f"/calendars/{quote(calendar_id, safe='')}/events"
    reset = False
    for attempt in range(2):
        params = {"maxResults": 250, "showDeleted": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        events = []
        for _ in range(MAX_SYNC_PAGES):
            try:
                with httpx.Client(timeout=12) as client:
                    result = client.get(path, params=params, headers={"Authorization": "Bearer " + access_token})
                if result.status_code == 410 and sync_token and not reset:
                    reset = True
                    sync_token = None
                    break
                result.raise_for_status()
                doc = result.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise google.GoogleIntegrationError("Google Calendar incremental sync failed") from exc
            events.extend(doc.get("items", []))
            page = doc.get("nextPageToken")
            if not page:
                next_token = doc.get("nextSyncToken")
                if not next_token:
                    raise google.GoogleIntegrationError("Google did not return a synchronization cursor")
                return events, next_token, reset
            params["pageToken"] = page
        else:
            raise google.GoogleIntegrationError("Google events exceed safe pagination limit")
    raise google.GoogleIntegrationError("Could not recover from an expired Google sync token")


def _flag_external_changes(db: Session, watch: CalendarWatch, events: list[dict]) -> int:
    """Flag only ZEQORO-owned event edits; never overwrite Google-side changes."""
    import team_google
    import team_google
    linked = team_google.connection(db, watch.member_id)
    if not linked or watch.calendar_id != linked.calendar_id:
        return 0
    ids = {str(e.get("id")): e for e in events if e.get("id")}
    if not ids:
        return 0
    links = db.scalars(select(team_google.TeamGoogleEventLink).where(
        team_google.TeamGoogleEventLink.organizer_member_id == watch.member_id,
        team_google.TeamGoogleEventLink.event_id.in_(ids.keys()),
        team_google.TeamGoogleEventLink.status.in_(("synced", "pending_meet")))).all()
    count = 0
    for link in links:
        event = ids[link.event_id]
        discrepancy = event.get("status") == "cancelled"
        if not discrepancy and link.synced_start_utc and link.synced_end_utc:
            try:
                start_str = event["start"]["dateTime"]
                end_str = event["end"]["dateTime"]
                provider_start = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                provider_end = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                discrepancy = provider_start != link.synced_start_utc or provider_end != link.synced_end_utc
            except (KeyError, TypeError, ValueError):
                discrepancy = True
        if discrepancy:
            link.status = "external_review"
            link.last_error = "Google event was changed outside ZEQORO; review before retrying synchronization"
            link.updated_utc = UTC_NOW()
            count += 1
    return count


def process_watch(channel_id: str) -> bool:
    """Consume a durable dirty flag. New notifications arriving during sync remain dirty."""
    with core.SessionLocal() as db:
        watch = db.get(CalendarWatch, channel_id)
        if not watch or not watch.active or not watch.dirty:
            return False
        linked = db.get(__import__("team_google").MemberGoogleConnection, watch.member_id)
        if not linked:
            watch.active = False
            db.commit()
            return False
        captured_number = watch.last_message_number
        last_cursor = watch.sync_token
        try:
            token = _settings_token(db, watch.member_id)
            events, cursor, _ = _fetch_delta(token, watch.calendar_id, last_cursor)
            _flag_external_changes(db, watch, events)
            watch.sync_token = cursor
            watch.last_delta_count = len(events)
            watch.last_sync_utc = UTC_NOW()
            watch.last_error = None
            # CAS on message number to preserve notifications during in-flight work.
            db.refresh(watch, attribute_names=["last_message_number"])
            if watch.last_message_number == captured_number:
                watch.dirty = False
            db.commit()
            return True
        except (google.GoogleIntegrationError, HTTPException) as exc:
            watch.last_error = str(exc)[:320]
            db.commit()  # dirty stays set, safe to retry
            return False


def run_due_watches(limit: int = 10, lease=None) -> dict[str, int]:
    """Worker-safe operations should be protected with a shared Redis lease."""
    processed = 0
    failures = 0
    renewed = 0
    with core.SessionLocal() as db:
        channels = list(db.scalars(select(CalendarWatch.channel_id).where(
            CalendarWatch.active.is_(True), CalendarWatch.dirty.is_(True)).limit(limit)))
    for channel in channels:
        lock = lease.acquire("calendar-push-" + channel) if lease else "no-lease"
        if not lock:
            continue
        try:
            if process_watch(channel):
                processed += 1
            else:
                failures += 1
        finally:
            if lease:
                lease.release("calendar-push-" + channel, lock)
    if core.PUBLIC_BASE_URL.startswith("https://"):
        with core.SessionLocal() as db:
            due = list(db.scalars(select(CalendarWatch).where(CalendarWatch.active.is_(True),
                CalendarWatch.expires_utc < UTC_NOW() + timedelta(hours=24)).limit(limit)))
            for watch in due:
                key = "calendar-renew-" + str(watch.member_id) + "-" + hashlib.sha256(watch.calendar_id.encode()).hexdigest()[:12]
                lock = lease.acquire(key) if lease else "no-lease"
                if not lock:
                    continue
                try:
                    register_watch(db, watch.member_id, watch.calendar_id)
                    renewed += 1
                except (HTTPException, google.GoogleIntegrationError):
                    LOG.warning("Google notification channel renewal failed member_id=%s", watch.member_id)
                finally:
                    if lease:
                        lease.release(key, lock)
    return {"push_processed": processed, "push_failures": failures, "push_renewed": renewed}


def install(web: FastAPI) -> None:
    @web.post("/webhooks/google/calendar")
    def webhook(request: Request):
        with core.SessionLocal() as db:
            incoming_push(db, request.headers)
        return Response(status_code=204)

    @web.post("/team/google/calendars/refresh")
    def refresh(request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            member = team._need_member(db, request)
            team._csrf(request, token)
            core.throttle(request, "calendar_list", limit=15, window=3600)
            access = _settings_token(db, member.id)
            try:
                result = list_calendars(access)
            except google.GoogleIntegrationError as exc:
                raise HTTPException(503, "Could not list calendars; reconnect with updated permissions") from exc
            refresh_catalog(db, member.id, result)
            db.commit()
        return RedirectResponse("/team/google?result=refreshed", 303)

    @web.post("/team/google/calendars/select")
    async def select_calendars(request: Request):
        form = await request.form()
        with core.SessionLocal() as db:
            member = team._need_member(db, request)
            team._csrf(request, str(form.get("token", "")))
            ids = {str(x) for x in form.getlist("calendar_ids")}
            available = list(db.scalars(select(MemberCalendar).where(MemberCalendar.member_id == member.id,
                            MemberCalendar.readable.is_(True))))
            if not ids or len(ids) > MAX_CALENDARS or not ids.issubset({r.calendar_id for r in available}):
                raise HTTPException(400, "Select between one and ten readable calendars")
            for row in available:
                row.selected = row.calendar_id in ids
            # Stop treating unselected calendars as active resources locally.
            old_watches = db.scalars(select(CalendarWatch).where(CalendarWatch.member_id == member.id,
                CalendarWatch.active.is_(True))).all()
            for watch in old_watches:
                if watch.calendar_id not in ids:
                    watch.active = False
            db.commit()
        return RedirectResponse("/team/google?result=selected", 303)

    @web.post("/team/google/watches/refresh")
    def refresh_watches(request: Request, token: str = Form(...)):
        with core.SessionLocal() as db:
            member = team._need_member(db, request)
            team._csrf(request, token)
            core.throttle(request, "calendar_watch", limit=10, window=3600)
            ids = chosen_calendars(db, member.id)
            if not ids:
                raise HTTPException(409, "Choose calendars before setting up notifications")
            _require_public_https()
            for calendar_id in ids:
                try:
                    register_watch(db, member.id, calendar_id)
                except google.GoogleIntegrationError as exc:
                    raise HTTPException(503, "Google could not register a watch for every selected calendar") from exc
        return RedirectResponse("/team/google?result=watching", 303)

    @web.post("/team/google/groups/{group_id}/organizer")
    def set_group_organizer(group_id: int, request: Request, token: str = Form(...), member_id: int = Form(...)):
        with core.SessionLocal() as db:
            actor = team._need_member(db, request, ("owner", "admin"))
            team._csrf(request, token)
            group = db.get(team.GroupEvent, group_id)
            if not group or group.organization_id != actor.organization_id or group.mode != "collective":
                raise HTTPException(404, "Collective group event not found")
            accepted = db.scalar(select(team.GroupParticipant.id).join(team.Member, team.Member.id == team.GroupParticipant.member_id)
                .where(team.GroupParticipant.group_event_id == group.id, team.Member.id == member_id,
                       team.Member.active.is_(True), team.Member.organization_id == actor.organization_id))
            if not accepted:
                raise HTTPException(403, "The organizer must participate in this collective event")
            row = db.get(GroupOrganizer, group.id)
            if row:
                row.member_id = member_id
            else:
                db.add(GroupOrganizer(group_id=group.id, member_id=member_id))
            db.commit()
        return RedirectResponse("/team/google?result=organizer", 303)
