"""Google Calendar REST integration: OAuth PKCE, encrypted credentials, free/busy and Meet.

The public booking routes do not accept arbitrary Google URLs. Credentials never
leave the server, and every request is made to Google's fixed endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

import httpx
from cryptography.fernet import Fernet, InvalidToken

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_CACHE_LOCK = threading.RLock()
_ACCESS_TOKENS: dict[str, tuple[str, float]] = {}


class GoogleIntegrationError(Exception):
    """User-safe error: no OAuth secrets or provider response body are exposed."""


class GoogleEventMissing(GoogleIntegrationError):
    """A previously linked event no longer exists on the selected calendar."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str

    @classmethod
    def from_env(cls, public_base_url: str) -> "Settings | None":
        keys = [os.getenv(k, "").strip() for k in (
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_TOKEN_ENCRYPTION_KEY"
        )]
        if not all(keys):
            return None
        result = cls(*keys)
        if result.redirect_uri != public_base_url.rstrip("/") + "/admin/google/callback":
            raise GoogleIntegrationError("Google redirect URI must exactly match PUBLIC_BASE_URL + /admin/google/callback")
        try:
            Fernet(result.encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise GoogleIntegrationError("GOOGLE_TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc
        return result


def encrypt_secret(value: str, settings: Settings) -> str:
    return Fernet(settings.encryption_key.encode()).encrypt(value.encode()).decode()


def decrypt_secret(value: str, settings: Settings) -> str:
    try:
        return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise GoogleIntegrationError("Stored Google authorization cannot be decrypted. Check the encryption key.") from exc


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorization_url(settings: Settings, state: str, verifier: str, *, scopes: tuple[str, ...] = SCOPES) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.client_id, "redirect_uri": settings.redirect_uri,
        "response_type": "code", "scope": " ".join(scopes), "state": state,
        "access_type": "offline", "prompt": "consent", "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256", "include_granted_scopes": "true",
    })


