import importlib
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_SECRET", "testing-secret-that-is-long-enough-123")
    monkeypatch.setenv("ADMIN_USER", "anwar")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-secret-password")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("HOST_TZ", "Africa/Cairo")
    monkeypatch.setenv("SMTP_HOST", "")
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    with TestClient(module.app) as client:
        yield client, module
    module.engine.dispose()
    sys.modules.pop("app", None)


def first_bookable_slot(client):
    local_today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    for offset in range(14):
        day = (local_today + timedelta(days=offset)).isoformat()
        res = client.get("/api/slots/sponsor-meeting", params={"day": day})
        assert res.status_code == 200
        if res.json()["slots"]:
            return res.json()["slots"][0]["start"], day
    raise AssertionError("No bookable slots in next 14 days")


def test_public_pages_and_slot_validation(setup):
    client, _ = setup
    assert client.get("/").status_code == 200
    assert b"Sponsor Discussion" in client.get("/").content
    assert client.get("/book/sponsor-meeting").status_code == 200
    assert client.get("/book/invalid").status_code == 404
    assert client.get("/api/slots/sponsor-meeting?day=not-a-date").status_code == 400
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_booking_conflict_cancel_and_ics(setup):
    client, module = setup
    slot, day = first_bookable_slot(client)
    payload = {"guest_name": "Test Guest", "guest_email": "test@example.com", "start": slot, "notes": "A collaboration"}
    res = client.post("/book/sponsor-meeting", data=payload, follow_redirects=False)
    assert res.status_code == 303
    private_path = res.headers["location"]
    token = private_path.split("/")[-1]
    assert b"Test Guest" in client.get(private_path).content
    ics = client.get(f"/ics/{token}")
    assert ics.status_code == 200
    assert "BEGIN:VCALENDAR" in ics.text
    assert "DTSTART:" in ics.text
    duplicate = client.post("/book/sponsor-meeting", data=payload, follow_redirects=False)
    assert duplicate.status_code == 409
    # A different meeting type cannot overlap an existing booking.
    other = client.get("/api/slots/quick-call", params={"day": day})
    assert slot not in {s["start"] for s in other.json()["slots"]}
    cancelled = client.post(f"/cancel/{token}", follow_redirects=False)
    assert cancelled.status_code == 303
    assert b"Booking cancelled" in client.get(f"/cancel/{token}").content
    assert client.get(f"/ics/{token}").status_code == 404
    again = client.post("/book/sponsor-meeting", data=payload, follow_redirects=False)
    assert again.status_code == 303


def test_admin_login_and_availability_csrf(setup):
    client, module = setup
    r = client.post("/admin/login", data={"username": "anwar", "password": "wrong"})
    assert b"Invalid username" in r.content
    r = client.post("/admin/login", data={"username": "anwar", "password": "test-secret-password"}, follow_redirects=False)
    assert r.status_code == 303
    dash = client.get("/admin")
    assert b"Calendar overview" in dash.content
    with module.SessionLocal() as db:
        assert db.query(module.Availability).count() == 5
    token = client.cookies.get("session")
    assert token
    assert client.post("/admin/availability", data={"token": "bad"}).status_code == 403
    import re
    csrf_token = re.search(r'name="token" value="([^"]+)"', dash.text).group(1)
    data = {"token": csrf_token, "enabled_0": "1", "start_0": "10:00", "end_0": "18:00"}
    save = client.post("/admin/availability", data=data, follow_redirects=False)
    assert save.status_code == 303
    with module.SessionLocal() as db:
        hours = db.query(module.Availability).all()
        assert len(hours) == 1
        assert hours[0].start_min == 600
    r = client.post("/admin/events", data={"token": csrf_token, "slug": "new-chat", "title": "New Chat", "description": "Test", "duration_min": "15"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/book/new-chat").status_code == 200
