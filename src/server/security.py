"""Credential and opaque session-token helpers.

The service stores only salted password digests and token digests.  The local
implementation uses scrypt from the standard library; a deployment can swap
the adapter without changing the auth/session contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("口令至少需要 10 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$" + _b64(salt) + "$" + _b64(digest)


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded or not encoded.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt, expected = encoded.split("$", 5)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(expected))


def random_token(size: int = 32) -> str:
    return _b64(secrets.token_bytes(size))


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request_hash(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(method.upper().encode() + b"\0" + path.encode() + b"\0" + body).hexdigest()
