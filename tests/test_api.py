from fastapi.testclient import TestClient

from loggingx_ai.config import Settings
from loggingx_ai.diagnosis import Diagnoser
from loggingx_ai.main import create_app
from tests.conftest import FakeLlm
from tests.test_stacktrace import JAVA


def client(**settings) -> TestClient:
    config = Settings(_env_file=None, **settings)
    return TestClient(create_app(config, Diagnoser(FakeLlm(), config)))


def test_diagnose_returns_camel_case():
    response = client().post("/api/v1/diagnoses", json={"logText": JAVA, "storeName": "shop"})
    assert response.status_code == 200
    body = response.json()
    assert body["rootCause"] and body["confidence"] == "high"
    assert body["promptTokens"] == 40 and len(body["steps"]) == 4


def test_api_key_required_when_configured():
    api = client(api_key="secret")
    assert api.post("/api/v1/diagnoses", json={"logText": JAVA}).status_code == 401
    ok = api.post("/api/v1/diagnoses", json={"logText": JAVA}, headers={"X-API-Key": "secret"})
    assert ok.status_code == 200


def test_stream_emits_agents_then_result():
    with client().stream("POST", "/api/v1/diagnoses/stream", json={"logText": JAVA}) as response:
        text = "".join(response.iter_text())
    events = [line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")]
    assert events[0] == "agent" and events[-2:] == ["result", "done"]


def test_health_lists_configured_providers():
    assert client(deepseek_api_key="k").get("/health").json()["providers"] == ["deepseek"]
