"""ZEQORO: independent, single-host appointment booking MVP.

SQLite / single-worker edition. Dates in DB are naive UTC; all public display
and availability calculations use the configured IANA host timezone.
"""
from __future__ import annotations

import csv
import hmac
import io
import os
import re
import secrets
import threading
import time as monotonic_time
from collections import defaultdict, deque
import smtplib
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import google_integration as google

from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select, text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
APP_ENV = os.getenv("APP_ENV", "development")
APP_SECRET = os.getenv("APP_SECRET", "dev-only-change-before-deploying")
ADMIN_USER = os.getenv("ADMIN_USER", "anwar")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")
HOST_NAME = os.getenv("HOST_NAME", "Anwar")
TZ_NAME = os.getenv("HOST_TZ", "Africa/Cairo")
try:
    HOST_TZ = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError as e:
    raise RuntimeError(f"Invalid HOST_TZ: {TZ_NAME}") from e
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'calendar.db'}")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
if APP_ENV == "production":
    if len(APP_SECRET) < 32 or APP_SECRET.startswith(("dev-", "replace-")):
        raise RuntimeError("Set a random APP_SECRET with 32+ characters for production")
    if ADMIN_PASSWORD in {"change-this-password", "admin", "password"} or len(ADMIN_PASSWORD) < 12:
        raise RuntimeError("Set a strong ADMIN_PASSWORD for production")
    if not COOKIE_SECURE or not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("Production requires HTTPS PUBLIC_BASE_URL and COOKIE_SECURE=true")

if DATABASE_URL.startswith("sqlite:"):
    path_string = DATABASE_URL.split("sqlite:///", 1)[-1]
    Path(path_string).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class EventType(Base):
    __tablename__ = "event_types"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    buffer_before_min: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after_min: Mapped[int] = mapped_column(Integer, default=10)
    min_notice_hours: Mapped[int] = mapped_column(Integer, default=4)
    max_advance_days: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bookings: Mapped[list["Booking"]] = relationship(back_populates="event_type")

class Availability(Base):
    __tablename__ = "availability"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer, unique=True)
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)

class BlockedDate(Base):
    """Whole-day time off in the host's configured timezone."""
    __tablename__ = "blocked_dates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(160), default="Unavailable")

class GoogleConnection(Base):
    """Single-host calendar authorization; refresh token is encrypted at rest."""
    __tablename__ = "google_connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    calendar_id: Mapped[str] = mapped_column(String(320), default="primary")
    connected_utc: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

class GoogleEventLink(Base):
    """Durable idempotency and retry state for each Google event."""
    __tablename__ = "google_event_links"
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    meet_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(320), nullable=True)
    synced_start_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_end_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_utc: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.id"))
    guest_name: Mapped[str] = mapped_column(String(160))
    guest_email: Mapped[str] = mapped_column(String(320))
    notes: Mapped[str] = mapped_column(Text, default="")
    start_utc: Mapped[datetime] = mapped_column(DateTime)
    end_utc: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    cancel_token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_utc: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    event_type: Mapped[EventType] = relationship(back_populates="bookings")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(EventType.id).limit(1)):
            db.add_all([
                EventType(slug="quick-call", title="Quick Intro Call", description="A short call to connect and explore an idea.", duration_min=15),
                EventType(slug="sponsor-meeting", title="Sponsor Discussion", description="Let's discuss partnership and sponsorship opportunities.", duration_min=30),
                EventType(slug="business-meeting", title="Business Meeting", description="A focused meeting for a project or collaboration.", duration_min=60),
            ])
            db.add_all([Availability(weekday=i, start_min=9*60, end_min=17*60) for i in range(5)])
        db.commit()
    yield

app = FastAPI(title="ZEQORO", version="0.6.0", lifespan=lifespan)

class SlidingWindowLimiter:
    """Single-process throttle. Replace with Redis before multi-worker deployment."""
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = defaultdict(deque)

    def check(self, scope: str, key: str, limit: int, window: int) -> bool:
        now = monotonic_time.monotonic()
        identifier = (scope, key)
        with self._lock:
            events = self._entries[identifier]
            while events and events[0] <= now - window:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    def clear(self, scope: str, key: str) -> None:
        with self._lock:
            self._entries.pop((scope, key), None)

limiter = SlidingWindowLimiter()

def client_key(request: Request) -> str:
    # Do not trust X-Forwarded-For unless a trusted proxy config is enforced.
    return request.client.host if request.client else "unknown"

