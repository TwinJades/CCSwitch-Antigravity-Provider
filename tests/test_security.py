import pytest
from argon2.low_level import Type

from ai_provider_gateway.security import passwords
from ai_provider_gateway.security.secrets import MIN_SECRET_BYTES, generate_secret


def test_generated_secrets_are_nonempty_and_distinct() -> None:
    first = generate_secret()
    second = generate_secret()

    assert first
    assert second
    assert first != second


def test_secret_generation_rejects_insufficient_entropy() -> None:
    with pytest.raises(ValueError, match=str(MIN_SECRET_BYTES)):
        generate_secret(MIN_SECRET_BYTES - 1)


def test_password_verifier_is_configured_for_argon2id() -> None:
    assert passwords._PASSWORD_HASHER.type is Type.ID


def test_password_verification_rejects_empty_or_invalid_values() -> None:
    assert passwords.verify_password("", "password") is False
    assert passwords.verify_password("invalid", "password") is False
