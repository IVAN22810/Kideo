"""API key generation, hashing, and prefix extraction.

Plaintext keys look like:  kideo_live_<32 base62 chars>     (43 chars total)
We persist ONLY the SHA-256 hex of the plaintext — never the plaintext itself.
The first 15 chars (`kideo_live_AbCd`) are stored separately as `prefix` so the
dashboard can show a human-recognizable identifier without revealing the key.

Why SHA-256 (not bcrypt/argon2): opaque tokens carry ~190 bits of entropy by
construction (32 base62 chars), so the per-guess cost is irrelevant — an
attacker who can grind 2^190 SHA-256 hashes has already broken the universe.
bcrypt-style stretching exists to protect low-entropy human passwords; here it
just makes legitimate auth slower for no security gain. This is the pattern
Stripe, GitHub, AWS, and Mercury use for their API keys.
"""
from __future__ import annotations

import hashlib
import secrets
import string

# Public constants — referenced from routers/tests/seed
KEY_PREFIX = "kideo_live_"
KEY_BODY_LENGTH = 32                   # 32 base62 chars ~= 190 bits of entropy
KEY_TOTAL_LENGTH = len(KEY_PREFIX) + KEY_BODY_LENGTH  # 43
PREFIX_DISPLAY_LENGTH = len(KEY_PREFIX) + 4           # 15 chars, e.g. "kideo_live_AbCd"

_KEY_ALPHABET = string.ascii_letters + string.digits  # base62


def generate_plaintext_key() -> str:
    """Return a fresh plaintext API key, e.g. `kideo_live_aB3xZ...`.

    Cryptographically random via `secrets`. Caller is responsible for hashing
    before storage and for returning the plaintext to the customer exactly once.
    """
    body = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(KEY_BODY_LENGTH))
    return f"{KEY_PREFIX}{body}"


def hash_key(plaintext: str) -> str:
    """Return the SHA-256 hex digest of a plaintext key.

    Deterministic — the same plaintext always hashes to the same value, which
    is what makes the constant-time index lookup at auth time possible.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def extract_prefix(plaintext: str) -> str:
    """Return the first PREFIX_DISPLAY_LENGTH chars of the plaintext key.

    Stored in the DB alongside the hash so the dashboard can show
    `kideo_live_AbCd…` without ever seeing the full key after creation.
    """
    return plaintext[:PREFIX_DISPLAY_LENGTH]
