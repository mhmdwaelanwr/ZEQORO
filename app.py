"""ZEQORO: independent, single-host appointment booking MVP.

SQLite / single-worker edition. Dates in DB are naive UTC; all public display
and availability calculations use the configured IANA host timezone.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import smtplib
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select, text
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

app = FastAPI(title="ZEQORO", version="0.1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=COOKIE_SECURE, max_age=60*60*24*3)
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

def available_slots(db: Session, event: EventType, day: date) -> list[dict[str, str]]:
    today = datetime.now(timezone.utc)
    if day < today.astimezone(HOST_TZ).date() or day > (today.astimezone(HOST_TZ) + timedelta(days=event.max_advance_days)).date():
        return []
    availability = db.scalar(select(Availability).where(Availability.weekday == day.weekday()))
    if not availability:
        return []
    low = utc_naive(datetime.combine(day - timedelta(days=1), time.min, tzinfo=HOST_TZ))
    high = utc_naive(datetime.combine(day + timedelta(days=2), time.min, tzinfo=HOST_TZ))
    existing = db.scalars(select(Booking).where(Booking.status == "confirmed", Booking.end_utc >= low, Booking.start_utc <= high)).all()
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
            result.append({"start": start_local.isoformat(), "label": start_local.strftime("%I:%M %p").lstrip("0")})
    return result

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
    email.set_content(f"Hello {guest_name},\n\nYour meeting is confirmed.\n{event_title}\n{local_time(start)} ({TZ_NAME})\n\nCancel: {PUBLIC_BASE_URL}/cancel/{token}\n\n— {HOST_NAME}")
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
    guest_name, guest_email, notes = guest_name.strip(), guest_email.strip(), notes.strip()
    if len(guest_name) < 2 or len(guest_name) > 160 or len(notes) > 2000:
        return public_error(request, "Please enter a valid name (2–160 characters) and a shorter note.")
    try:
        guest_email = validate_email(guest_email, check_deliverability=False).normalized
    except EmailNotValidError:
        return public_error(request, "Please enter a valid email address.")
    try:
        selected = datetime.fromisoformat(start)
        if selected.tzinfo is None:
            raise ValueError()
        selected_day = selected.astimezone(HOST_TZ).date()
    except ValueError:
        return public_error(request, "Please choose a valid appointment time.")
    with SessionLocal() as db:
        # Serialize booking checks+writes for SQLite to prevent two guests
        # taking the same time slot under concurrent requests.
        if engine.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
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
        tasks.add_task(booking_email, booking.guest_email, booking.guest_name, event.title, booking.start_utc, booking.cancel_token)
        return RedirectResponse(f"/confirmed/{booking.cancel_token}", status_code=303)

@app.get("/confirmed/{token}", response_class=HTMLResponse)
def confirmed(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This confirmation link is invalid.", 404)
        return page(request, "confirmed.html", booking=booking)

@app.get("/cancel/{token}", response_class=HTMLResponse)
def cancel_page(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This booking could not be found.", 404)
        return page(request, "cancel.html", booking=booking)

@app.post("/cancel/{token}")
def cancel_booking(token: str, request: Request):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking:
            return public_error(request, "This booking could not be found.", 404)
        if booking.start_utc <= datetime.now(timezone.utc).replace(tzinfo=None):
            return public_error(request, "You cannot cancel a meeting after its start time.")
        booking.status = "cancelled"
        db.commit()
        return RedirectResponse(f"/cancel/{token}", status_code=303)

@app.get("/ics/{token}")
def ics(token: str):
    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.cancel_token == token))
        if not booking or booking.status != "confirmed":
            raise HTTPException(404)
        fmt = lambda d: d.strftime("%Y%m%dT%H%M%SZ")
        def esc(value):
            return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        body = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ZEQORO//EN", "BEGIN:VEVENT",
            f"UID:booking-{booking.id}@zeqoro.local", f"DTSTAMP:{fmt(datetime.now(timezone.utc))}",
            f"DTSTART:{fmt(booking.start_utc)}", f"DTEND:{fmt(booking.end_utc)}",
            f"SUMMARY:{esc(booking.event_type.title + ' with ' + HOST_NAME)}",
            f"DESCRIPTION:{esc('Guest: ' + booking.guest_name)}",
            "END:VEVENT", "END:VCALENDAR", "",
        ])
        return Response(body, media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": "attachment; filename=meeting.ics"})

@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin", 303)
    return page(request, "login.html", error="")

@app.post("/admin/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD):
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
def dashboard(request: Request):
    admin_only(request)
    with SessionLocal() as db:
        events = db.scalars(select(EventType).order_by(EventType.id)).all()
        bookings = db.scalars(select(Booking).order_by(Booking.start_utc.desc()).limit(100)).all()
        schedule = {a.weekday: a for a in db.scalars(select(Availability)).all()}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        upcoming = sum(b.status == "confirmed" and b.start_utc >= now for b in bookings)
        return page(request, "admin.html", events=events, bookings=bookings, schedule=schedule, days=DAYS, now=now, upcoming=upcoming, csrf_token=request.session["csrf"])

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
    return RedirectResponse("/admin#bookings", 303)
