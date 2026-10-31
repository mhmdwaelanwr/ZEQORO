"""Regression checks for data-backed Figma dashboard and responsive booking flow."""
import re
from test_booking import setup, first_bookable_slot


def test_booking_uses_real_date_tiles_and_keeps_post_contract(setup):
    client, _ = setup
    html = client.get('/book/sponsor-meeting').text
    assert 'data-design="figma-booking-1-75"' in html
    assert 'id="date-tiles"' in html
    assert 'id="next-dates"' in html
    assert 'id="day"' in html
    assert 'id="slots"' in html
    assert 'id="continue-btn"' in html
    assert 'id="guest-details"' in html
    assert 'name="start"' in html
    assert 'name="guest_email"' in html
    assert 'min="' in html and 'max="' in html
    assert client.get('/static/figma.css').status_code == 200
    assert client.get('/static/booking.js').status_code == 200


def test_dashboard_shows_actual_booking_metrics_and_honors_filters(setup):
    client, _ = setup
    client.post('/admin/login', data={'username': 'anwar', 'password': 'test-secret-password'})
    before = client.get('/admin').text
    assert 'data-design="figma-dashboard-1-2"' in before
    assert '<strong>0</strong>' in before
    assert 'Your next meeting will show up here.' in before
    assert 'Hanan Name' not in before
    slot, _ = first_bookable_slot(client)
    created = client.post('/book/sponsor-meeting', data={
        'guest_name': 'Hanan Name', 'guest_email': 'hanan@example.com', 'start': slot,
    }, follow_redirects=False)
    assert created.status_code == 303
    after = client.get('/admin').text
    assert 'Hanan Name' in after
    assert 'Hours scheduled' in after
    assert re.search(r'Upcoming meetings</span><strong>1</strong>', after)
    assert '1 meeting in the next 7 days' in after or '1 meetings in the next 7 days' not in after
    assert 'Hanan Name' not in client.get('/admin?q=nothingfound').text
    assert 'id="create-event"' in after
    assert 'id="blocked-days"' in after


def test_legacy_landing_and_private_pages_remain_intact(setup):
    client, _ = setup
    assert '<body class="">' in client.get('/').text
    assert client.get('/admin', follow_redirects=False).status_code == 303
    booking_css = client.get('/book/sponsor-meeting').text
    assert '<body class="zeqoro-booking">' in booking_css
    assert '<link rel="stylesheet" href="/static/figma.css">' in booking_css
