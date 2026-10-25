"""Provider image request boundary: actual loopback HTTP, no flight/provider."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
import uuid

from PIL import Image
import pytest

from src.intelligence import starship_visual_transport as transport
from src.intelligence.starship_visual_agent import build_payload, reconstruct_payload, decode_response
from src.runtime import starship_final_plan as planning
from src.runtime.starship_final_visual import FinalVisualPolicy
from src.runtime.starship_visual_contract import digest
from test_starship_final_visual import episode, visual_script, host, ROOT


@pytest.fixture(autouse=True)
def no_flight_or_external_provider(monkeypatch):
    from src.runtime import starship_sixdof as dynamics
    def no_step(*_args, **_kwargs):
        raise AssertionError("image contract cannot advance physics")
    monkeypatch.setattr(dynamics, "step", no_step)
    monkeypatch.setattr(dynamics, "_rk4", no_step)
    from src.runtime.starship_deployment_world import DeploymentWorld
    def static_scene(world):
        # Named scene fixture: no copied-satellite ephemeris integration. PNG,
        # secure frame reads, serializer and HTTP remain the production path.
        anchor = [0., 0., world.profile["payload"]["center_z_m"]]
        return {"schema": "missionos.starship_deployment_world.v1", "time_s": world._state.time_s,
            "attachment_body_m": anchor, "payload_center_body_m": [3., 0., anchor[2]],
            "payload_rotation_columns": [[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],
            "payload_dimensions_m": list(world.profile["payload"]["dimensions_m"]),
            "parent_state_sha256": digest(asdict(world._state)),
            "parent_vehicle_sha256": digest(asdict(world._vehicle)),
            "target_release_sha256": world._release_digests.get("satellite_13"),
            "child_state_sha256": digest(None), "child_propagation_steps": 0,
            "geometry_scope": "diagnostic_synthetic_static_scene_no_child_dynamics",
            "physical_execution": False}
    monkeypatch.setattr(DeploymentWorld, "snapshot", static_scene)
    original = transport._send_once
    def local_only(endpoint, *args, **kwargs):
        transport._fixture_endpoint(endpoint)
        return original(endpoint, *args, **kwargs)
    monkeypatch.setattr(transport, "_send_once", local_only)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv(transport.LIVE_OPT_IN_ENV, raising=False)


def frame_bytes(width, height):
    out = BytesIO()
    Image.new("RGB", (width, height), (9, 12, 18)).save(out, format="PNG")
    data = out.getvalue()
    return data, {"image_bytes": len(data), "image_sha256": sha256(data).hexdigest(),
                  "width": width, "height": height, "mime_type": "image/png"}


@pytest.mark.parametrize("width,height,accepted", [(384, 256, False), (256, 384, False),
    (384, 279, False), (279, 384, False), (280, 280, True), (384, 280, True)])
def test_provider_checks_each_side_independently_of_total_area(width, height, accepted):
    data, frame = frame_bytes(width, height)
    assert transport.validate_image(data, frame) == "png"  # Historical structural reader.
    if accepted:
        assert transport.validate_provider_image(data, frame) == "png"
    else:
        with pytest.raises(ValueError, match="visual_provider_image_dimensions"):
            transport.validate_provider_image(data, frame)


def test_new_registration_requires_280_but_historical_config_remains_readable():
    cfg = json.loads((ROOT/planning.CONFIG).read_text())
    assert cfg["visual_policy"]["height"] == FinalVisualPolicy().height == 280
    cfg["visual_policy"]["height"] = 256
    assert planning.configuration(cfg, provider_ready=False) == cfg
    with pytest.raises(ValueError, match="final_visual_provider_dimensions"):
        planning.configuration(cfg)
    old = FinalVisualPolicy(height=256)
    assert old.record()["height"] == 256
    with pytest.raises(ValueError, match="final_visual_provider_dimensions"):
        old.require_provider_dimensions()


def test_historical_wire_reconstructs_but_cannot_be_resent(tmp_path):
    observer, _, _ = episode(tmp_path)
    request = deepcopy(observer.requests[0]["request"])
    data, metadata = frame_bytes(384, 256)
    frame = request["manifest"]["frames"][0]
    frame.update(metadata)
    request["manifest"]["frames"] = [frame]
    request["manifest"]["manifest_sha256"] = digest({k:v for k,v in request["manifest"].items() if k != "manifest_sha256"})
    class Store:
        def read(self, _frame): return data
    refs = [{"frame_id": frame["frame_id"], "asset_reference": str(uuid.uuid4()),
             "image_sha256": frame["image_sha256"]}]
    payload, binding = reconstruct_payload(request, Store(), refs)
    assert payload["assets"] and binding["image_count"] == 1
    with pytest.raises(ValueError, match="visual_provider_image_dimensions"):
        build_payload(request, Store())
    with transport.fixture_provider([visual_script()]) as server:
        client = transport.VisualTransport(mode="loopback_fixture", fixture_endpoint=server["endpoint"],
            remaining=lambda: 8.)
        result, receipt = client.invoke(payload, request=request, binding=binding,
            decode=lambda value: decode_response(value, request))
        assert result is None and receipt["status"] == "invalid_payload_binding"
        assert not receipt["local_http_attempted"] and not receipt["hosted_model_attempted"]
        assert client.calls == 0 and not server["requests"]


def test_current_frames_cross_real_loopback_and_keep_model_claim_false(tmp_path):
    observer, _, clock = episode(tmp_path)
    frames = observer.requests[0]["request"]["manifest"]["frames"]
    assert all(f["width"] == 384 and f["height"] == 280 for f in frames)
    with transport.fixture_provider([visual_script()]) as server:
        reply = host(tmp_path, server["endpoint"], clock).assess(observer.requests[0])
        receipt = reply["result"]["provider_receipt"]
        assert receipt["status"] == "fixture_succeeded" and receipt["http_status"] == 200
        assert receipt["model_inference_invoked"] is False and receipt["hosted_model_attempted"] is False
        assert len(server["requests"]) == 1 and server["requests"][0]["image_header"]


@contextmanager
def error_server(body):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/systemone", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_400_keeps_bounded_diagnostics_without_prose_or_retry(tmp_path):
    observer, _, clock = episode(tmp_path)
    body = json.dumps({"error":{"code":"max_tokens_exceeded", "message":"private-provider-prose-and-input"}}).encode()
    with error_server(body) as (endpoint, calls):
        judge = host(tmp_path, endpoint, clock)
        reply = judge.assess(observer.requests[0])
        receipt = reply["result"]["provider_receipt"]
        assert receipt["status"] == "http_error" and receipt["http_status"] == 400
        assert receipt["model_inference_invoked"] is False and receipt["retries"] == 0
        detail = receipt["http_error_diagnostics"]
        assert detail["category"] == "max_tokens_exceeded"
        assert detail["body_sha256"] == sha256(body).hexdigest()
        assert detail["body_bytes_read"] == len(body) and detail["raw_body_recorded"] is False
        assert "private-provider-prose-and-input" not in json.dumps(reply)
        with pytest.raises(ValueError, match="replay_deadline_or_budget"):
            judge.assess(observer.requests[0])
        assert len(calls) == 1


@pytest.mark.parametrize("body,category", [(b"not JSON secret text", "http_error_body_not_json"),
    (b'{"error":"secret arbitrary reason"}', "unclassified_http_error"),
    (b'x'*(transport.MAX_RESPONSE_BYTES+1), "http_error_body_too_large")])
def test_error_diagnostics_never_copy_arbitrary_body(body, category):
    detail = transport._http_error_diagnostics(body, 400)
    assert detail["category"] == category and detail["raw_body_recorded"] is False
    assert "secret" not in json.dumps(detail)
    if len(body) > transport.MAX_RESPONSE_BYTES:
        assert detail["body_sha256"] is None and detail["body_complete_within_limit"] is False
