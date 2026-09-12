from fastapi.testclient import TestClient

from ai_provider_gateway.control.app import create_app as create_supervisor_app
from ai_provider_gateway.gateway.app import create_app as create_gateway_app


def test_supervisor_health_is_the_only_default_phase_two_surface() -> None:
    client = TestClient(create_supervisor_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "service": "supervisor",
        "status": "ok",
        "phase": "phase-2",
    }
    assert client.get("/v1/models").status_code == 404
    assert client.post("/v1/chat/completions").status_code == 404
    assert client.get("/oauth/callback").status_code == 404
    assert client.get("/docs").status_code == 404


def test_gateway_remains_phase_one_surface_during_phase_two() -> None:
    client = TestClient(create_gateway_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "service": "gateway",
        "status": "ok",
        "phase": "phase-1",
    }
    assert client.get("/v1/models").status_code == 404
    assert client.post("/v1/chat/completions").status_code == 404
    assert client.get("/docs").status_code == 404


def test_app_factories_do_not_share_route_instances() -> None:
    first = create_gateway_app()
    second = create_gateway_app()

    assert first is not second
    assert first.routes[0] is not second.routes[0]
