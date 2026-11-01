"""Google Calendar integration: pure mocks, no real OAuth credentials or network."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import pytest
from cryptography.fernet import Fernet
from test_booking import setup, first_bookable_slot


def enable_google(monkeypatch, app_module):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'http://localhost:8000/admin/google/callback')
    monkeypatch.setenv('GOOGLE_TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    return app_module.google_settings()


def connect_fake(module, settings):
    with module.SessionLocal() as db:
        db.add(module.GoogleConnection(id=1, encrypted_refresh_token=module.google.encrypt_secret('fake-refresh', settings)))
        db.commit()


def login_admin(client):
    r = client.post('/admin/login', data={'username': 'anwar', 'password': 'test-secret-password'}, follow_redirects=False)
    assert r.status_code == 303
    dashboard = client.get('/admin')
    return re.search(r'name="token" value="([^"]+)"', dashboard.text).group(1)


def test_oauth_requires_admin_and_csrf_and_state(setup, monkeypatch):
    client, module = setup
    enable_google(monkeypatch, module)
    assert client.post('/admin/google/connect', data={'token': 'x'}, follow_redirects=False).status_code in {403, 303}
    csrf = login_admin(client)
    assert client.post('/admin/google/connect', data={'token': 'bad'}).status_code == 403
    start = client.post('/admin/google/connect', data={'token': csrf}, follow_redirects=False)
    assert start.status_code == 303
    params = parse_qs(urlparse(start.headers['location']).query)
    assert params['access_type'] == ['offline']
    assert params['code_challenge_method'] == ['S256']
    assert 'calendar.freebusy' in params['scope'][0]
    assert 'calendar.events' in params['scope'][0]
    assert client.get('/admin/google/callback', params={'code': 'x', 'state': 'bad'}).status_code == 403
    # The invalid callback consumes its state; replay must also fail.
    assert client.get('/admin/google/callback', params={'code': 'x', 'state': params['state'][0]}).status_code == 403


def test_oauth_callback_encrypted_and_disconnect_retains_events(setup, monkeypatch):
    client, module = setup
    settings = enable_google(monkeypatch, module)
    csrf = login_admin(client)
    start = client.post('/admin/google/connect', data={'token': csrf}, follow_redirects=False)
    state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
    monkeypatch.setattr(module.google, 'exchange_code', lambda *_: {'access_token': 'fake-access', 'refresh_token': 'confidential', 'scope': ' '.join(module.google.SCOPES)})
    callback = client.get('/admin/google/callback', params={'state': state, 'code': 'once'}, follow_redirects=False)
    assert callback.status_code == 303
    with module.SessionLocal() as db:
        c = db.get(module.GoogleConnection, 1)
        assert c and 'confidential' not in c.encrypted_refresh_token
        assert module.google.decrypt_secret(c.encrypted_refresh_token, settings) == 'confidential'
    assert 'Google Calendar connected successfully.' in client.get('/admin/integrations?message=connected').text
    assert client.post('/admin/google/disconnect', data={'token': csrf}, follow_redirects=False).status_code == 303
    with module.SessionLocal() as db:
        assert db.get(module.GoogleConnection, 1) is None


def test_remote_busy_blocks_slots_and_google_outage_fails_closed(setup, monkeypatch):
    client, module = setup
    slot, day = first_bookable_slot(client)
    settings = enable_google(monkeypatch, module)
    connect_fake(module, settings)
    monkeypatch.setattr(module.google, 'access_token', lambda *_: 'access')
    local = datetime.fromisoformat(slot)
    busy_start = local.astimezone(timezone.utc).replace(tzinfo=None)
    captured = []
    def busy(_token, _calendar, start, end):
        captured.append((start, end))
        return [(busy_start, busy_start + timedelta(minutes=45))]
    monkeypatch.setattr(module.google, 'freebusy', busy)
    blocked = client.get(f'/api/slots/sponsor-meeting?day={day}')
    assert blocked.status_code == 200
    assert slot not in {x['start'] for x in blocked.json()['slots']}
    assert captured
    # An external outage must not expose potentially conflicting slots.
    monkeypatch.setattr(module.google, 'freebusy', lambda *args: (_ for _ in ()).throw(module.google.GoogleIntegrationError('offline')))
    assert client.get(f'/api/slots/sponsor-meeting?day={day}').status_code == 503


def test_booking_create_google_meet_reschedule_and_cancel(setup, monkeypatch):
    client, module = setup
    slot, day = first_bookable_slot(client)
    settings = enable_google(monkeypatch, module)
    connect_fake(module, settings)
    monkeypatch.setattr(module.google, 'access_token', lambda *_: 'access')
    monkeypatch.setattr(module.google, 'freebusy', lambda *_: [])
    created, updated, deleted = [], [], []
    def insert(token, calendar, payload):
        created.append(payload)
        return {'id': payload['id'], 'hangoutLink': 'https://meet.google.com/abc-defg-hij'}
    def patch(token, calendar, event_id, payload):
        updated.append((event_id, payload))
        return {'id': event_id, 'hangoutLink': 'https://meet.google.com/abc-defg-hij'}
    monkeypatch.setattr(module.google, 'insert_event', insert)
    monkeypatch.setattr(module.google, 'patch_event', patch)
    monkeypatch.setattr(module.google, 'delete_event', lambda _t, _c, event_id: deleted.append(event_id))
    monkeypatch.setattr(module.google, 'get_event', lambda _t, _c, event_id: {'id': event_id, 'hangoutLink': 'https://meet.google.com/abc-defg-hij'})
    response = client.post('/book/sponsor-meeting', data={'guest_name': 'Cloud User', 'guest_email': 'cloud@example.com', 'start': slot}, follow_redirects=False)
    assert response.status_code == 303
    token = response.headers['location'].split('/')[-1]
    assert len(created) == 1
    assert created[0]['conferenceData']['createRequest']['conferenceSolutionKey']['type'] == 'hangoutsMeet'
    assert created[0]['attendees'][0]['email'] == 'cloud@example.com'
    assert 'https://meet.google.com/abc-defg-hij' in client.get('/confirmed/' + token).text
    assert 'LOCATION:https://meet.google.com/abc-defg-hij' in client.get('/ics/' + token).text
    with module.SessionLocal() as db:
        b = db.scalar(module.select(module.Booking).where(module.Booking.cancel_token == token))
        first_id = db.get(module.GoogleEventLink, b.id).event_id
        booking_id = b.id
    assert module.sync_google_booking(booking_id) == 'synced'
    assert len(created) == 1  # Idempotency: no duplicate event.
    newer = client.get('/api/reschedule-slots/' + token, params={'day': day}).json()['slots']
    next_slot = next(x['start'] for x in newer if x['start'] != slot)
    result = client.post('/reschedule/' + token, data={'start': next_slot}, follow_redirects=False)
    assert result.status_code == 303
    assert updated and updated[0][0] == first_id
    assert len(created) == 1
    assert client.post('/cancel/' + token, follow_redirects=False).status_code == 303
    assert deleted == [first_id]
    with module.SessionLocal() as db:
        assert db.get(module.GoogleEventLink, booking_id).status == 'deleted'


def test_failed_google_creation_retries_without_duplicate_link(setup, monkeypatch):
    client, module = setup
    slot, _ = first_bookable_slot(client)
    settings = enable_google(monkeypatch, module)
    connect_fake(module, settings)
    monkeypatch.setattr(module.google, 'access_token', lambda *_: 'access')
    monkeypatch.setattr(module.google, 'freebusy', lambda *_: [])
    monkeypatch.setattr(module.google, 'insert_event', lambda *_: (_ for _ in ()).throw(module.google.GoogleIntegrationError('temporary outage')))
    response = client.post('/book/sponsor-meeting', data={'guest_name': 'Cloud User', 'guest_email': 'cloud@example.com', 'start': slot}, follow_redirects=False)
    assert response.status_code == 303
    token = response.headers['location'].split('/')[-1]
    with module.SessionLocal() as db:
        booking = db.scalar(module.select(module.Booking).where(module.Booking.cancel_token == token))
        linked = db.get(module.GoogleEventLink, booking.id)
        assert linked.status == 'error'
        event_id = linked.event_id
        booking_id = booking.id
    assert 'pending synchronization' in client.get('/confirmed/' + token).text
    monkeypatch.setattr(module.google, 'insert_event', lambda _t, _c, p: {'id': p['id'], 'conferenceData': {'entryPoints': [{'entryPointType': 'video', 'uri': 'https://meet.google.com/abc-defg-hij'}]}})
    csrf = login_admin(client)
    res = client.post(f'/admin/google/retry/{booking_id}', data={'token': csrf}, follow_redirects=False)
    assert res.status_code == 303
    with module.SessionLocal() as db:
        linked = db.get(module.GoogleEventLink, booking_id)
        assert linked.status == 'synced' and linked.event_id == event_id


def test_google_rest_event_payload_and_error_handling(monkeypatch):
    import google_integration as g
    from datetime import datetime
    a = datetime(2026, 11, 3, 11, 0)
    b = a + timedelta(minutes=30)
    payload = g.event_data('Call', 'Host', 'Guest', 'guest@example.com', a, b, 'zq1234567890', create_meet=True)
    assert payload['id'] == 'zq1234567890'
    assert payload['conferenceData']['createRequest']['requestId'] == 'zq1234567890meet'
    assert g.meet_link({'conferenceData': {'entryPoints': [{'entryPointType': 'video', 'uri': 'https://meet.google.com/test'}]}}) == 'https://meet.google.com/test'
    assert g.meet_link({'hangoutLink': 'https://evil.example.com/abc'}) is None
    monkeypatch.setattr(g, '_request', lambda *args, **kwargs: {'_status': 404})
    with pytest.raises(g.GoogleIntegrationError):
        g.get_event('token', 'primary', 'missing')
    monkeypatch.setattr(g, '_request', lambda *args, **kwargs: {'calendars': {'primary': {'errors': [{'reason': 'notFound'}]}}})
    with pytest.raises(g.GoogleIntegrationError):
        g.freebusy('token', 'primary', a.replace(tzinfo=timezone.utc), b.replace(tzinfo=timezone.utc))
