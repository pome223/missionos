"""Artifact HTTP protection; no worker, browser, provider or physics invocation."""
from hashlib import sha256
import json
import socket
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient
import pytest
import uvicorn

from src.gateway import server
from src.runtime.starship_mission_control import StarshipMissionError, StarshipMissionService

API_KEY = "artifact-csp-fixture-key"
URL = "/missionos/starship/sessions/fixture-session/plans/fixture-plan/artifacts/"
REPORT = b"<!doctype html><style>canvas{display:block}</style><canvas></canvas><script>document.title='Saved replay';</script>"


class RetainedArtifactFixture(StarshipMissionService):
    """Retain the real allowlist/path/hash reader with a fixed saved-state fixture."""
    def __init__(self, root):
        self.root = root
        result = root / ("run-" + "a" * 32) / "results"
        result.mkdir(parents=True)
        for name, body in (("report.html", REPORT), ("study.json", b'{"fixture":true}')):
            (result / name).write_bytes(body)
        self.hashes = {p.name: sha256(p.read_bytes()).hexdigest() for p in result.iterdir()}

    def status(self, session_id, plan_id=None):
        if (session_id, plan_id) != ("fixture-session", "fixture-plan"):
            raise StarshipMissionError("no_matching_starship_plan")
        return {"status": "verified", "execution": {"run_id": "a" * 32, "worker_receipt_verified": True, "artifact_sha256": self.hashes}}


@pytest.fixture
def artifact_app(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    for name, filename in (("TASK_STORE_DB_PATH", "tasks.db"), ("MEMORY_DB_PATH", "memory.db"),
                           ("AUDIT_LOG_PATH", "audit.log")):
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setenv("GATEWAY_API_KEY", API_KEY)
    monkeypatch.setenv("GATEWAY_CORS_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    service = RetainedArtifactFixture(tmp_path / "retained")
    monkeypatch.setattr(server, "get_starship_service", lambda: service)
    monkeypatch.setattr(server, "run_missionos_autonomy_conversation",
                        lambda *a, **k: pytest.fail("artifact display cannot enter an authority route"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    try:
        yield server.create_missionos_gateway().app, service
    finally:
        reset_task_store()
        reset_settings()
        audit._audit_logger = None


def policy(response):
    return {part.split()[0]: part.split()[1:] for part in
            response.headers["Content-Security-Policy"].split(";") if part.strip()}


@pytest.mark.parametrize("destination", ["document", "iframe"])
def test_report_response_is_opaque_even_when_opened_outside_operator_iframe(artifact_app, destination):
    app, _ = artifact_app
    response = TestClient(app).get(URL + "report.html", headers={"X-API-Key": API_KEY, "Sec-Fetch-Dest": destination})
    assert response.status_code == 200 and response.content == REPORT
    directives = policy(response)
    assert directives["sandbox"] == ["allow-scripts"]
    assert directives["script-src"] == directives["style-src"] == ["'unsafe-inline'"]
    assert directives["default-src"] == directives["connect-src"] == ["'none'"]
    assert directives["base-uri"] == directives["form-action"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'self'"]
    assert directives["img-src"] == ["data:", "blob:"]
    assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_json_artifact_remains_json_and_cannot_be_sniffed_as_executable_html(artifact_app):
    app, _ = artifact_app
    response = TestClient(app).get(URL + "study.json", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200 and response.json() == {"fixture": True}
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in response.headers


def test_report_sandbox_does_not_replace_auth_session_or_saved_hash_checks(artifact_app):
    app, service = artifact_app
    client = TestClient(app)
    assert client.get(URL + "report.html").status_code == 401
    assert client.get(URL.replace("fixture-session", "another-session") + "report.html",
                      headers={"X-API-Key": API_KEY}).status_code == 404
    assert client.get(URL + "unknown.html", headers={"X-API-Key": API_KEY}).status_code == 404
    path = service.root / ("run-" + "a" * 32) / "results/report.html"
    path.write_bytes(REPORT + b"<script>fetch('/missionos/starship/operator/actions')</script>")
    assert client.get(URL + "report.html", headers={"X-API-Key": API_KEY}).status_code == 404


def test_sandbox_null_origin_cannot_post_operator_actions_even_with_api_key(artifact_app):
    app, _ = artifact_app
    response = TestClient(app).post("/missionos/starship/operator/actions",
        headers={"X-API-Key": API_KEY, "Origin": "null"}, json={"action": "run"})
    assert response.status_code == 403 and response.json()["detail"] == "Browser origin is not allowed"


def test_fresh_loopback_http_serves_sandboxed_fixture_and_rejects_null_origin(artifact_app):
    """Actual TCP/HTTP middleware+artifact reader; Gateway lifecycle/physics are off."""
    app, _ = artifact_app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gateway = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = Thread(target=lambda: gateway.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not gateway.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.started
        base = f"http://127.0.0.1:{port}"
        with urlopen(Request(base + URL + "report.html", headers={"X-API-Key": API_KEY}), timeout=3) as response:
            assert response.status == 200 and response.read() == REPORT
            assert "sandbox allow-scripts;" in response.headers["Content-Security-Policy"]
            assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
        denied = Request(base + "/missionos/starship/operator/actions", method="POST",
            headers={"X-API-Key": API_KEY, "Origin": "null", "Content-Type": "application/json"},
            data=json.dumps({"action": "run"}).encode())
        with pytest.raises(HTTPError) as error:
            urlopen(denied, timeout=3)
        assert error.value.code == 403
    finally:
        gateway.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
