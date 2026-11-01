"""v0.7 regression coverage: database locks, readiness and retry safety."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from test_booking import first_bookable_slot, setup


def test_postgres_serialization_compiles_to_row_lock(setup):
    _, app = setup
    statement = select(app.SchedulingLock).where(app.SchedulingLock.id == 1).with_for_update()
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    with app.SessionLocal() as db:
        assert db.get(app.SchedulingLock, 1).id == 1
        app.write_serialize(db)  # SQLite path remains valid.
        db.rollback()


def test_health_and_readiness(setup, monkeypatch):
    client, app = setup
    assert client.get("/health").status_code == 200
    assert client.get("/ready").json() == {"ready": True}
    class Unavailable:
        def ping(self):
            raise ConnectionError("no redis")
    monkeypatch.setattr(app, "REDIS_URL", "redis://private:6379/0")
    monkeypatch.setattr(app, "redis_client_from_url", lambda *_: Unavailable())
    assert client.get("/ready").status_code == 503


def test_redis_limiter_names_not_raw_ip_and_fails_closed(setup, monkeypatch):
    client, app = setup
    from infra import RedisWindowLimiter

    class ScriptClient:
        def __init__(self):
            self.calls = []
            self.fail = False
        def eval(self, script, num_keys, key, *argv):
            if self.fail:
                raise ConnectionError("unavailable")
            self.calls.append((script, num_keys, key, argv))
            return 1
        def delete(self, key):
            return 1
    redis = ScriptClient()
    limiter = RedisWindowLimiter(redis)
    assert limiter.check("login", "1.2.3.4", 3, 60)
    script, keys, name, args = redis.calls[-1]
    assert keys == 1 and "1.2.3.4" not in name
    assert "ZREMRANGEBYSCORE" in script and "ZCARD" in script
    assert args[0] == 60000 and args[1] == 3
    redis.fail = True
    monkeypatch.setattr(app, "limiter", limiter)
    assert client.post("/admin/login", data={"username": "x", "password": "x"}).status_code == 503


def test_due_google_jobs_and_distributed_worker_lease(setup, monkeypatch):
    client, app = setup
    slot, _ = first_bookable_slot(client)
    with app.SessionLocal() as db:
        event = db.scalar(select(app.EventType).where(app.EventType.slug == "sponsor-meeting"))
        start = datetime.fromisoformat(slot).astimezone(timezone.utc).replace(tzinfo=None)
        booking = app.Booking(event_type_id=event.id, guest_name="Retry Test", guest_email="test@example.com", start_utc=start, end_utc=start + timedelta(minutes=30), status="confirmed", cancel_token="v07-token")
        db.add(booking)
        db.flush()
        bid = booking.id
        db.add(app.GoogleConnection(id=1, encrypted_refresh_token="ciphertext"))
        db.add(app.GoogleEventLink(booking_id=bid, event_id="zqretry007", status="error", updated_utc=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)))
        db.commit()
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    assert bid in worker.due_booking_ids()
    calls = []
    monkeypatch.setattr(worker, "sync_google_booking", lambda booking_id: calls.append(booking_id) or "synced")
    class Lease:
        def __init__(self, allow):
            self.allow = allow
            self.released = []
        def acquire(self, booking_id, ttl=90):
            return "lock-token" if self.allow else None
        def release(self, booking_id, token):
            self.released.append((booking_id, token))
    first = Lease(False)
    assert worker.run_once(first) == {"processed": 0, "skipped": 1, "errors": 0}
    assert not calls
    second = Lease(True)
    assert worker.run_once(second) == {"processed": 1, "skipped": 0, "errors": 0}
    assert calls == [bid]
    assert second.released == [(bid, "lock-token")]
    # Recent links must not be scheduled again until their delay expires.
    with app.SessionLocal() as db:
        link = db.get(app.GoogleEventLink, bid)
        link.updated_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    assert bid not in worker.due_booking_ids()
    sys.modules.pop("worker", None)
