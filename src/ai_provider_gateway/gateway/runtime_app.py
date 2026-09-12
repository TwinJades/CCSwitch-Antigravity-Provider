"""ASGI factory for the production Gateway process."""

from fastapi import FastAPI

from .app import create_app
from .runtime import GatewayRuntimeSettings, build_gateway_service


def create_runtime_app() -> FastAPI:
    return create_app(build_gateway_service(GatewayRuntimeSettings.from_env()))
