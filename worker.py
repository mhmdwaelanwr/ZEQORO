"""ZEQORO automatic Google Calendar synchronization retry worker.

Runs separately from the web container. Leases prevent two workers in the same
Redis deployment from retrying a booking simultaneously; the stable Google
Calendar event ID makes provider-side event creation idempotent.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app import Booking, GoogleConnection, GoogleEventLink, SessionLocal, sync_google_booking
from infra import RedisLease, redis_client_from_url

LOG = logging.getLogger("zeqoro.worker")
BACKOFF_SECONDS = {"error": 600, "pending": 90, "pending_meet": 180}


def due_booking_ids(now: datetime | None = None, limit: int = 20) -> list[int]:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        if not db.get(GoogleConnection, 1):
            return []
        # The query is bounded; extend to a durable claim queue for huge datasets.
        rows = db.scalars(select(GoogleEventLink).where(
            GoogleEventLink.status.in_(tuple(BACKOFF_SECONDS))
        ).order_by(GoogleEventLink.updated_utc.asc()).limit(250)).all()
        ids = []
        for link in rows:
            delay = BACKOFF_SECONDS[link.status]
            if link.updated_utc and link.updated_utc > now - timedelta(seconds=delay):
                continue
            booking = db.get(Booking, link.booking_id)
            if booking and (booking.status != "confirmed" or booking.end_utc > now):
                ids.append(link.booking_id)
                if len(ids) >= limit:
                    break
        return ids


def due_team_booking_ids(now: datetime | None = None, limit: int = 20) -> list[int]:
    """Bounded retry queue for team Google events, including newly linked members."""
    import team_google
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    retry_after = {"error": 600, "pending": 90, "pending_meet": 180, "waiting_account": 180}
    with SessionLocal() as db:
        rows = db.scalars(select(team_google.TeamGoogleEventLink).where(
            team_google.TeamGoogleEventLink.status.in_(tuple(retry_after))
        ).order_by(team_google.TeamGoogleEventLink.updated_utc).limit(250)).all()
        due = []
        for link in rows:
            if link.updated_utc and link.updated_utc > now - timedelta(seconds=retry_after[link.status]):
                continue
            booking = db.get(Booking, link.booking_id)
            if not booking or (booking.status == "confirmed" and booking.end_utc <= now):
                continue
            if link.status == "waiting_account":
                # Don't repeatedly invoke Google when no team host has connected.
                import team
                member_ids = list(db.scalars(select(team.BookingAssignee.member_id).where(
                    team.BookingAssignee.booking_id == link.booking_id)).all())
                if link.organizer_member_id is not None:
                    member_ids = [link.organizer_member_id]
                if not member_ids or not db.scalar(select(team_google.MemberGoogleConnection.member_id).where(
                    team_google.MemberGoogleConnection.member_id.in_(member_ids)).limit(1)):
                    continue
            due.append(link.booking_id)
            if len(due) >= limit:
                break
        return due


def run_once(lease: RedisLease | None = None) -> dict[str, int]:
    ids = due_booking_ids()
    counts = {"processed": 0, "skipped": 0, "errors": 0}
    for booking_id in ids:
        lock = None
        try:
            if lease:
                lock = lease.acquire(booking_id)
                if not lock:
                    counts["skipped"] += 1
                    continue
            status = sync_google_booking(booking_id)
            counts["processed"] += 1
            if status == "error":
                counts["errors"] += 1
        except Exception:
            # Do not include provider tokens, booking details or guest PII in logs.
            LOG.exception("Google synchronization retry failed for booking id=%s", booking_id)
            counts["errors"] += 1
        finally:
            if lock and lease:
                try:
                    lease.release(booking_id, lock)
                except Exception:
                    LOG.exception("Could not release sync lease for booking id=%s", booking_id)
    # Separate leases prevent legacy and team sync jobs for the same booking id colliding.
    import team_google
    for booking_id in due_team_booking_ids():
        key = f"team-{booking_id}"
        lock = None
        try:
            if lease:
                lock = lease.acquire(key)
                if not lock:
                    counts["skipped"] += 1
                    continue
            status = team_google.sync_team_booking(booking_id)
            counts["processed"] += 1
            if status == "error":
                counts["errors"] += 1
        except Exception:
            LOG.exception("Team Google sync retry failed for booking id=%s", booking_id)
            counts["errors"] += 1
        finally:
            if lock and lease:
                try:
                    lease.release(key, lock)
                except Exception:
                    LOG.exception("Could not release team sync lease for booking id=%s", booking_id)
    import team_calendar
    push = team_calendar.run_due_watches(lease=lease)
    if any(push.values()):
        counts.update(push)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise SystemExit("Worker needs REDIS_URL so multiple workers cannot process a booking simultaneously")
    client = redis_client_from_url(url)
    # Require a healthy Redis connection before attempting any provider writes.
    client.ping()
    lease = RedisLease(client)
    period = max(30, int(os.getenv("SYNC_POLL_SECONDS", "60")))
    while True:
        try:
            stats = run_once(lease)
            LOG.info("Calendar worker stats: %s", stats)
        except Exception:
            LOG.exception("Google sync retry poll failed")
        time.sleep(period)


if __name__ == "__main__":
    main()
