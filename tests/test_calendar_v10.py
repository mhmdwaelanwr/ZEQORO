"""v1.0 multi-calendar and push tests use mock Google; no live OAuth or API calls."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from test_booking import setup
try:  # v0.9 has two compatible helper naming conventions across branches.
    from test_team_google import added_member, configure, link, owner_login
except ImportError:
    from test_team_google import second_member as added_member, configured as configure
    from test_team_google import linked as link, owner as owner_login
from test_team import next_slot, book


def owner_id(app):
    with app.SessionLocal() as db:
        return db.scalar(select(app.team.Member.id).where(app.team.Member.role == "owner"))


def test_calendar_discovery_multi_select_and_live_freebusy(setup, monkeypatch):
    client, app = setup
    configure(monkeypatch)
    mid = owner_id(app)
    link(app, mid, "owner@gmail.com", "sub-owner")
    csrf = owner_login(client)
    from team_calendar import MemberCalendar
    assert b"Calendars to check" in client.get("/team/google").content
    options = [
        {"id": "owner@gmail.com", "name": "Personal", "readable": True, "primary": True},
        {"id": "work@group.calendar.google.com", "name": "Work", "readable": True},
        {"id": "unavailable", "name": "Private", "readable": False},
    ]
    monkeypatch.setattr(app.team_calendar, "list_calendars", lambda token: options)
    monkeypatch.setattr(app.google, "access_token", lambda *args: "test-access")
    assert client.post("/team/google/calendars/refresh", data={"token": "bad"}).status_code == 403
    assert client.post("/team/google/calendars/refresh", data={"token": csrf}, follow_redirects=False).status_code == 303
    with app.SessionLocal() as db:
        rows = db.scalars(select(MemberCalendar).where(MemberCalendar.member_id == mid)).all()
        assert len(rows) == 3
        assert {r.calendar_id for r in rows if r.selected} == {"owner@gmail.com"}
    selected = ["owner@gmail.com", "work@group.calendar.google.com"]
    assert client.post("/team/google/calendars/select", data={"token": csrf, "calendar_ids": selected},
                       follow_redirects=False).status_code == 303
    assert client.post("/team/google/calendars/select", data={"token": csrf, "calendar_ids": ["unavailable"]}).status_code == 400
    assert client.post("/team/google/calendars/select", data={"token": csrf, "calendar_ids": []}).status_code == 400
    monkeypatch.setattr(app.google, "access_token", lambda *args: "test-access")
    seen = []
    def busy(token, calendar_id, start, end):
        seen.append(calendar_id)
        return [(datetime.now(timezone.utc).replace(tzinfo=None), datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))] if calendar_id.endswith("@group.calendar.google.com") else []
    monkeypatch.setattr(app.google, "freebusy", busy)
    with app.SessionLocal() as db:
        today = datetime.now(app.HOST_TZ).date()
        result = app.team_calendar.multi_busy(db, mid, today)
        assert result
        assert seen == selected
    monkeypatch.setattr(app.google, "freebusy", lambda *args: (_ for _ in ()).throw(app.google.GoogleIntegrationError("offline")))
    with app.SessionLocal() as db:
        with pytest.raises(Exception) as err:
            app.team_calendar.multi_busy(db, mid, datetime.now(app.HOST_TZ).date())
        assert getattr(err.value, "status_code", None) == 503


def test_calendar_selection_rejects_another_members_ids(setup, monkeypatch):
    client, app = setup
    configure(monkeypatch)
    owner_login(client)
    csrf = re.search(r'name="token" value="([^"]+)"',client.get("/team/google").text).group(1)
    member2 = added_member(app)
    with app.SessionLocal() as db:
        db.add(app.team_calendar.MemberCalendar(member_id=member2, calendar_id="somebody@example.org",
                            display_name="Someone else's work", selected=True, readable=True))
        db.commit()
    assert client.post("/team/google/calendars/select", data={"token": csrf, "calendar_ids": ["somebody@example.org"]}).status_code == 400


def test_organizer_preference_permissions_and_pinned_booking(setup, monkeypatch):
    client, app = setup
    configure(monkeypatch)
    member_id = added_member(app)
    oid = owner_id(app)
    with app.SessionLocal() as db:
        event = app.EventType(slug="o-zeqoro-team-standup", title="Standup", duration_min=30)
        db.add(event)
        db.flush()
        org_id = db.scalar(select(app.team.Organization.id))
        group = app.team.GroupEvent(event_type_id=event.id, organization_id=org_id, mode="collective")
        db.add(group)
        db.flush()
        group_id = group.id
        db.add_all([app.team.GroupParticipant(group_event_id=group_id, member_id=x) for x in (oid,member_id)])
        db.commit()
    csrf = owner_login(client)
    action=f"/team/google/groups/{group_id}/organizer"
    assert client.post(action, data={"token": "bad", "member_id": member_id}).status_code == 403
    assert client.post(action, data={"token": csrf, "member_id": 999999}).status_code == 403
    assert client.post(action, data={"token": csrf, "member_id": member_id}, follow_redirects=False).status_code == 303
    link(app, oid, "owner@gmail.com", "sub-owner")
    link(app, member_id, "team@gmail.com", "sub-team")
    monkeypatch.setattr(app.google, "access_token", lambda *args: "token")
    monkeypatch.setattr(app.google, "freebusy", lambda *args: [])
    monkeypatch.setattr(app.google, "insert_event", lambda *args: {"hangoutLink":"https://meet.google.com/abc-defg-hij"})
    slot, _ = next_slot(client, "/api/team/slots/o/zeqoro-team/standup")
    result = book(client,"/o/zeqoro-team/book/standup",slot)
    assert result.status_code == 303
    with app.SessionLocal() as db:
        bid = db.scalar(select(app.team.TeamBooking.booking_id).where(app.team.TeamBooking.kind == "collective"))
        calendar_link = db.get(app.team_google.TeamGoogleEventLink, bid)
        assert calendar_link.organizer_member_id == member_id
        assert calendar_link.organizer_google_subject == "sub-team"
        assert calendar_link.status == "synced"
    # Reconfiguring for future bookings cannot transfer the event already created.
    assert client.post(action, data={"token": csrf,"member_id": oid},follow_redirects=False).status_code==303
    with app.SessionLocal() as db:
        assert db.get(app.team_google.TeamGoogleEventLink,bid).organizer_member_id == member_id


def _watch(app, member_id, *, expires=None):
    secret = "very-random-webhook-token"
    with app.SessionLocal() as db:
        watch = app.team_calendar.CalendarWatch(channel_id="watch-123", member_id=member_id,
            calendar_id="primary",resource_id="resource-1",token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            expires_utc=expires or datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(days=5),
            dirty=False,active=True,last_message_number=0)
        db.add(watch)
        db.commit()
    return {"x-goog-channel-id":"watch-123", "x-goog-channel-token":secret,
            "x-goog-resource-id":"resource-1", "x-goog-resource-state":"exists", "x-goog-message-number":"4"}


def test_webhook_authentication_duplicate_and_durable_dirty(setup):
    client, app = setup
    headers=_watch(app,owner_id(app))
    assert client.post("/webhooks/google/calendar",headers={**headers,"x-goog-channel-token":"bad"}).status_code==403
    assert client.post("/webhooks/google/calendar",headers={**headers,"x-goog-resource-id":"wrong"}).status_code==403
    assert client.post("/webhooks/google/calendar",headers={**headers,"x-goog-message-number":"x"}).status_code==403
    assert client.post("/webhooks/google/calendar",headers=headers).status_code==204
    assert client.post("/webhooks/google/calendar",headers=headers).status_code==204
    with app.SessionLocal() as db:
        w=db.get(app.team_calendar.CalendarWatch,"watch-123")
        assert w.last_message_number==4 and w.dirty
    assert client.post("/webhooks/google/calendar",headers={**headers,"x-goog-message-number":"3"}).status_code==204
    with app.SessionLocal() as db:
        assert db.get(app.team_calendar.CalendarWatch,"watch-123").last_message_number==4


def test_webhook_expired_channel_rejected(setup):
    client, app=setup
    headers=_watch(app,owner_id(app),expires=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1))
    assert client.post("/webhooks/google/calendar",headers=headers).status_code==403


def test_watch_registration_requires_https_and_stops_previous(setup,monkeypatch):
    client,app=setup
    configure(monkeypatch)
    oid=owner_id(app)
    link(app,oid,"owner@gmail.com","owner-sub")
    csrf=owner_login(client)
    assert client.post("/team/google/watches/refresh",data={"token":csrf}).status_code==503
    monkeypatch.setattr(app,"PUBLIC_BASE_URL","https://calendar.example.org")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://calendar.example.org/admin/google/callback")
    monkeypatch.setattr(app.google,"access_token",lambda *args:"access")
    calls=[]
    def fake_google(token,method,endpoint,payload):
        calls.append((endpoint,payload))
        return {"resourceId":"resource-123", "expiration":int((datetime.now(timezone.utc)+timedelta(days=6)).timestamp()*1000)}
    monkeypatch.setattr(app.team_calendar,"_google_watch_call",fake_google)
    assert client.post("/team/google/watches/refresh",data={"token":csrf},follow_redirects=False).status_code==303
    with app.SessionLocal() as db:
        first=db.scalar(select(app.team_calendar.CalendarWatch))
        assert first and first.active and first.token_hash
        assert "secret" not in first.token_hash
    assert calls[0][0].endswith("/events/watch")
    assert calls[0][1]["address"]=="https://calendar.example.org/webhooks/google/calendar"
    assert client.post("/team/google/watches/refresh",data={"token":csrf},follow_redirects=False).status_code==303
    assert any(path=="/channels/stop" for path,_ in calls)
    with app.SessionLocal() as db:
        active=db.scalars(select(app.team_calendar.CalendarWatch).where(app.team_calendar.CalendarWatch.active.is_(True))).all()
        assert len(active)==1


def test_worker_delta_cursor_updates_and_external_edit_requires_review(setup, monkeypatch):
    client,app=setup
    configure(monkeypatch)
    oid=owner_id(app)
    link(app,oid,"owner@gmail.com","owner-sub")
    headers=_watch(app,oid)
    with app.SessionLocal() as db:
        event=db.scalar(select(app.EventType).where(app.EventType.slug=="sponsor-meeting"))
        start=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(days=3)
        booking=app.Booking(event_type_id=event.id,guest_name="Guest",guest_email="guest@example.org",
                            start_utc=start,end_utc=start+timedelta(minutes=30),status="confirmed",cancel_token="v10-private")
        db.add(booking)
        db.flush()
        bid=booking.id
        db.add(app.team_google.TeamGoogleEventLink(booking_id=bid,organizer_member_id=oid,
            organizer_google_subject="owner-sub", event_id="zqexternal123",status="synced",
            synced_start_utc=start,synced_end_utc=start+timedelta(minutes=30)))
        db.commit()
    client.post("/webhooks/google/calendar",headers=headers)
    monkeypatch.setattr(app.google,"access_token",lambda *args:"token")
    google_start=start+timedelta(hours=2)
    def fake_delta(token,calendar_id,cursor):
        return ([{"id":"zqexternal123","status":"confirmed","start":{"dateTime":google_start.replace(tzinfo=timezone.utc).isoformat()},
                  "end":{"dateTime":(google_start+timedelta(minutes=30)).replace(tzinfo=timezone.utc).isoformat()}}],"cursor-1",False)
    monkeypatch.setattr(app.team_calendar,"_fetch_delta",fake_delta)
    assert app.team_calendar.process_watch("watch-123") is True
    with app.SessionLocal() as db:
        watched=db.get(app.team_calendar.CalendarWatch,"watch-123")
        linked=db.get(app.team_google.TeamGoogleEventLink,bid)
        assert not watched.dirty and watched.sync_token=="cursor-1"
        assert linked.status=="external_review"
        assert linked.last_error and "outside ZEQORO" in linked.last_error
    client.post("/webhooks/google/calendar",headers={**headers,"x-goog-message-number":"5"})
    monkeypatch.setattr(app.team_calendar,"_fetch_delta",lambda *args: (_ for _ in ()).throw(app.google.GoogleIntegrationError("offline")))
    assert app.team_calendar.process_watch("watch-123") is False
    with app.SessionLocal() as db:
        assert db.get(app.team_calendar.CalendarWatch,"watch-123").dirty


def test_group_organizer_cannot_target_round_robin(setup):
    client,app=setup
    csrf=owner_login(client)
    with app.SessionLocal() as db:
        group=db.scalar(select(app.team.GroupEvent).where(app.team.GroupEvent.mode=="round_robin"))
        group_id=group.id
    assert client.post(f"/team/google/groups/{group_id}/organizer",data={"token":csrf,"member_id":owner_id(app)}).status_code==404
