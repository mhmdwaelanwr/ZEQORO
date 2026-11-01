"""Team Google integration: mocked provider calls, never touches real Google accounts."""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from sqlalchemy import select
from cryptography.fernet import Fernet
from test_booking import setup
from test_team import next_slot, book

def configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/admin/google/callback")
    monkeypatch.setenv("GOOGLE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

def owner(client):
    assert client.post("/admin/login", data={"username":"anwar","password":"test-secret-password"},follow_redirects=False).status_code == 303
    return re.search(r'name="token" value="([^"]+)"',client.get("/team").text).group(1)

def linked(app, member_id, email, subject):
    with app.SessionLocal() as db:
        db.add(app.team_google.MemberGoogleConnection(member_id=member_id,google_subject=subject,
            verified_email=email,encrypted_refresh_token=app.google.encrypt_secret(
            "private-"+subject,app.team_google.team_settings())))
        db.commit()

def second_member(app):
    with app.SessionLocal() as db:
        member=app.team.Member(organization_id=db.scalar(select(app.team.Organization.id)),
            name="Nora",slug="nora",email="nora@example.org",active=True,role="member",
            password_hash=app.team.password_hash("a long test password!"))
        db.add(member);db.flush()
        app.team._default_hours(db,member.id)
        event=app.EventType(slug="p-nora-intro",title="Meet Nora",duration_min=30)
        db.add(event);db.flush()
        db.add(app.team.PersonalEvent(member_id=member.id,event_type_id=event.id))
        db.commit()
        return member.id

def test_team_oauth_is_member_bound_and_token_is_encrypted(setup,monkeypatch):
    client,app=setup
    configured(monkeypatch)
    assert client.get("/team/google",follow_redirects=False).status_code==303
    csrf=owner(client)
    assert client.post("/team/google/connect",data={"token":"wrong"}).status_code==403
    response=client.post("/team/google/connect",data={"token":csrf},follow_redirects=False)
    assert response.status_code==303
    query=parse_qs(urlsplit(response.headers["location"]).query)
    assert query["redirect_uri"]==["http://localhost:8000/team/google/callback"]
    assert "openid" in query["scope"][0] and "calendar.freebusy" in query["scope"][0]
    assert client.get("/team/google/callback",params={"state":"wrong","code":"x"}).status_code==403
    response=client.post("/team/google/connect",data={"token":csrf},follow_redirects=False)
    state=parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    monkeypatch.setattr(app.google,"exchange_code",lambda *args:{"access_token":"access","refresh_token":"a-secret-token",
        "scope":" ".join(app.team_google.SCOPES)})
    monkeypatch.setattr(app.google,"account_identity",lambda token:{"email":"owner@gmail.com","sub":"owner-google-sub"})
    assert client.get("/team/google/callback",params={"state":state,"code":"fake"},follow_redirects=False).status_code==303
    with app.SessionLocal() as db:
        owner_id=db.scalar(select(app.team.Member.id).where(app.team.Member.role=="owner"))
        connection=db.get(app.team_google.MemberGoogleConnection,owner_id)
        assert connection.verified_email=="owner@gmail.com"
        assert "a-secret-token" not in connection.encrypted_refresh_token
    assert client.post("/team/google/disconnect",data={"token":csrf},follow_redirects=False).status_code==303
    with app.SessionLocal() as db:
        assert db.scalar(select(app.team_google.MemberGoogleConnection.member_id)) is None

def test_member_freebusy_blocks_slots_and_outages_fail_closed(setup,monkeypatch):
    client,app=setup
    configured(monkeypatch)
    member_id=second_member(app)
    start,day=next_slot(client,"/api/team/slots/u/nora/intro")
    linked(app,member_id,"nora@gmail.com","nora-google-sub")
    monkeypatch.setattr(app.google,"access_token",lambda *args:"token")
    instant=datetime.fromisoformat(start).astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(app.google,"freebusy",lambda *args:[(instant,instant+timedelta(minutes=30))])
    slots=client.get("/api/team/slots/u/nora/intro",params={"day":day}).json()["slots"]
    assert start not in {slot["start"] for slot in slots}
    monkeypatch.setattr(app.google,"freebusy",lambda *args:(_ for _ in ()).throw(app.google.GoogleIntegrationError("offline")))
    assert client.get("/api/team/slots/u/nora/intro",params={"day":day}).status_code==503

def test_collective_creates_single_google_meet_and_updates(setup,monkeypatch):
    client,app=setup
    configured(monkeypatch)
    second=second_member(app)
    with app.SessionLocal() as db:
        owner_id=db.scalar(select(app.team.Member.id).where(app.team.Member.role=="owner"))
        event=app.EventType(slug="o-zeqoro-team-together",title="Work together",duration_min=30)
        db.add(event);db.flush()
        group=app.team.GroupEvent(organization_id=db.scalar(select(app.team.Organization.id)),event_type_id=event.id,mode="collective")
        db.add(group);db.flush()
        db.add_all([app.team.GroupParticipant(group_event_id=group.id,member_id=i) for i in (owner_id,second)])
        db.commit()
    linked(app,owner_id,"owner@gmail.com","owner-sub")
    linked(app,second,"nora@gmail.com","nora-sub")
    monkeypatch.setattr(app.google,"access_token",lambda *args:"token")
    monkeypatch.setattr(app.google,"freebusy",lambda *args:[])
    added,changed,removed=[],[],[]
    meet={"hangoutLink":"https://meet.google.com/abc-team-room"}
    monkeypatch.setattr(app.google,"insert_event",lambda token,cal,data:added.append(data) or meet)
    monkeypatch.setattr(app.google,"get_event",lambda *args:meet)
    monkeypatch.setattr(app.google,"patch_event",lambda token,cal,eid,data:changed.append(data) or meet)
    monkeypatch.setattr(app.google,"delete_event",lambda token,cal,eid:removed.append(eid))
    start,_=next_slot(client,"/api/team/slots/o/zeqoro-team/together")
    resp=book(client,"/o/zeqoro-team/book/together",start)
    assert resp.status_code==303 and len(added)==1
    assert {x["email"] for x in added[0]["attendees"]}=={"nora@gmail.com","guest@example.org"}
    assert b"meet.google.com" in client.get(resp.headers["location"]).content
    with app.SessionLocal() as db:
        bid=db.scalar(select(app.team_google.TeamGoogleEventLink.booking_id))
    assert app.team_google.sync_team_booking(bid)=="synced"
    assert len(added)==1
    with app.SessionLocal() as db:
        b=db.get(app.Booking,bid)
        b.start_utc+=timedelta(days=7);b.end_utc+=timedelta(days=7);db.commit()
    assert app.team_google.sync_team_booking(bid)=="synced"
    assert len(changed)==1 and "conferenceData" not in changed[0]
    with app.SessionLocal() as db:
        db.get(app.Booking,bid).status="cancelled";db.commit()
    assert app.team_google.sync_team_booking(bid)=="deleted" and len(removed)==1

def test_missing_calendar_keeps_booking_pending_not_fake_meet(setup):
    client,app=setup
    start,_=next_slot(client,"/api/team/slots/u/anwar/intro")
    resp=book(client,"/u/anwar/book/intro",start)
    assert resp.status_code==303
    with app.SessionLocal() as db:
        link=db.scalar(select(app.team_google.TeamGoogleEventLink))
        assert link.status=="waiting_account" and link.organizer_member_id is None
    assert b"connect Google Calendar" in client.get(resp.headers["location"]).content


def test_organizer_cannot_silently_switch_google_accounts(setup, monkeypatch):
    client,app=setup
    configured(monkeypatch)
    with app.SessionLocal() as db:
        oid=db.scalar(select(app.team.Member.id).where(app.team.Member.role=="owner"))
    linked(app,oid,"owner@gmail.com","original-google-subject")
    monkeypatch.setattr(app.google,"access_token",lambda *args:"token")
    monkeypatch.setattr(app.google,"freebusy",lambda *args:[])
    monkeypatch.setattr(app.google,"insert_event",lambda *args:{"hangoutLink":"https://meet.google.com/test-room"})
    start,_=next_slot(client,"/api/team/slots/u/anwar/intro")
    assert book(client,"/u/anwar/book/intro",start).status_code==303
    with app.SessionLocal() as db:
        conn=db.get(app.team_google.MemberGoogleConnection,oid)
        conn.google_subject="another-google-account"
        conn.verified_email="new@gmail.com"
        bid=db.scalar(select(app.team_google.TeamGoogleEventLink.booking_id))
        db.commit()
    monkeypatch.setattr(app.google,"insert_event",lambda *args:(_ for _ in ()).throw(AssertionError("must not create duplicate")))
    assert app.team_google.sync_team_booking(bid)=="account_mismatch"


def test_worker_retries_waiting_team_bookings_after_connection(setup,monkeypatch):
    import importlib,sys
    client,app=setup
    configured(monkeypatch)
    start,_=next_slot(client,"/api/team/slots/u/anwar/intro")
    assert book(client,"/u/anwar/book/intro",start).status_code==303
    with app.SessionLocal() as db:
        row=db.scalar(select(app.team_google.TeamGoogleEventLink))
        row.updated_utc-=timedelta(minutes=20)
        bid=row.booking_id
        oid=db.scalar(select(app.team.Member.id).where(app.team.Member.role=="owner"))
        db.commit()
    sys.modules.pop("worker",None)
    worker=importlib.import_module("worker")
    assert worker.due_team_booking_ids()==[]
    linked(app,oid,"owner@gmail.com","owner-google-subject")
    assert worker.due_team_booking_ids()==[bid]
    calls=[]
    monkeypatch.setattr(worker,"due_booking_ids",lambda:[])
    monkeypatch.setattr(app.team_google,"sync_team_booking",lambda x:calls.append(x) or "synced")
    assert worker.run_once()=={"processed":1,"skipped":0,"errors":0}
    assert calls==[bid]
    sys.modules.pop("worker",None)
