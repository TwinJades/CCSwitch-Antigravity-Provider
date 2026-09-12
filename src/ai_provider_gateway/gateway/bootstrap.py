"""Explicit local-only API-key bootstrap helper; no HTTP management API exists."""

from __future__ import annotations

import argparse

from ..database import create_database_engine
from .authentication import GatewayApiKeyStore, IssuedGatewayKey


def create_initial_key(store: GatewayApiKeyStore, key_id: str = "default") -> IssuedGatewayKey:
    """Issue a key for an already-migrated local database; callers print it once."""

    return store.create(key_id)


def main() -> None:
    """Explicitly issue one local key and print it once to the invoking terminal."""

    parser = argparse.ArgumentParser(description="Create a local Gateway API key.")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--key-id", default="default")
    arguments = parser.parse_args()
    issued = create_initial_key(
        GatewayApiKeyStore(create_database_engine(arguments.database_url)), arguments.key_id
    )
    print(issued.secret)


if __name__ == "__main__":
    main()
