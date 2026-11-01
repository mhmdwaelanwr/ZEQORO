"""Unified design and navigation regressions across public and admin screens."""
import re
from test_booking import setup, first_bookable_slot


def test_shared_design_system_and_public_guide(setup):
    client, _ = setup
    for uri in ('/', '/how-it-works', '/book/sponsor-meeting', '/admin/login'):
        result = client.get(uri)
        assert result.status_code == 200, uri
        html = result.text
        assert 'href="/static/unified.css"' in html
        assert 'href="/how-it-works"' in html
        assert 'class="skip-link"' in html
        assert 'href="/static/figma.css"' in html
        assert '<title>' in html
    guide = client.get('/how-it-works').text
    assert 'Frequently asked questions.' in guide
    assert 'connects Google Calendar' in guide  # Feature requires explicit host consent.
    assert 'No. Choose a meeting' in guide
    assert client.get('/static/unified.css').status_code == 200
    assert client.get('/static/site.js').status_code == 200


def test_booking_and_reschedule_share_interaction_controls(setup):
    client, _ = setup
    slot, _ = first_bookable_slot(client)
    created = client.post('/book/sponsor-meeting', data={
        'guest_name': 'Test Guest', 'guest_email': 'guest@example.com', 'start': slot,
    }, follow_redirects=False)
    assert created.status_code == 303
    token = created.headers['location'].split('/')[-1]
    booking_html = client.get('/book/sponsor-meeting').text
    manage_html = client.get(f'/reschedule/{token}').text
    for html in (booking_html, manage_html):
        assert 'class="zeqoro-booking"' in html
        for element_id in ('day', 'date-tiles', 'prev-dates', 'next-dates', 'slots', 'selected-slot', 'confirm-btn', 'selection-note'):
            assert f'id="{element_id}"' in html
        assert 'href="/static/unified.css"' in html
    assert f'action="/reschedule/{token}"' in manage_html
    assert 'Your original meeting stays confirmed' in manage_html
    assert 'id="guest-details"' not in manage_html  # No guest data needed to reschedule.


def test_private_states_are_unified_and_noindex(setup):
    client, _ = setup
    slot, _ = first_bookable_slot(client)
    response = client.post('/book/sponsor-meeting', data={
        'guest_name': 'Test Guest', 'guest_email': 'guest@example.com', 'start': slot,
    }, follow_redirects=False)
    token = response.headers['location'].split('/')[-1]
    for path in (f'/confirmed/{token}', f'/cancel/{token}'):
        resp = client.get(path)
        assert resp.status_code == 200
        assert 'class="zeqoro-state"' in resp.text
        assert 'name="robots" content="noindex,nofollow"' in resp.text
        assert resp.headers['cache-control'].startswith('no-store')
        assert 'href="/static/unified.css"' in resp.text
    error = client.get('/book/does-not-exist')
    assert error.status_code == 404
    assert 'class="zeqoro-state"' in error.text
    assert 'How scheduling works' in error.text


def test_dashboard_share_actions_and_create_accessibility(setup):
    client, _ = setup
    client.post('/admin/login', data={'username': 'anwar', 'password': 'test-secret-password'})
    html = client.get('/admin').text
    assert 'class="workspace-share"' in html
    assert 'data-copy-url="http://localhost:8000/"' in html
    assert 'data-copy-url="http://localhost:8000/book/sponsor-meeting"' in html
    assert 'href="/how-it-works"' in html
    for identifier in ('new-title', 'new-slug', 'new-duration', 'new-desc'):
        assert f'for="{identifier}"' in html
        assert f'id="{identifier}"' in html
    assert re.search(r'<form[^>]*id="create-event"', html)


def test_unified_static_theme_has_focus_and_mobile_rules(setup):
    client, _ = setup
    css = client.get('/static/unified.css').text
    assert '--z-accent:#755bff' in css
    assert '.skip-link:focus' in css
    assert ':focus-visible' in css
    assert '@media(max-width:720px)' in css
    js = client.get('/static/booking.js').text
    assert 'continueButton?.addEventListener' in js
    assert 'prev?.addEventListener' in js
    assert 'Retry loading times' in js
