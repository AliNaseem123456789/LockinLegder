#!/usr/bin/env python3
"""
mint_token.py - make a test token, so you can check the bot without the
                browser and without two real LockInLedger logins.
=============================================================================
This is the Python twin of what ai/authorize.php does. It signs the same
HS256 token with the same secret, so the bot cannot tell them apart. That is
exactly why it is a TEST tool: keep it off the production box, or at least
remember that anyone who has it plus the secret can mint any company.

Usage
-----
    export LEDGERASSIST_JWT_SECRET=<same value ledgerassist_secret.php returns>

    python3 mint_token.py 146                # company 146, user 86, 15 min
    python3 mint_token.py 200 --user 99      # a different company
    python3 mint_token.py 146 --ttl -60      # already expired, to test 401
    python3 mint_token.py 146 --session      # a session token, not a handoff one

Exchange it for the real 8-hour one, the way the React app will:

    T=$(python3 mint_token.py 146)
    curl -s -X POST localhost:8000/api/auth/exchange \\
         -H "Authorization: Bearer $T" | python3 -m json.tool

Then:
    TOKEN=$(python3 mint_token.py 146)
    curl -s localhost:8000/api/whoami -H "Authorization: Bearer $TOKEN"
=============================================================================
"""
import os
import sys
import json
import time
import hmac
import base64
import hashlib
import argparse


def b64(raw: bytes) -> str:
    """base64url, no padding - what JWT uses."""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


AUD_HANDOFF = "ledgerassist"            # what ai/authorize.php mints
AUD_SESSION = "ledgerassist-session"    # what /api/auth/exchange mints


def mint(secret: str, sid: str, uid: str, name: str, ttl: int,
         aud: str = AUD_HANDOFF) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": "lockinledger",
        "aud": aud,
        "iat": now,
        "exp": now + ttl,
        "sid": str(sid),
        "uid": str(uid),
        "name": name,
    }
    signing_input = (b64(json.dumps(header, separators=(',', ':')).encode()) + "." +
                     b64(json.dumps(payload, separators=(',', ':')).encode()))
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)


def main():
    ap = argparse.ArgumentParser(description="Mint a LedgerAssist test token.")
    ap.add_argument("system_id", help="the company id to put in the token")
    ap.add_argument("--user", default="86", help="ledger user id (default 86)")
    ap.add_argument("--name", default="Test Operator", help="display name")
    ap.add_argument("--ttl", type=int, default=900,
                    help="seconds until expiry; pass a negative number to "
                         "mint an already-expired token (default 900)")
    ap.add_argument("--secret", default=None,
                    help="override LEDGERASSIST_JWT_SECRET, e.g. to prove a "
                         "wrong secret is rejected")
    ap.add_argument("--session", action="store_true",
                    help="mint a SESSION token (8h audience) instead of the "
                         "15-minute handoff one - use this to check that a "
                         "session token cannot be exchanged again")
    ap.add_argument("--decode", metavar="TOKEN",
                    help="print a token's claims instead of minting one "
                         "(does NOT verify the signature)")
    args = ap.parse_args()

    if args.decode:
        body = args.decode.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        left = claims.get('exp', 0) - int(time.time())
        claims['_expires_in_seconds'] = left
        claims['_expires_in'] = f"{left // 3600}h {(left % 3600) // 60}m" if left > 0 else "-"
        claims['_kind'] = ("session (8h, minted by the bot)"
                           if claims.get('aud') == AUD_SESSION
                           else "handoff (15min, minted by authorize.php)")
        claims['_status'] = "valid window" if left > 0 else "EXPIRED"
        print(json.dumps(claims, indent=2))
        return

    secret = args.secret or os.getenv("LEDGERASSIST_JWT_SECRET")
    if not secret:
        sys.exit("LEDGERASSIST_JWT_SECRET is not set. Export it first - it "
                 "must be the same string ledgerassist_secret.php returns.")

    aud = AUD_SESSION if args.session else AUD_HANDOFF
    print(mint(secret, args.system_id, args.user, args.name, args.ttl, aud))


if __name__ == '__main__':
    main()