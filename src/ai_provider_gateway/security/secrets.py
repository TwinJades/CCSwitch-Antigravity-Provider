"""Cryptographically secure local secret generation."""

from __future__ import annotations

import secrets


MIN_SECRET_BYTES = 32


def generate_secret(num_bytes: int = MIN_SECRET_BYTES) -> str:
    """Return a URL-safe secret backed by at least 32 random bytes."""

    if num_bytes < MIN_SECRET_BYTES:
        raise ValueError(f"num_bytes must be at least {MIN_SECRET_BYTES}")
    return secrets.token_urlsafe(num_bytes)