def account_identity(token: str) -> dict[str, str]:
    """Verify the Google account owner with OpenID Connect email scope."""
    try:
        with httpx.Client(timeout=8) as client:
            response = client.get("https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        identity = response.json()
        if identity.get("email_verified") is not True or not identity.get("sub") or not identity.get("email"):
            raise GoogleIntegrationError("Google did not provide a verified account identity")
        return {"sub": str(identity["sub"]), "email": str(identity["email"])}
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleIntegrationError("Could not verify the connected Google account") from exc


def _post_token(data: dict) -> dict:
    try:
        with httpx.Client(timeout=8) as client:
            response = client.post(TOKEN_URL, data=data)
        response.raise_for_status()
        result = response.json()
        if not result.get("access_token"):
            raise GoogleIntegrationError("Google did not return an access token")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleIntegrationError("Google OAuth token exchange failed; please reconnect the account") from exc


def exchange_code(settings: Settings, code: str, verifier: str) -> dict:
    return _post_token({
        "client_id": settings.client_id, "client_secret": settings.client_secret,
        "redirect_uri": settings.redirect_uri, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code",
    })


def access_token(settings: Settings, encrypted_refresh_token: str) -> str:
    # Cache access tokens in memory, but keep only encrypted refresh tokens on disk.
    key = hashlib.sha256(encrypted_refresh_token.encode()).hexdigest()
    with _CACHE_LOCK:
        cached = _ACCESS_TOKENS.get(key)
        if cached and cached[1] > time.monotonic() + 45:
            return cached[0]
        refresh = decrypt_secret(encrypted_refresh_token, settings)
        result = _post_token({
            "client_id": settings.client_id, "client_secret": settings.client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        })
        token = result["access_token"]
        ttl = max(60, int(result.get("expires_in", 3600)))
        _ACCESS_TOKENS[key] = (token, time.monotonic() + ttl)
        return token


def _request(token: str, method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=9) as client:
            response = client.request(
                method, CALENDAR_API + path,
                headers={"Authorization": "Bearer " + token},
                params=params, json=payload,
            )
        if response.status_code in (204, 404, 409):
            return {"_status": response.status_code}
        response.raise_for_status()
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleIntegrationError("Google Calendar is temporarily unavailable; sync can be retried") from exc


def freebusy(token: str, calendar_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    result = _request(token, "POST", "/freeBusy", payload={
        "timeMin": start.astimezone(timezone.utc).isoformat(),
        "timeMax": end.astimezone(timezone.utc).isoformat(),
        "items": [{"id": calendar_id}],
    })
    calendar = result.get("calendars", {}).get(calendar_id)
    if not isinstance(calendar, dict) or calendar.get("errors"):
        raise GoogleIntegrationError("Google calendar availability could not be verified")
    intervals = []
    for item in calendar.get("busy", []):
        a = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
        if a.tzinfo is None or b.tzinfo is None:
            raise GoogleIntegrationError("Google returned an invalid busy interval")
        intervals.append((a.astimezone(timezone.utc).replace(tzinfo=None), b.astimezone(timezone.utc).replace(tzinfo=None)))
    return intervals



def freebusy_many(token: str, calendar_ids: list[str], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Check all selected Google calendars in one API call; fail closed on any error.

    Google FreeBusy supports up to 50 calendars per request. Keeping all members'
    selected calendars in the same response reduces scheduling API latency.
    """
    ids = list(dict.fromkeys(calendar_ids))
    if not ids or len(ids) > 50 or any(not isinstance(i, str) or not i for i in ids):
        raise GoogleIntegrationError("Invalid Google calendar selection")
    result = _request(token, "POST", "/freeBusy", payload={
        "timeMin": start.astimezone(timezone.utc).isoformat(),
        "timeMax": end.astimezone(timezone.utc).isoformat(),
        "items": [{"id": identifier} for identifier in ids],
    })
    calendars = result.get("calendars")
    if not isinstance(calendars, dict) or result.get("groups", {}).get("errors"):
        raise GoogleIntegrationError("Google calendar availability could not be verified")
    intervals = []
    for identifier in ids:
        calendar = calendars.get(identifier)
        if not isinstance(calendar, dict) or calendar.get("errors") or not isinstance(calendar.get("busy"), list):
            raise GoogleIntegrationError("Google calendar availability could not be verified")
        try:
            for item in calendar["busy"]:
                a = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
                if a.tzinfo is None or b.tzinfo is None or b <= a:
                    raise ValueError("Invalid busy interval")
                intervals.append((a.astimezone(timezone.utc).replace(tzinfo=None),
                                  b.astimezone(timezone.utc).replace(tzinfo=None)))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise GoogleIntegrationError("Google returned an invalid busy interval") from exc
    return intervals

def event_path(calendar_id: str, event_id: str = "") -> str:
    base = "/calendars/" + quote(calendar_id, safe="") + "/events"
    return base + ("/" + quote(event_id, safe="") if event_id else "")


def event_data(event_name: str, host_name: str, guest_name: str, guest_email: str, start: datetime, end: datetime, event_id: str, *, create_meet: bool) -> dict:
    payload = {
        "summary": f"{event_name} · {host_name}",
        "description": f"Booking through ZEQORO · Guest: {guest_name}",
        "start": {"dateTime": start.replace(tzinfo=timezone.utc).isoformat()},
        "end": {"dateTime": end.replace(tzinfo=timezone.utc).isoformat()},
        "attendees": [{"email": guest_email, "displayName": guest_name}],
    }
    if create_meet:
        payload["id"] = event_id
        payload["conferenceData"] = {"createRequest": {
            "requestId": event_id + "meet",
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
    return payload


def insert_event(token: str, calendar_id: str, payload: dict) -> dict:
    result = _request(token, "POST", event_path(calendar_id), params={"conferenceDataVersion": 1, "sendUpdates": "all"}, payload=payload)
    if result.get("_status") == 409:
        return get_event(token, calendar_id, payload["id"])
    return result


def get_event(token: str, calendar_id: str, event_id: str) -> dict:
    result = _request(token, "GET", event_path(calendar_id, event_id))
    if result.get("_status") == 404:
        raise GoogleEventMissing("Linked Google event was deleted; recreating requires retry")
    return result


def patch_event(token: str, calendar_id: str, event_id: str, payload: dict) -> dict:
    result = _request(token, "PATCH", event_path(calendar_id, event_id), params={"conferenceDataVersion": 1, "sendUpdates": "all"}, payload=payload)
    if result.get("_status") == 404:
        raise GoogleEventMissing("Linked Google event was deleted; recreating requires retry")
    return result


def delete_event(token: str, calendar_id: str, event_id: str) -> None:
    _request(token, "DELETE", event_path(calendar_id, event_id), params={"sendUpdates": "all"})


def meet_link(event: dict) -> str | None:
    for entry in event.get("conferenceData", {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video" and str(entry.get("uri", "")).startswith("https://meet.google.com/"):
            return entry["uri"]
    candidate = event.get("hangoutLink", "")
    return candidate if candidate.startswith("https://meet.google.com/") else None
