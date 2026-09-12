"""Shared validation for localhost Harness connection descriptions."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from ..models import ModelRecord


class IntegrationConfigError(ValueError):
    """A requested Harness configuration is unsafe or inconsistent."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def gateway_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise IntegrationConfigError("Gateway base URL must be an absolute HTTP URL.")
    try:
        parsed = urlparse(value)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as error:
        raise IntegrationConfigError("Gateway base URL must use a valid explicit loopback port.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IntegrationConfigError("Gateway base URL must be an absolute HTTP URL.")
    if port is None or port == 0:
        raise IntegrationConfigError("Gateway base URL must use a valid explicit loopback port.")
    hostname = hostname.lower()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise IntegrationConfigError("Gateway base URL must use loopback.")
        except ValueError as error:
            raise IntegrationConfigError("Gateway base URL must use loopback.") from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise IntegrationConfigError("Gateway base URL cannot contain credentials or a query.")
    path = parsed.path.rstrip("/")
    if path != "/v1":
        raise IntegrationConfigError("Gateway base URL must end with /v1.")
    return value.rstrip("/")


def safe_id(value: str, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise IntegrationConfigError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not _SAFE_ID.fullmatch(normalized):
        raise IntegrationConfigError(f"{label} is invalid.")
    return normalized


def environment_name(value: str) -> str:
    if not isinstance(value, str):
        raise IntegrationConfigError("API-key environment name must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or not _ENV_NAME.fullmatch(normalized):
        raise IntegrationConfigError("API-key environment name is invalid.")
    return normalized


def available_models(records: list[ModelRecord] | tuple[ModelRecord, ...]) -> tuple[ModelRecord, ...]:
    available = tuple(sorted((record for record in records if record.available), key=lambda item: item.canonical_id))
    if not available:
        raise IntegrationConfigError("No available models can be exported.")
    if len({record.canonical_id for record in available}) != len(available):
        raise IntegrationConfigError("Model catalogue contains duplicate canonical IDs.")
    return available


def selected_model(records: tuple[ModelRecord, ...], requested: str | None) -> str:
    if requested is None:
        return records[0].canonical_id
    normalized = requested.strip() if isinstance(requested, str) else ""
    for record in records:
        if normalized in {record.canonical_id, record.alias}:
            return record.canonical_id
    raise IntegrationConfigError("Selected model is not available.")