def throttle(request: Request, scope: str, *, limit: int, window: int) -> None:
    if not limiter.check(scope, client_key(request), limit, window):
        raise HTTPException(429, "Too many requests. Please try again later.", headers={"Retry-After": str(window)})

app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=COOKIE_SECURE, max_age=60*60*24*3)
@app.middleware("http")
async def privacy_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith(("/admin", "/confirmed/", "/cancel/", "/reschedule/", "/ics/")):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

def local_time(dt_utc: datetime, fmt: str = "%a, %d %b %Y · %I:%M %p") -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(HOST_TZ).strftime(fmt)

templates.env.filters["local_time"] = local_time

def page(request: Request, name: str, **context):
    return templates.TemplateResponse(request, name, {"host": HOST_NAME, "tz": TZ_NAME, "base_url": PUBLIC_BASE_URL, **context})

def utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def get_event(db: Session, slug: str) -> EventType | None:
    return db.scalar(select(EventType).where(EventType.slug == slug, EventType.is_active.is_(True)))

def available_slots(db: Session, event: EventType, day: date, *, ignore_booking_id: int | None = None) -> list[dict[str, str]]:
    today = datetime.now(timezone.utc)
    if day < today.astimezone(HOST_TZ).date() or day > (today.astimezone(HOST_TZ) + timedelta(days=event.max_advance_days)).date():
        return []
    if db.scalar(select(BlockedDate.id).where(BlockedDate.day == day.isoformat())):
        return []
    availability = db.scalar(select(Availability).where(Availability.weekday == day.weekday()))
    if not availability:
        return []
    low = utc_naive(datetime.combine(day - timedelta(days=1), time.min, tzinfo=HOST_TZ))
    high = utc_naive(datetime.combine(day + timedelta(days=2), time.min, tzinfo=HOST_TZ))
    query = select(Booking).where(Booking.status == "confirmed", Booking.end_utc >= low, Booking.start_utc <= high)
    if ignore_booking_id is not None:
        query = query.where(Booking.id != ignore_booking_id)
    existing = db.scalars(query).all()
    external_busy = calendar_busy(db, day)
    result = []
    for minute in range(availability.start_min, availability.end_min - event.duration_min + 1, 15):
        hour, rem = divmod(minute, 60)
        naive_local = datetime.combine(day, time(hour, rem))
        start_local = naive_local.replace(tzinfo=HOST_TZ)
        # Skip nonexistent DST wall-clock times.
        if start_local.astimezone(timezone.utc).astimezone(HOST_TZ).replace(tzinfo=None) != naive_local:
            continue
        end_local = start_local + timedelta(minutes=event.duration_min)
        if end_local.astimezone(timezone.utc).astimezone(HOST_TZ).replace(tzinfo=None) != end_local.replace(tzinfo=None):
            continue
        start_aware_utc = start_local.astimezone(timezone.utc)
        if start_aware_utc < today + timedelta(hours=event.min_notice_hours) or start_aware_utc > today + timedelta(days=event.max_advance_days):
            continue
        candidate_start = utc_naive(start_local) - timedelta(minutes=event.buffer_before_min)
        candidate_end = utc_naive(end_local) + timedelta(minutes=event.buffer_after_min)
        collision = False
        for booking in existing:
            old_start = booking.start_utc - timedelta(minutes=booking.event_type.buffer_before_min)
            old_end = booking.end_utc + timedelta(minutes=booking.event_type.buffer_after_min)
            if candidate_start < old_end and candidate_end > old_start:
                collision = True
                break
        if not collision:
            for busy_start, busy_end in external_busy:
                if candidate_start < busy_end and candidate_end > busy_start:
                    collision = True
                    break
        if not collision:
            result.append({"start": start_local.isoformat(), "label": start_local.strftime("%I:%M %p").lstrip("0")})
    return result

def parse_selected_start(value: str) -> tuple[datetime, date]:
    try:
        selected = datetime.fromisoformat(value)
        if selected.tzinfo is None:
            raise ValueError()
        host_equivalent = selected.astimezone(HOST_TZ)
        # A client must submit the exact host-local start supplied by the slots API.
        # Reject spoofed offsets even when the absolute UTC instant matches.
        if selected.utcoffset() != host_equivalent.utcoffset():
            raise ValueError()
        selected_day = host_equivalent.date()
        return selected, selected_day
    except ValueError as exc:
        raise ValueError("Please choose a valid appointment time.") from exc

def write_serialize(db: Session) -> None:
    # SQLite single-writer lock prevents double booking when multiple requests race.
    if engine.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))

