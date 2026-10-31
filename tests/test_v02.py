"""Regression tests for ZEQORO v0.2 scheduling flows and privacy."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from test_booking import setup, first_bookable_slot


def admin_session(client):
    response = client.post('/admin/login', data={'username': 'anwar', 'password': 'test-secret-password'}, follow_redirects=False)
    assert response.status_code == 303
    html = client.get('/admin').text
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_reschedule_keeps_private_token_and_frees_original_slot(setup):
    client, _ = setup
    first_slot, first_day = first_bookable_slot(client)
    created = client.post('/book/sponsor-meeting', data={'guest_name': 'Alice Example', 'guest_email': 'alice@example.com', 'start': first_slot}, follow_redirects=False)
    assert created.status_code == 303
    token = created.headers['location'].split('/')[-1]
    assert client.get(f'/reschedule/{token}').status_code == 200
    private_slots = client.get(f'/api/reschedule-slots/{token}', params={'day': first_day}).json()['slots']
    assert first_slot in {slot['start'] for slot in private_slots}
    next_slot = next((s['start'] for s in private_slots if datetime.fromisoformat(s['start']) - datetime.fromisoformat(first_slot) >= timedelta(minutes=60)), None)
    assert next_slot
    changed = client.post(f'/reschedule/{token}', data={'start': next_slot}, follow_redirects=False)
    assert changed.status_code == 303
    assert changed.headers['location'] == f'/confirmed/{token}'
    regular_slots = client.get('/api/slots/sponsor-meeting', params={'day': first_day}).json()['slots']
    assert first_slot in {s['start'] for s in regular_slots}
    assert next_slot not in {s['start'] for s in regular_slots}
    assert client.get(f'/ics/{token}').status_code == 200


def test_reschedule_rejects_conflicting_or_cancelled_booking(setup):
    client, _ = setup
    first_slot, first_day = first_bookable_slot(client)
    slot_list = client.get('/api/slots/sponsor-meeting', params={'day': first_day}).json()['slots']
    next_slot = next(s['start'] for s in slot_list if datetime.fromisoformat(s['start']) - datetime.fromisoformat(first_slot) >= timedelta(minutes=60))
    first = client.post('/book/sponsor-meeting', data={'guest_name': 'Alice', 'guest_email': 'alice@example.com', 'start': first_slot}, follow_redirects=False)
    second = client.post('/book/sponsor-meeting', data={'guest_name': 'Bob', 'guest_email': 'bob@example.com', 'start': next_slot}, follow_redirects=False)
    token = first.headers['location'].split('/')[-1]
    assert second.status_code == 303
    assert client.post(f'/reschedule/{token}', data={'start': next_slot}).status_code == 409
    assert client.post(f'/cancel/{token}', follow_redirects=False).status_code == 303
    assert client.get(f'/reschedule/{token}').status_code == 409
    assert client.post(f'/reschedule/{token}', data={'start': first_slot}).status_code == 409


def test_day_blocking_and_admin_auth(setup):
    client, _ = setup
    start, day = first_bookable_slot(client)
    assert client.get('/admin/bookings.csv', follow_redirects=False).status_code == 303
    assert client.post('/admin/blocked-dates', data={'token': 'bad', 'day': day}).status_code == 403
    csrf = admin_session(client)
    blocked = client.post('/admin/blocked-dates', data={'token': csrf, 'day': day, 'reason': 'Holiday'}, follow_redirects=False)
    assert blocked.status_code == 303
    assert client.get('/api/slots/sponsor-meeting', params={'day': day}).json()['slots'] == []
    duplicate = client.post('/admin/blocked-dates', data={'token': csrf, 'day': day})
    assert duplicate.status_code == 409
    dashboard = client.get('/admin')
    assert 'Holiday' in dashboard.text
    block_id = int(re.search(r'action="/admin/blocked-dates/(\d+)/remove"', dashboard.text).group(1))
    assert client.post(f'/admin/blocked-dates/{block_id}/remove', data={'token': csrf}, follow_redirects=False).status_code == 303
    assert start in {s['start'] for s in client.get('/api/slots/sponsor-meeting', params={'day': day}).json()['slots']}


def test_event_edits_update_slot_rules_and_export(setup):
    client, module = setup
    csrf = admin_session(client)
    with module.SessionLocal() as db:
        event = db.scalar(module.select(module.EventType).where(module.EventType.slug == 'sponsor-meeting'))
        event_id = event.id
    updated = client.post(f'/admin/events/{event_id}/edit', data={
        'token': csrf, 'title': 'Partnership session', 'description': 'Discuss projects',
        'duration_min': 45, 'buffer_before_min': 15, 'buffer_after_min': 20,
        'min_notice_hours': 0, 'max_advance_days': 90,
    }, follow_redirects=False)
    assert updated.status_code == 303
    assert b'Partnership session' in client.get('/').content
    assert client.post(f'/admin/events/{event_id}/edit', data={
        'token': csrf, 'title': 'X', 'duration_min': 10,
    }).status_code == 400
    slot, _ = first_bookable_slot(client)
    res = client.post('/book/sponsor-meeting', data={'guest_name': '=1+1', 'guest_email': 'csv@example.com', 'start': slot, 'notes': '+cmd'}, follow_redirects=False)
    assert res.status_code == 303
    exported = client.get('/admin/bookings.csv')
    assert exported.status_code == 200
    assert exported.headers['content-type'].startswith('text/csv')
    assert "'=1+1" in exported.text
    assert "'+cmd" in exported.text
    assert 'Partnership session' in exported.text


def test_private_pages_have_protective_headers(setup):
    client, _ = setup
    response = client.get('/admin/login')
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store, private'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert response.headers['x-frame-options'] == 'DENY'
