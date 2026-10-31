"""Security and dashboard regression checks for ZEQORO v0.3."""
from datetime import datetime, timezone, timedelta
from test_booking import setup, first_bookable_slot
from test_v02 import admin_session

def test_login_throttle_and_recovery(setup):
    client, module = setup
    for _ in range(8):
        assert client.post('/admin/login', data={'username':'anwar','password':'wrong'}).status_code == 200
    res = client.post('/admin/login', data={'username':'anwar','password':'test-secret-password'})
    assert res.status_code == 429
    assert res.headers['retry-after'] == '900'
    module.limiter.clear('admin_login','testclient')
    assert client.post('/admin/login', data={'username':'anwar','password':'test-secret-password'},follow_redirects=False).status_code == 303

def test_public_booking_throttle(setup):
    client, _ = setup
    for _ in range(12):
        assert client.post('/book/sponsor-meeting',data={'guest_name':'x','guest_email':'invalid','start':'bad'}).status_code == 400
    assert client.post('/book/sponsor-meeting',data={'guest_name':'x','guest_email':'invalid','start':'bad'}).status_code == 429

def test_timezone_offset_and_dashboard_search(setup):
    client, _ = setup
    slot, _ = first_bookable_slot(client)
    d = datetime.fromisoformat(slot)
    wrong_offset=d.astimezone(timezone.utc).isoformat()
    if wrong_offset==slot:
        wrong_offset=d.astimezone(timezone(timedelta(hours=5))).isoformat()
    assert client.post('/book/sponsor-meeting',data={'guest_name':'Offset Test','guest_email':'test@example.com','start':wrong_offset}).status_code==400
    res=client.post('/book/sponsor-meeting',data={'guest_name':'Hanan Search','guest_email':'hanan@example.com','start':slot},follow_redirects=False)
    assert res.status_code==303
    token=res.headers['location'].split('/')[-1]
    invitation=client.get(f'/ics/{token}').content
    assert b'METHOD:PUBLISH' in invitation and b'STATUS:CONFIRMED' in invitation
    assert all(len(line)<=75 for line in invitation.split(b'\r\n') if line)
    admin_session(client)
    html=client.get('/admin').text
    assert '<title>Dashboard · ZEQORO</title>' in html
    assert 'id="blocked-days"' in html
    assert 'id="booking-q"' in html
    assert 'Hanan Search' in client.get('/admin?q=hanan&status=confirmed').text
    assert 'Hanan Search' not in client.get('/admin?q=noresults&status=confirmed').text
    assert client.get('/admin?status=bad').status_code == 400
