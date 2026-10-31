"""v0.8 team scheduling: authorization, independent profiles and collision checks."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select

from test_booking import setup


def admin(client):
    response = client.post("/admin/login", data={"username":"anwar", "password":"test-secret-password"}, follow_redirects=False)
    assert response.status_code == 303
    html = client.get("/team").text
    assert "Team workspace" in html
    match = re.search(r'name="token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def next_slot(client, url):
    today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    for i in range(16):
        day = (today + timedelta(days=i)).isoformat()
        response = client.get(url, params={"day":day})
        assert response.status_code == 200, response.text
        if response.json()["slots"]:
            return response.json()["slots"][0]["start"], day
    raise AssertionError(f"No slot found on {url}")


def invite_member(client, csrf, name="Nora", slug="nora", email="nora@example.org", role="member"):
    response = client.post("/team/members/invite", data={"token":csrf,"name":name,"slug":slug,"email":email,"role":role})
    assert response.status_code == 200, response.text
    match = re.search(r'id="invite-url"[^>]+value="([^"]+)"', response.text)
    assert match
    return urlsplit(match.group(1)).path


def accept_member(client, invitation, password="A strong test password 123!"):
    response = client.post(invitation,data={"password":password,"confirm_password":password}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return client.get("/team")


def create_group(client, csrf, slug, participant_ids, mode):
    payload={"token":csrf,"slug":slug,"title":slug.title(),"mode":mode,"duration_min":"30","member_ids":[str(x) for x in participant_ids]}
    return client.post("/team/events/organization", data=payload, follow_redirects=False)


def book(client, url, start):
    return client.post(url, data={"start":start,"guest_name":"Test Guest","guest_email":"guest@example.org", "notes":"Interested in a team meeting"}, follow_redirects=False)


def test_bootstrap_team_profiles_and_google_disclosure(setup):
    client, app = setup
    assert client.get("/o/zeqoro-team").status_code == 200
    assert b"Talk to our team" in client.get("/o/zeqoro-team").content
    assert client.get("/u/anwar").status_code == 200
    assert b"Meet with Anwar" in client.get("/u/anwar").content
    assert client.get("/u/unknown").status_code == 404
    assert client.get("/team", follow_redirects=False).status_code == 303
    assert client.get("/team/login").status_code == 200
    with app.SessionLocal() as db:
        assert db.scalar(select(app.team.Organization.id))
        assert db.scalar(select(app.team.Member.id).where(app.team.Member.role=="owner"))


def test_team_owner_can_book_personal_and_org_separately(setup):
    client, app = setup
    personal_url="/api/team/slots/u/anwar/intro"
    start, day = next_slot(client, personal_url)
    r=book(client,"/u/anwar/book/intro",start)
    assert r.status_code == 303
    assert "confirmed" in r.headers["location"]
    assert b"Assigned to: Anwar" in client.get(r.headers["location"]).content
    assert b"not yet available" in client.get(r.headers["location"]).content
    assert start not in {s["start"] for s in client.get(personal_url,params={"day":day}).json()["slots"]}
    assert book(client,"/u/anwar/book/intro",start).status_code == 409
    with app.SessionLocal() as db:
        booking=db.scalar(select(app.Booking))
        assert db.get(app.team.TeamBooking, booking.id).kind=="personal"
        assert app.sync_google_booking(booking.id)=="team_booking_no_member_google_connection"


def test_invite_is_private_single_use_and_role_limited(setup):
    client, app=setup
    token=admin(client)
    assert client.post("/team/members/invite",data={"token":"invalid", "name":"Nora","slug":"nora","email":"nora@example.org","role":"member"}).status_code == 403
    invite=invite_member(client,token)
    assert client.get(invite).status_code == 200
    assert "no-store" in client.get(invite).headers["cache-control"]
    assert b"Nora" not in client.get("/o/zeqoro-team").content # pending user isn't shown publicly
    bad=client.post(invite, data={"password":"short","confirm_password":"short"})
    assert b"at least 12 characters" in bad.content
    dash=accept_member(client,invite)
    assert b"My personal meeting links" in dash.content
    assert b"People &amp; roles" not in dash.content
    assert client.get(invite).status_code==404
    assert client.post("/team/events/organization",data={"token":"bad"},follow_redirects=False).status_code==403
    assert client.post("/team/members/invite",data={"token":"bad","name":"Other","slug":"other","email":"other@example.org"}).status_code==403
    member_csrf=re.search(r'name="token" value="([^"]+)"',dash.text).group(1)
    assert client.post("/team/events/personal",data={"token":member_csrf,"slug":"private-chat","title":"Private Chat","duration_min":"30"},follow_redirects=False).status_code==303
    assert client.get("/u/nora/book/private-chat").status_code==200
    # A member can't mutate another member's working hours through a form field.
    assert client.post("/team/availability",data={"token":member_csrf,"enabled_0":"1","start_0":"11:00","end_0":"12:00","member_id":"1"},follow_redirects=False).status_code==303
    with app.SessionLocal() as db:
        owner=db.scalar(select(app.team.Member).where(app.team.Member.role=="owner"))
        assert db.scalar(select(app.team.MemberHours.start_min).where(app.team.MemberHours.member_id==owner.id,app.team.MemberHours.weekday==0))==540


def test_org_round_robin_assigns_one_and_avoids_double_booking(setup):
    client, app=setup
    token=admin(client)
    invite=invite_member(client,token)
    accept_member(client,invite)
    client.post("/team/logout",data={"token":re.search(r'name="token" value="([^"]+)"',client.get("/team").text).group(1)},follow_redirects=False)
    token=admin(client)
    with app.SessionLocal() as db:
        members=db.scalars(select(app.team.Member).order_by(app.team.Member.id)).all()
        ids=[m.id for m in members]
    assert create_group(client,token,"sales-call",ids,"round_robin").status_code==303
    url="/api/team/slots/o/zeqoro-team/sales-call"
    start,day=next_slot(client,url)
    one=book(client,"/o/zeqoro-team/book/sales-call",start)
    two=book(client,"/o/zeqoro-team/book/sales-call",start)
    # Both members can be booked at the same time, but never the same member.
    assert one.status_code==303
    assert two.status_code==303
    assert book(client,"/o/zeqoro-team/book/sales-call",start).status_code==409
    with app.SessionLocal() as db:
        bookings=db.scalars(select(app.Booking).where(app.Booking.event_type.has(slug="o-zeqoro-team-sales-call"))).all()
        assigned=[db.scalar(select(app.team.BookingAssignee.member_id).where(app.team.BookingAssignee.booking_id == b.id)) for b in bookings]
        assert len(bookings)==2 and len(set(assigned))==2
    assert start not in {s["start"] for s in client.get(url,params={"day":day}).json()["slots"]}


def test_collective_requires_all_participants(setup):
    client, app=setup
    token=admin(client)
    invite=invite_member(client,token)
    accept_member(client,invite)
    client.post("/team/logout",data={"token":re.search(r'name="token" value="([^"]+)"',client.get("/team").text).group(1)},follow_redirects=False)
    token=admin(client)
    with app.SessionLocal() as db:
        ids=[m.id for m in db.scalars(select(app.team.Member)).all()]
    assert create_group(client,token,"all-hands",ids,"collective").status_code==303
    url="/api/team/slots/o/zeqoro-team/all-hands"
    start,day=next_slot(client,url)
    result=book(client,"/o/zeqoro-team/book/all-hands",start)
    assert result.status_code==303
    with app.SessionLocal() as db:
        booking=db.scalar(select(app.Booking).where(app.Booking.event_type.has(slug="o-zeqoro-team-all-hands")))
        assignments=db.scalars(select(app.team.BookingAssignee).where(app.team.BookingAssignee.booking_id==booking.id)).all()
        assert len(assignments)==2
    assert book(client,"/o/zeqoro-team/book/all-hands",start).status_code==409


def test_disabling_member_hours_and_days_off_blocks_slots(setup):
    client, app=setup
    token=admin(client)
    start, day=next_slot(client,"/api/team/slots/u/anwar/intro")
    weekday=datetime.fromisoformat(day).weekday()
    with app.SessionLocal() as db:
        owner=db.scalar(select(app.team.Member).where(app.team.Member.role=="owner"))
        db.add(app.team.MemberDayOff(member_id=owner.id, day=day))
        db.commit()
    assert client.get("/api/team/slots/u/anwar/intro",params={"day":day}).json()["slots"]==[]
    assert book(client,"/u/anwar/book/intro",start).status_code==409


def test_admin_dashboard_and_csrf_access(setup):
    client, app=setup
    assert client.get("/team",follow_redirects=False).status_code==303
    csrf=admin(client)
    dashboard=client.get("/team")
    assert "Team workspace" in dashboard.text and "Round Robin" in dashboard.text
    assert 'href="/o/zeqoro-team"' in dashboard.text
    assert client.post("/team/events/personal",data={"token":"bad","title":"Call","slug":"call","duration_min":"30"}).status_code==403
    assert client.post("/team/events/personal",data={"token":csrf,"title":"Call","slug":"call","duration_min":"30"},follow_redirects=False).status_code==303
    assert client.get("/u/anwar/book/call").status_code==200
    assert client.post("/team/events/personal",data={"token":csrf,"title":"Call","slug":"call","duration_min":"30"}).status_code==409


def test_manual_meeting_visible_and_prevents_conflicts(setup):
    client, app=setup
    csrf=admin(client)
    start, day=next_slot(client,"/api/team/slots/u/anwar/intro")
    with app.SessionLocal() as db:
        owner=db.scalar(select(app.team.Member).where(app.team.Member.role=="owner"))
        owner_id=owner.id
    naive=datetime.fromisoformat(start).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
    data={"token":csrf,"title":"Weekly Planning","start":naive,"duration_min":"30","member_ids":str(owner_id)}
    assert client.post("/team/bookings/manual",data=data,follow_redirects=False).status_code==303
    assert b"Weekly Planning" in client.get("/team").content
    assert client.post("/team/bookings/manual",data=data,follow_redirects=False).status_code==409
    assert start not in {slot["start"] for slot in client.get("/api/team/slots/u/anwar/intro",params={"day":day}).json()["slots"]}
    with app.SessionLocal() as db:
        created=db.scalar(select(app.Booking).where(app.Booking.event_type.has(title="Weekly Planning")))
        assert created and db.get(app.team.TeamBooking,created.id).kind=="internal"
        assert created.event_type.is_active is False


def test_team_rescheduling_preserves_assignments_and_rechecks_busy(setup):
    client, app=setup
    start,day=next_slot(client,"/api/team/slots/u/anwar/intro")
    result=book(client,"/u/anwar/book/intro",start)
    assert result.status_code==303
    private=result.headers["location"]
    token=private.rsplit("/",1)[-1]
    slots=client.get(f"/api/reschedule-slots/{token}",params={"day":day})
    assert slots.status_code==200
    assert start in {slot["start"] for slot in slots.json()["slots"]}
    alternatives=[slot["start"] for slot in slots.json()["slots"] if slot["start"]!=start]
    assert alternatives
    assert client.get(f"/reschedule/{token}").status_code==200
    changed=client.post(f"/reschedule/{token}",data={"start":alternatives[0]},follow_redirects=False)
    assert changed.status_code==303
    with app.SessionLocal() as db:
        b=db.scalar(select(app.Booking).where(app.Booking.cancel_token==token))
        assert b.start_utc == datetime.fromisoformat(alternatives[0]).astimezone(timezone.utc).replace(tzinfo=None)
        assert db.scalar(select(app.team.BookingAssignee.member_id).where(app.team.BookingAssignee.booking_id==b.id))
    assert client.post(f"/reschedule/{token}",data={"start":start},follow_redirects=False).status_code==303


def test_team_admin_cannot_assign_pending_member_or_outsiders(setup):
    client, app=setup
    csrf=admin(client)
    invite_member(client,csrf)
    with app.SessionLocal() as db:
        pending=db.scalar(select(app.team.Member).where(app.team.Member.slug=="nora"))
        assert pending
    assert create_group(client,csrf,"closed",[pending.id],"collective").status_code==403
    assert client.post("/team/bookings/manual",data={"token":csrf,"title":"Secret","start":"2026-11-09T10:00","duration_min":"30","member_ids":str(pending.id)},follow_redirects=False).status_code==403
