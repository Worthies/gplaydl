"""Add a Google account to gplaydl, no phone or Authenticator app needed.

Mirrors what the Authenticator app does on an Android phone, minus the
WebView: a Google sign-in's oauth_token cookie is traded for a long-lived
AAS token, which is then synced to the dispenser under this machine's own
device identity.
"""

from __future__ import annotations

import platform
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from gplaydl.auth import DispenserError

# Recorded server-side against the device at enrolment, so the wording
# someone agreed to is kept on file. Must match the dispenser's own
# currentConsentVersion (and the Authenticator app's BuildConfig value).
CONSENT_VERSION = "2026-07-27"

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"

# The AC2DM exchange below is copied verbatim from Aurora Authenticator /
# gplaydl Authenticator's AC2DMTask: field names, order and the raw
# (non-URL-encoded) body join all matter. Google's /auth endpoint rejects
# clients that do not look like the platform HTTP stack, answering
# MissingDroidguard -- an attestation only genuine Play Services can
# produce. httpx (built on the stdlib ssl module) presents a TLS fingerprint
# close enough to pass; do not swap in a different HTTP stack without
# re-verifying that.
_AC2DM_URL = "https://android.clients.google.com/auth"
_CALLER_SIG = "38918a453d07199354f8b19af05ec6562ced5788"
_PLAY_SERVICES_VERSION = 19629032
_SDK_VERSION = 28

# Google's edge sometimes resets the TLS handshake or the socket for this
# request before any HTTP response exists to interpret, without it being a
# real rejection of the token -- a fresh connection right after usually goes
# through. Retry a couple of times on that specific class of failure only;
# anything that produced a real HTTP response (including Google's own
# Error= rejections) is never retried.
_MAX_MINT_ATTEMPTS = 3
_MINT_RETRY_DELAY = 0.5
_TRANSIENT_MARKERS = (
    "sslhandshake", "socket", "eof", "connection closed", "connection reset", "timed out",
)

_OAUTH_COOKIE_RE = re.compile(r"(?:^|;\s*|\s)oauth_token=([^;\s]+)")


class GoogleAuthError(Exception):
    """Google refused to mint an AAS token; str(exc) says why, for the user."""


@dataclass
class MintedCredentials:
    email: str
    aas_token: str


def extract_oauth_token(raw: str) -> str:
    """Pull oauth_token out of a pasted Cookie header, or pass a bare value through.

    Accepts whatever is easiest for someone to copy out of dev tools: the
    whole Cookie request header, a single "oauth_token=..." line, or just the
    bare value.
    """
    raw = raw.strip()
    if not raw:
        return ""
    match = _OAUTH_COOKIE_RE.search(raw)
    if match:
        return match.group(1)
    if raw.lower().startswith("oauth_token"):
        _, _, value = raw.partition("=")
        if value:
            return value.strip().rstrip(";")
    return raw


def mint_aas_token(email: str, oauth_token: str) -> MintedCredentials:
    """Exchange a Google oauth_token cookie for a long-lived AAS token."""
    email = email.strip()
    if not email or email.lower() == "null":
        raise GoogleAuthError("No Google account email given.")
    oauth_token = oauth_token.strip()
    if not oauth_token:
        raise GoogleAuthError("No oauth_token value given.")

    body = _build_ac2dm_body(email, oauth_token)
    headers = {
        "app": "com.google.android.gms",
        "User-Agent": "",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    resp = _post_with_retry(body, headers)

    fields = _parse_kv(resp.text)
    if "Error" in fields:
        raise GoogleAuthError(_describe_error(fields["Error"], fields, resp.status_code))
    token = fields.get("Token")
    if not token:
        detail = resp.text.strip()[:200] or "an empty response"
        raise GoogleAuthError(f"Google did not return a token (HTTP {resp.status_code}): {detail}")
    if not token.startswith("aas_et/"):
        raise GoogleAuthError("Google returned an unexpected token format.")
    return MintedCredentials(email=fields.get("Email", email), aas_token=token)


def _build_ac2dm_body(email: str, oauth_token: str) -> str:
    params = {
        "lang": "en-US",
        "google_play_services_version": _PLAY_SERVICES_VERSION,
        "sdk_version": _SDK_VERSION,
        "device_country": "us",
        "Email": email,
        "service": "ac2dm",
        "get_accountid": 1,
        "ACCESS_TOKEN": 1,
        "callerPkg": "com.google.android.gms",
        "add_account": 1,
        "Token": oauth_token,
        "callerSig": _CALLER_SIG,
        "droidguard_results": "null",
    }
    return "&".join(f"{k}={v}" for k, v in params.items())


def _post_with_retry(body: str, headers: dict) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_MINT_ATTEMPTS + 1):
        try:
            return httpx.post(_AC2DM_URL, content=body.encode(), headers=headers, timeout=30)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < _MAX_MINT_ATTEMPTS and _is_transient(str(exc)):
                time.sleep(_MINT_RETRY_DELAY)
                continue
            raise GoogleAuthError(f"Could not reach Google: {exc}") from exc
    raise GoogleAuthError(f"Could not reach Google: {last_exc}")  # pragma: no cover


def _parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _is_transient(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def _describe_error(error: str, fields: dict[str, str], status: int) -> str:
    if error == "BadAuthentication":
        return (
            "Google rejected the sign-in. oauth_token is single-use and "
            "short-lived -- sign in again and paste a fresh one."
        )
    if error in ("NeedsBrowser", "DeviceManagementRequiredOrSyncDisabled"):
        return "This account needs extra verification in a browser before it can be used."
    if error == "MissingDroidguard":
        return (
            "Google asked for a Play Services integrity check this client could not "
            "provide. This account may have been flagged -- try a freshly created "
            "Google account."
        )
    return fields.get("ErrorDetail") or f"Google returned an error: {error} (HTTP {status})"


def enroll_device(base: str, secret: str, label: str) -> str:
    """Register this machine's identity with the dispenser; returns its API key.

    Re-enrolling with the same secret (e.g. after deleting the config file)
    re-issues the same identity instead of creating an orphaned duplicate.
    """
    try:
        resp = httpx.post(
            base + "/api/v1/devices/enroll",
            json={"deviceSecret": secret, "label": label, "consentVersion": CONSENT_VERSION},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise DispenserError(0, f"could not reach {base}: {exc}") from exc
    payload = _json_or_empty(resp)
    if resp.status_code not in (200, 201) or not payload.get("apiKey"):
        raise DispenserError(
            resp.status_code, payload.get("error") or f"dispenser returned HTTP {resp.status_code}",
        )
    return payload["apiKey"]


def sync_account(base: str, api_key: str, email: str, aas_token: str) -> dict:
    """Upload the minted token; the account stays private to this device's owner."""
    try:
        resp = httpx.post(
            base + "/api/v1/accounts",
            json={"email": email, "aasToken": aas_token},
            headers={"X-Api-Key": api_key},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise DispenserError(0, f"could not reach {base}: {exc}") from exc
    payload = _json_or_empty(resp)
    if resp.status_code not in (200, 201):
        raise DispenserError(
            resp.status_code, payload.get("error") or f"dispenser returned HTTP {resp.status_code}",
        )
    return payload.get("account", {})


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def device_label() -> str:
    return (platform.node() or "gplaydl")[:64]
