"""Google multi-calendar FreeBusy batching and fail-closed semantics."""
from datetime import datetime, timezone, timedelta

import pytest
import google_integration as google

START = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def test_bulk_google_freebusy_one_request_and_union(monkeypatch):
    seen = []
    def request(token, method, path, *, payload=None, **kwargs):
        seen.append((method, path, payload))
        return {"calendars": {
            "personal": {"busy": [{"start": "2026-11-02T09:00:00Z", "end": "2026-11-02T10:00:00Z"}]},
            "work": {"busy": [{"start": "2026-11-02T11:00:00Z", "end": "2026-11-02T12:00:00Z"}]},
        }}
    monkeypatch.setattr(google, "_request", request)
    intervals = google.freebusy_many("access", ["personal", "work", "personal"], START, END)
    assert len(intervals) == 2
    assert len(seen) == 1
    assert seen[0][1] == "/freeBusy"
    assert seen[0][2]["items"] == [{"id": "personal"}, {"id": "work"}]
    assert intervals[0][0].tzinfo is None


@pytest.mark.parametrize("broken", [
    {"calendars": {"personal": {"busy": []}}},
    {"calendars": {"personal": {"busy": []}, "work": {"errors": [{"reason": "notFound"}], "busy": []}}},
    {"calendars": {"personal": {"busy": []}, "work": {"busy": [{"start": "bad", "end": "2026-11-02T11:00:00Z"}]}}},
    {"calendars": {"personal": {"busy": []}, "work": {"busy": [{"start": "2026-11-02T12:00:00Z", "end": "2026-11-02T11:00:00Z"}]}}},
])
def test_bulk_google_freebusy_rejects_bad_response(monkeypatch, broken):
    monkeypatch.setattr(google, "_request", lambda *args, **kwargs: broken)
    with pytest.raises(google.GoogleIntegrationError):
        google.freebusy_many("access", ["personal", "work"], START, END)


def test_bulk_google_freebusy_rejects_empty_and_too_many():
    with pytest.raises(google.GoogleIntegrationError):
        google.freebusy_many("access", [], START, END)
    with pytest.raises(google.GoogleIntegrationError):
        google.freebusy_many("access", [str(i) for i in range(51)], START, END)