def google_settings() -> google.Settings | None:
    return google.Settings.from_env(PUBLIC_BASE_URL)


def google_connection(db: Session) -> GoogleConnection | None:
    return db.get(GoogleConnection, 1)


def calendar_busy(db: Session, day: date) -> list[tuple[datetime, datetime]]:
    """Get actual busy times, conservatively failing closed for connected calendars."""
    connection = google_connection(db)
    if not connection:
        return []
    try:
        settings = google_settings()
        if not settings:
            raise google.GoogleIntegrationError("Google credentials are not configured")
        token = google.access_token(settings, connection.encrypted_refresh_token)
        start = datetime.combine(day, time.min, tzinfo=HOST_TZ)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=HOST_TZ)
        return google.freebusy(token, connection.calendar_id, start, end)
    except google.GoogleIntegrationError as exc:
        # Do not display slots we cannot verify against Google Calendar.
        raise HTTPException(503, "Calendar availability is temporarily unavailable. Please retry shortly.") from exc


def sync_google_booking(booking_id: int) -> str:
    """Idempotent sync job. Persist links before calling Google; retry failed jobs in admin.

    A local booking remains confirmed if Google fails; UI explicitly shows pending/error.
    A persistent event ID ensures retries don't create duplicate events.
    """
    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        if not booking:
            return "missing"
        connection = google_connection(db)
        link = db.get(GoogleEventLink, booking_id)
        if not connection:
            return "not_connected"
        if not link:
            # Base32hex accepts digits and a-v; hexadecimal UUID is a valid subset.
            link = GoogleEventLink(booking_id=booking.id, event_id="zq" + secrets.token_hex(16), status="pending")
            db.add(link)
            db.commit()
        try:
            settings = google_settings()
            if not settings:
                raise google.GoogleIntegrationError("Google connection configuration is missing")
            token = google.access_token(settings, connection.encrypted_refresh_token)
            if booking.status != "confirmed":
                google.delete_event(token, connection.calendar_id, link.event_id)
                link.status = "deleted"
                link.meet_url = None
            else:
                details = google.event_data(
                    booking.event_type.title, HOST_NAME, booking.guest_name,
                    booking.guest_email, booking.start_utc, booking.end_utc,
                    link.event_id, create_meet=True,
                )
                try:
                    if link.synced_start_utc == booking.start_utc and link.synced_end_utc == booking.end_utc:
                        result = google.get_event(token, connection.calendar_id, link.event_id)
                    elif link.synced_start_utc is not None:
                        details_update = dict(details)
                        details_update.pop("id")
                        details_update.pop("conferenceData")  # Preserve existing Meet room.
                        result = google.patch_event(token, connection.calendar_id, link.event_id, details_update)
                    else:
                        result = google.insert_event(token, connection.calendar_id, details)
                except google.GoogleEventMissing:
                    # Account changed or event deleted externally. Recreate with the
                    # same stable ID; Google will deduplicate if it already exists.
                    result = google.insert_event(token, connection.calendar_id, details)
                link.meet_url = google.meet_link(result)
                link.status = "synced" if link.meet_url else "pending_meet"
                link.synced_start_utc = booking.start_utc
                link.synced_end_utc = booking.end_utc
            link.last_error = None
        except google.GoogleIntegrationError as exc:
            link.status = "error"
            link.last_error = str(exc)[:320]
        link.updated_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return link.status


def booking_google_link(db: Session, booking_id: int) -> GoogleEventLink | None:
    return db.get(GoogleEventLink, booking_id)


def admin_only(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})

def csrf(request: Request, token: str):
    if not request.session.get("admin") or not hmac.compare_digest(str(token), str(request.session.get("csrf", "missing"))):
        raise HTTPException(403, detail="Invalid session or CSRF token")

def public_error(request: Request, message: str, status: int = 400):
    return templates.TemplateResponse(request, "error.html", {"host": HOST_NAME, "tz": TZ_NAME, "base_url": PUBLIC_BASE_URL, "message": message}, status_code=status)

