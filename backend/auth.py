"""
auth.py — the two tokens LedgerAssist understands
=============================================================================
There are two, and the difference is the whole design:

  HANDOFF   aud = "ledgerassist"            minted by ai/authorize.php
            Lives for 15 minutes. Travels through a URL fragment, which means
            it can land in a screenshot, a pasted link, a support ticket. So
            it is short-lived on purpose, and it is spent exactly once: the
            React app trades it for a session token the moment it arrives.

  SESSION   aud = "ledgerassist-session"    minted HERE, by this file
            Lives for 8 hours. Never appears in a URL — it goes straight into
            the app's memory and rides in the Authorization header. This is
            what an accountant's working day actually is, so the bot stops
            interrupting people mid-voucher.

Both are HS256 over the SAME secret, so PHP and Python can each verify what
the other signed. Set LEDGERASSIST_JWT_SECRET to the value that
ledgerassist_secret.php returns.

The bot NEVER reads system_id from a request body, URL or prompt — only from
a verified token. That is the rule the whole multi-tenant story rests on.
=============================================================================
"""
import os
import hmac
import json
import time
import base64
import hashlib
from typing import Dict, Optional, Tuple

from fastapi import Header, HTTPException

JWT_SECRET  = (os.getenv("LEDGERASSIST_JWT_SECRET") or "").encode()
JWT_ISSUER  = "lockinledger"

AUD_HANDOFF = "ledgerassist"             # from ai/authorize.php
AUD_SESSION = "ledgerassist-session"     # minted below

JWT_LEEWAY  = 30                         # seconds of clock skew tolerated on exp

# How long a session token lasts. 8 hours = one working day, so a person who
# signs in at 9am is not asked again before they go home.
SESSION_TTL = int(os.getenv("LEDGERASSIST_SESSION_TTL", str(8 * 60 * 60)))


class TokenError(Exception):
    """The token was missing, malformed, expired, or not ours."""


# --------------------------------------------------------------------------
# base64url — JWT's flavour, no padding
# --------------------------------------------------------------------------
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
def _decode(token: str, expect_aud: Optional[str] = None) -> Dict:
    """
    Signature first, claims second, trust last. Returns the raw payload.

    expect_aud=None accepts either audience; pass one to insist.
    """
    if not JWT_SECRET:
        raise TokenError("server not configured (LEDGERASSIST_JWT_SECRET unset)")

    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed token")

    signing_input = (parts[0] + "." + parts[1]).encode()
    try:
        sig = _b64url_decode(parts[2])
    except Exception:
        raise TokenError("bad signature encoding")

    expected = hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise TokenError("signature mismatch")

    try:
        header  = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        raise TokenError("bad token body")

    # Pin the algorithm: never let a token choose 'none' or a weaker alg.
    if header.get("alg") != "HS256":
        raise TokenError("unexpected alg")

    now = int(time.time())
    if int(payload.get("exp", 0)) < now - JWT_LEEWAY:
        raise TokenError("token expired")
    if payload.get("iss") != JWT_ISSUER:
        raise TokenError("wrong issuer")

    aud = payload.get("aud")
    if aud not in (AUD_HANDOFF, AUD_SESSION):
        raise TokenError("wrong audience")
    if expect_aud and aud != expect_aud:
        if expect_aud == AUD_HANDOFF:
            raise TokenError("this is already a session token - a session "
                             "token cannot be exchanged again")
        raise TokenError("wrong audience")

    if not str(payload.get("sid") or "").strip():
        raise TokenError("token missing sid")
    if not str(payload.get("uid") or "").strip():
        raise TokenError("token missing uid")

    return payload


def _claims(payload: Dict) -> Dict:
    return {
        "system_id": str(payload["sid"]).strip(),
        "user_id":   str(payload["uid"]).strip(),
        "name":      str(payload.get("name") or ""),
        "kind":      "session" if payload.get("aud") == AUD_SESSION else "handoff",
        "exp":       int(payload.get("exp", 0)),
    }


def verify_token(token: str) -> Dict:
    """
    Accept EITHER token for ordinary API calls.

    Handoff tokens are accepted here on purpose: it keeps the bot working for
    a client that hasn't implemented the exchange yet, and costs nothing —
    such a token dies on its own in 15 minutes.
    """
    return _claims(_decode(token))


def verify_handoff(token: str) -> Dict:
    """Only the 15-minute token from ai/authorize.php. Used by the exchange."""
    return _claims(_decode(token, expect_aud=AUD_HANDOFF))


# --------------------------------------------------------------------------
# mint
# --------------------------------------------------------------------------
def mint_session_token(system_id: str, user_id: str, name: str = "",
                       ttl: Optional[int] = None) -> Tuple[str, int]:
    """
    Sign an 8-hour session token for one company. Returns (token, exp).

    Only ever called with values that came out of a VERIFIED handoff token —
    never from anything a caller supplied.
    """
    if not JWT_SECRET:
        raise TokenError("server not configured (LEDGERASSIST_JWT_SECRET unset)")

    now = int(time.time())
    exp = now + int(ttl or SESSION_TTL)
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss":  JWT_ISSUER,
        "aud":  AUD_SESSION,
        "iat":  now,
        "exp":  exp,
        "sid":  str(system_id),
        "uid":  str(user_id),
        "name": name or "",
    }
    signing_input = (
        _b64url_encode(json.dumps(header,  separators=(',', ':')).encode()) + "." +
        _b64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    )
    sig = hmac.new(JWT_SECRET, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url_encode(sig), exp


# --------------------------------------------------------------------------
# FastAPI helper (main2.py uses its own get_ledger_context; this stays for
# anything that wants a hard requirement with no fallback)
# --------------------------------------------------------------------------
async def require_ledger(authorization: str = Header(None)) -> Dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        return verify_token(authorization.split(None, 1)[1].strip())
    except TokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")