def booking_email(to_email: str, guest_name: str, event_title: str, start: datetime, token: str):
    host = os.getenv("SMTP_HOST", "")
    sender = os.getenv("SMTP_FROM", "")
    if not host or not sender:
        return
    email = EmailMessage()
    email["From"] = sender
    email["To"] = to_email
    email["Subject"] = f"Confirmed: {event_title} with {HOST_NAME}"
    meet = None
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if booking:
            link = booking_google_link(db, booking.id)
            if link and link.status == "synced":
                meet = link.meet_url
    meeting_detail = f"\nGoogle Meet: {meet}\n" if meet else "\nVideo meeting link: check your booking page for updates.\n"
    email.set_content(f"Hello {guest_name},\n\nYour meeting is confirmed.\n{event_title}\n{local_time(start)} ({TZ_NAME})\n{meeting_detail}\nManage: {PUBLIC_BASE_URL}/confirmed/{token}\n\n— {HOST_NAME}")
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as server:
            if os.getenv("SMTP_USE_TLS", "true").lower() == "true":
                server.starttls()
            if os.getenv("SMTP_USER"):
                server.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", ""))
            server.send_message(email)
    except Exception as exc:
        # Email transport must not undo an already committed booking.
        print(f"SMTP delivery failed: {type(exc).__name__}")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with SessionLocal() as db:
        events = db.scalars(select(EventType).where(EventType.is_active.is_(True)).order_by(EventType.duration_min)).all()
        return page(request, "home.html", events=events)

@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works(request: Request):
    return page(request, "how_it_works.html")

@app.get("/book/{slug}", response_class=HTMLResponse)
def booking_page(slug: str, request: Request):
    with SessionLocal() as db:
        event = get_event(db, slug)
        if not event:
            return public_error(request, "This booking link is not available.", 404)
        today = datetime.now(HOST_TZ).date()
        default_day = today
        for offset in range(min(event.max_advance_days + 1, 14)):
            candidate = today + timedelta(days=offset)
            if available_slots(db, event, candidate):
                default_day = candidate
                break
        return page(request, "book.html", event=event, today=today.isoformat(), default_day=default_day.isoformat(), max_date=(datetime.now(HOST_TZ) + timedelta(days=event.max_advance_days)).date().isoformat())

@app.get("/api/slots/{slug}")
def slots_api(slug: str, day: str):
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD")
    with SessionLocal() as db:
        event = get_event(db, slug)
        if not event:
            raise HTTPException(404, "Event not available")
        return {"slots": available_slots(db, event, parsed), "timezone": TZ_NAME}

@app.post("/book/{slug}")
def book(slug: str, request: Request, tasks: BackgroundTasks, guest_name: str = Form(...), guest_email: str = Form(...), start: str = Form(...), notes: str = Form("")):
    throttle(request, "public_booking", limit=12, window=3600)
    guest_name, guest_email, notes = guest_name.strip(), guest_email.strip(), notes.strip()
    if len(guest_name) < 2 or len(guest_name) > 160 or len(notes) > 2000:
        return public_error(request, "Please enter a valid name (2–160 characters) and a shorter note.")
    try:
        guest_email = validate_email(guest_email, check_deliverability=False).normalized
    except EmailNotValidError:
        return public_error(request, "Please enter a valid email address.")
    try:
        selected, selected_day = parse_selected_start(start)
    except ValueError as exc:
        return public_error(request, str(exc))
    with SessionLocal() as db:
        # Serialize booking checks+writes for SQLite to prevent two guests
        # taking the same time slot under concurrent requests.
        write_serialize(db)
        event = get_event(db, slug)
        if not event:
            db.rollback()
            return public_error(request, "This booking link is not available.", 404)
        valid_starts = {slot["start"] for slot in available_slots(db, event, selected_day)}
        if selected.isoformat() not in valid_starts:
            db.rollback()
            return public_error(request, "This time has become unavailable. Please return and choose another slot.", 409)
        booking = Booking(event_type_id=event.id, guest_name=guest_name, guest_email=guest_email, notes=notes, start_utc=utc_naive(selected), end_utc=utc_naive(selected + timedelta(minutes=event.duration_min)), status="confirmed", cancel_token=secrets.token_urlsafe(32))
        db.add(booking)
        db.commit()
        # Remote write is idempotent and independently retriable if Google is offline.
        sync_google_booking(booking.id)
        tasks.add_task(booking_email, booking.guest_email, booking.guest_name, event.title, booking.start_utc, booking.cancel_token)
        return RedirectResponse(f"/confirmed/{booking.cancel_token}", status_code=303)

@app.get("/confirmed/{token}", response_class=HTMLResponse)
def confirmed(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This confirmation link is invalid.", 404)
        return page(request, "confirmed.html", booking=booking, google_link=booking_google_link(db, booking.id), google_connected=bool(google_connection(db)))

@app.get("/cancel/{token}", response_class=HTMLResponse)
def cancel_page(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This booking could not be found.", 404)
        return page(request, "cancel.html", booking=booking)

@app.post("/cancel/{token}")
def cancel_booking(token: str, request: Request):
    throttle(request, "cancel", limit=25, window=3600)
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This booking could not be found.", 404)
        if booking.status != "confirmed":
            return RedirectResponse(f"/cancel/{token}", status_code=303)
        if booking.start_utc <= datetime.now(timezone.utc).replace(tzinfo=None):
            return public_error(request, "You cannot cancel a meeting after its start time.")
        booking.status = "cancelled"
        db.commit()
        sync_google_booking(booking.id)
        return RedirectResponse(f"/cancel/{token}", status_code=303)

@app.get("/reschedule/{token}", response_class=HTMLResponse)
def reschedule_page(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This booking could not be found.", 404)
        if booking.status != "confirmed" or booking.start_utc <= datetime.now(timezone.utc).replace(tzinfo=None):
            return public_error(request, "This meeting can no longer be rescheduled.", 409)
        event = booking.event_type
        today = datetime.now(HOST_TZ).date()
        return page(request, "reschedule.html", booking=booking, event=event, today=today.isoformat(), default_day=booking.start_utc.replace(tzinfo=timezone.utc).astimezone(HOST_TZ).date().isoformat(), max_date=(datetime.now(HOST_TZ) + timedelta(days=event.max_advance_days)).date().isoformat())

@app.get("/api/reschedule-slots/{token}")
def reschedule_slots(token: str, day: str):
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD")
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            raise HTTPException(404, "Booking not found")
        if booking.status != "confirmed" or booking.start_utc <= datetime.now(timezone.utc).replace(tzinfo=None):
            raise HTTPException(409, "Booking is no longer active")
        return {"slots": available_slots(db, booking.event_type, parsed, ignore_booking_id=booking.id), "timezone": TZ_NAME}

@app.post("/reschedule/{token}")
def reschedule_booking(token: str, request: Request, tasks: BackgroundTasks, start: str = Form(...)):
    try:
        selected, selected_day = parse_selected_start(start)
    except ValueError as exc:
        return public_error(request, str(exc))
    throttle(request, "reschedule", limit=25, window=3600)
    with SessionLocal() as db:
        write_serialize(db)
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            db.rollback()
            return public_error(request, "This booking could not be found.", 404)
        if booking.status != "confirmed" or booking.start_utc <= datetime.now(timezone.utc).replace(tzinfo=None):
            db.rollback()
            return public_error(request, "This meeting can no longer be rescheduled.", 409)
        event = booking.event_type
        if selected.isoformat() not in {slot["start"] for slot in available_slots(db, event, selected_day, ignore_booking_id=booking.id)}:
            db.rollback()
            return public_error(request, "This time is unavailable. Choose a different time.", 409)
        booking.start_utc = utc_naive(selected)
        booking.end_utc = utc_naive(selected + timedelta(minutes=event.duration_min))
        db.commit()
        sync_google_booking(booking.id)
        tasks.add_task(booking_email, booking.guest_email, booking.guest_name, "Updated: " + event.title, booking.start_utc, booking.cancel_token)
        return RedirectResponse(f"/confirmed/{token}", status_code=303)

@app.get("/ics/{token}")
def ics(token: str):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking or booking.status != "confirmed":
            raise HTTPException(404)
        fmt = lambda d: d.strftime("%Y%m%dT%H%M%SZ")
        def esc(value):
            return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        link = booking_google_link(db, booking.id)
        meet = link.meet_url if link and link.status == "synced" else None
        body = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ZEQORO//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
            f"UID:booking-{booking.id}@zeqoro.local", f"DTSTAMP:{fmt(datetime.now(timezone.utc))}", "STATUS:CONFIRMED",
            f"DTSTART:{fmt(booking.start_utc)}", f"DTEND:{fmt(booking.end_utc)}",
            f"SUMMARY:{esc(booking.event_type.title + ' with ' + HOST_NAME)}",
            f"DESCRIPTION:{esc('Guest: ' + booking.guest_name)}",
            *([f"LOCATION:{esc(meet)}", f"URL:{meet}"] if meet else []),
            "END:VEVENT", "END:VCALENDAR", "",
        ])
        # RFC 5545: UTF-8 content lines must be folded at 75 octets.
        folded = []
        for line in body.split("\r\n"):
            if not line:
                continue
            segment = ""
            for ch in line:
                if len((segment + ch).encode("utf-8")) > 75:
                    folded.append(segment)
                    segment = " " + ch
                else:
                    segment += ch
            folded.append(segment)
        return Response("\r\n".join(folded) + "\r\n", media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": "attachment; filename=meeting.ics"})

@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin", 303)
    return page(request, "login.html", error="")

@app.post("/admin/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    throttle(request, "admin_login", limit=8, window=900)
    if hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD):
        limiter.clear("admin_login", client_key(request))
        request.session.clear()
        request.session["admin"] = True
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/admin", 303)
    return page(request, "login.html", error="Invalid username or password.")

@app.post("/admin/logout")
def logout(request: Request, token: str = Form(...)):
    csrf(request, token)
    request.session.clear()
    return RedirectResponse("/", 303)

@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", q: str = ""):
    admin_only(request)
    if status not in {"all", "confirmed", "cancelled"} or len(q) > 120:
        raise HTTPException(400, "Invalid booking filter")
    q = q.strip()
    with SessionLocal() as db:
        events = db.scalars(select(EventType).order_by(EventType.id)).all()
        query = select(Booking)
        if status != "all":
            query = query.where(Booking.status == status)
        if q:
            term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where((Booking.guest_name.ilike(term, escape="\\")) | (Booking.guest_email.ilike(term, escape="\\")) | (Booking.notes.ilike(term, escape="\\")))
        bookings = db.scalars(query.order_by(Booking.start_utc.desc()).limit(100)).all()
        schedule = {a.weekday: a for a in db.scalars(select(Availability)).all()}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        upcoming = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == "confirmed", Booking.start_utc >= now)) or 0
        cancelled = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == "cancelled")) or 0
        confirmed_total = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == "confirmed")) or 0
        future_bookings = db.scalars(select(Booking).where(Booking.status == "confirmed", Booking.start_utc >= now).order_by(Booking.start_utc.asc())).all()
        upcoming_hours = round(sum((b.end_utc - b.start_utc).total_seconds() for b in future_bookings) / 3600, 1)
        active_links = sum(bool(event.is_active) for event in events)
        today_local = datetime.now(HOST_TZ).date()
        booking_counts = [0] * 7
        for booking in future_bookings:
            local_day = booking.start_utc.replace(tzinfo=timezone.utc).astimezone(HOST_TZ).date()
            delta = (local_day - today_local).days
            if 0 <= delta < 7:
                booking_counts[delta] += 1
        scale = max(booking_counts, default=0)
        chart_days = [{"date": (today_local + timedelta(days=i)).isoformat(), "label": (today_local + timedelta(days=i)).strftime("%a"), "count": n, "height": round(100 * n / scale) if scale else 0} for i, n in enumerate(booking_counts)]
        # Summary rows respect active guest/status filters, avoiding unexpected name disclosure.
        selected_ids = {b.id for b in bookings} if q or status != "all" else None
        next_meetings = [b for b in future_bookings if selected_ids is None or b.id in selected_ids][:4]
        blocked_dates = db.scalars(select(BlockedDate).order_by(BlockedDate.day)).all()
        connected = bool(google_connection(db))
        sync_issues = db.scalar(select(func.count()).select_from(GoogleEventLink).where(GoogleEventLink.status.in_(["error", "pending", "pending_meet"]))) or 0
        return page(request, "admin.html", google_connected=connected, google_sync_issues=sync_issues, events=events, bookings=bookings, schedule=schedule, days=DAYS, now=now, upcoming=upcoming, cancelled=cancelled, blocked_dates=blocked_dates, confirmed_total=confirmed_total, upcoming_hours=upcoming_hours, active_links=active_links, next_meetings=next_meetings, chart_days=chart_days, weekly_count=sum(booking_counts), csrf_token=request.session["csrf"], filter_status=status, filter_query=q)

@app.post("/admin/events")
def new_event(request: Request, token: str = Form(...), slug: str = Form(...), title: str = Form(...), description: str = Form(""), duration_min: int = Form(...)):
    csrf(request, token)
    slug, title = slug.lower().strip(), title.strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not 1 <= len(slug) <= 80 or not 2 <= len(title) <= 160 or duration_min not in {15, 30, 45, 60, 90, 120} or len(description) > 1000:
        raise HTTPException(400, "Invalid event values")
    with SessionLocal() as db:
        if db.scalar(select(EventType.id).where(EventType.slug == slug)):
            raise HTTPException(409, "Event link already exists")
        db.add(EventType(slug=slug, title=title, description=description.strip(), duration_min=duration_min))
        db.commit()
    return RedirectResponse("/admin#events", 303)

@app.post("/admin/events/{event_id}/toggle")
def toggle_event(event_id: int, request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        event = db.get(EventType, event_id)
        if not event:
            raise HTTPException(404)
        event.is_active = not event.is_active
        db.commit()
    return RedirectResponse("/admin#events", 303)

def parse_hhmm(value: str) -> int:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Invalid time")
    h, m = map(int, value.split(":"))
    return h * 60 + m

@app.post("/admin/events/{event_id}/edit")
def edit_event(event_id: int, request: Request, token: str = Form(...), title: str = Form(...), description: str = Form(""), duration_min: int = Form(...), buffer_before_min: int = Form(0), buffer_after_min: int = Form(10), min_notice_hours: int = Form(4), max_advance_days: int = Form(60)):
    csrf(request, token)
    title, description = title.strip(), description.strip()
    if not 2 <= len(title) <= 160 or len(description) > 1000 or duration_min not in {15, 30, 45, 60, 90, 120} or not 0 <= buffer_before_min <= 120 or not 0 <= buffer_after_min <= 120 or not 0 <= min_notice_hours <= 336 or not 1 <= max_advance_days <= 365:
        raise HTTPException(400, "Invalid meeting settings")
    with SessionLocal() as db:
        event = db.get(EventType, event_id)
        if not event:
            raise HTTPException(404)
        event.title = title
        event.description = description
        event.duration_min = duration_min
        event.buffer_before_min = buffer_before_min
        event.buffer_after_min = buffer_after_min
        event.min_notice_hours = min_notice_hours
        event.max_advance_days = max_advance_days
        db.commit()
    return RedirectResponse("/admin#events", 303)

@app.post("/admin/blocked-dates")
def add_blocked_date(request: Request, token: str = Form(...), day: str = Form(...), reason: str = Form("Unavailable")):
    csrf(request, token)
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    if parsed < datetime.now(HOST_TZ).date() or parsed > datetime.now(HOST_TZ).date() + timedelta(days=730):
        raise HTTPException(400, "Choose a future date within two years")
    reason = reason.strip() or "Unavailable"
    if len(reason) > 160:
        raise HTTPException(400, "Reason is too long")
    with SessionLocal() as db:
        if db.scalar(select(BlockedDate.id).where(BlockedDate.day == day)):
            raise HTTPException(409, "This day is already blocked")
        db.add(BlockedDate(day=day, reason=reason))
        db.commit()
    return RedirectResponse("/admin#blocked-days", 303)

@app.post("/admin/blocked-dates/{block_id}/remove")
def remove_blocked_date(block_id: int, request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        block = db.get(BlockedDate, block_id)
        if not block:
            raise HTTPException(404)
        db.delete(block)
        db.commit()
    return RedirectResponse("/admin#blocked-days", 303)

@app.get("/admin/bookings.csv")
def export_bookings(request: Request):
    admin_only(request)
    with SessionLocal() as db:
        bookings = db.scalars(select(Booking).order_by(Booking.start_utc.desc())).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Event", "Guest name", "Guest email", "Start UTC", "End UTC", "Status", "Notes"])
        for b in bookings:
            # Prefix potentially spreadsheet-executable text with an apostrophe.
            def safe(value):
                v = str(value or "")
                return "'" + v if v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v
            writer.writerow([b.id, safe(b.event_type.title), safe(b.guest_name), safe(b.guest_email), b.start_utc.isoformat(), b.end_utc.isoformat(), b.status, safe(b.notes)])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="zeqoro-bookings.csv"'})

@app.post("/admin/availability")
async def set_availability(request: Request):
    admin_only(request)
    form = await request.form()
    csrf(request, str(form.get("token", "")))
    values = []
    for i in range(7):
        if form.get(f"enabled_{i}"):
            try:
                a = parse_hhmm(str(form.get(f"start_{i}", "")))
                b = parse_hhmm(str(form.get(f"end_{i}", "")))
            except ValueError:
                raise HTTPException(400, "Invalid availability format")
            if not 0 <= a < b <= 1440:
                raise HTTPException(400, "Availability end must be after start")
            values.append(Availability(weekday=i, start_min=a, end_min=b))
    with SessionLocal() as db:
        for old in db.scalars(select(Availability)).all():
            db.delete(old)
        db.flush()
        db.add_all(values)
        db.commit()
    return RedirectResponse("/admin#availability", 303)

@app.post("/admin/bookings/{booking_id}/cancel")
def admin_cancel(booking_id: int, request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        if not booking:
            raise HTTPException(404)
        booking.status = "cancelled"
        db.commit()
        sync_google_booking(booking.id)
    return RedirectResponse("/admin#bookings", 303)


@app.get("/admin/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, message: str = ""):
    admin_only(request)
    with SessionLocal() as db:
        connection = google_connection(db)
        pending = db.scalars(select(GoogleEventLink).where(GoogleEventLink.status.in_(["error", "pending", "pending_meet"])).order_by(GoogleEventLink.updated_utc.desc()).limit(30)).all()
        links = {b.booking_id: b for b in pending}
        queue = [db.get(Booking, key) for key in links]
        configured = False
        config_error = ""
        try:
            configured = google_settings() is not None
        except google.GoogleIntegrationError as exc:
            config_error = str(exc)
        allowed = {"connected": "Google Calendar connected successfully.", "disconnected": "Connection removed. Existing Google events remain on the calendar.", "synced": "Synchronization attempted; check the results below."}
        return page(request, "integrations.html", connection=connection, configured=configured, config_error=config_error, queue=queue, links=links, csrf_token=request.session["csrf"], notice=allowed.get(message, ""))


@app.post("/admin/google/connect")
def google_connect(request: Request, token: str = Form(...)):
    csrf(request, token)
    settings = google_settings()
    if not settings:
        raise HTTPException(503, "Google Cloud OAuth is not configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    request.session["google_oauth_state"] = state
    request.session["google_pkce_verifier"] = verifier
    return RedirectResponse(google.authorization_url(settings, state, verifier), 303)


@app.get("/admin/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    admin_only(request)
    saved_state = request.session.pop("google_oauth_state", None)
    verifier = request.session.pop("google_pkce_verifier", None)
    if not saved_state or not verifier or not state or not secrets.compare_digest(saved_state, state):
        raise HTTPException(403, "Invalid Google OAuth state. Restart connection from integrations.")
    if error or not code:
        raise HTTPException(400, "Google authorization was declined or incomplete")
    settings = google_settings()
    if not settings:
        raise HTTPException(503, "Google integration is not configured")
    try:
        result = google.exchange_code(settings, code, verifier)
        granted = set(result.get("scope", "").split())
        if not set(google.SCOPES).issubset(granted):
            raise google.GoogleIntegrationError("Google Calendar permissions are incomplete")
        if not result.get("refresh_token"):
            raise google.GoogleIntegrationError("Google did not provide offline access; reconnect with consent")
        encrypted = google.encrypt_secret(result["refresh_token"], settings)
    except google.GoogleIntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    with SessionLocal() as db:
        current = google_connection(db)
        if current:
            current.encrypted_refresh_token = encrypted
            current.calendar_id = "primary"
        else:
            db.add(GoogleConnection(id=1, encrypted_refresh_token=encrypted, calendar_id="primary"))
        db.commit()
    return RedirectResponse("/admin/integrations?message=connected", 303)


@app.post("/admin/google/disconnect")
def google_disconnect(request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        connected = google_connection(db)
        if connected:
            db.delete(connected)
            db.commit()
    return RedirectResponse("/admin/integrations?message=disconnected", 303)


@app.post("/admin/google/sync")
def google_sync(request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        if not google_connection(db):
            raise HTTPException(409, "Connect Google Calendar first")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        candidates = db.scalars(select(Booking).where(
            (Booking.start_utc >= now) | (Booking.status == "cancelled")
        ).order_by(Booking.created_utc.desc()).limit(150)).all()
        ids = []
        for booking in candidates:
            link = booking_google_link(db, booking.id)
            if (booking.status == "confirmed" and (not link or link.status != "synced" or link.synced_start_utc != booking.start_utc)) or (booking.status == "cancelled" and link and link.status != "deleted"):
                ids.append(booking.id)
                if len(ids) == 25:
                    break
    for booking_id in ids:
        sync_google_booking(booking_id)
    return RedirectResponse("/admin/integrations?message=synced", 303)


@app.post("/admin/google/retry/{booking_id}")
def google_retry_one(booking_id: int, request: Request, token: str = Form(...)):
    csrf(request, token)
    with SessionLocal() as db:
        if not db.get(Booking, booking_id):
            raise HTTPException(404)
        if not google_connection(db):
            raise HTTPException(409, "Connect Google Calendar first")
    sync_google_booking(booking_id)
    return RedirectResponse("/admin/integrations", 303)
