from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError, URLError


SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "preflight_groot_lerobot_semantic_repair.py"
)
SPEC = importlib.util.spec_from_file_location("groot_lerobot_preflight", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Response:
    status = 206

    def __init__(self, url: str, *, revision: str, status: int = 206) -> None:
        self._url = url
        self.status = status
        self.headers = {"X-Repo-Commit": revision}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self) -> str:
        return self._url


def test_gate_passes_only_when_checkpoint_and_cosmos_are_accessible(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        assert timeout == 20
        if MODULE.COSMOS_REPOSITORY in request.full_url:
            assert request.headers["Authorization"] == "Bearer test-token"
            revision = MODULE.COSMOS_REVISION
        else:
            assert "Authorization" not in request.headers
            revision = MODULE.CHECKPOINT_REVISION
        return _Response(request.full_url, revision=revision)

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is True
    assert report["checkpoint"]["public_access_required"] is True
    assert report["checkpoint"]["resolution"] == "direct_success"
    assert report["next_stage_gate"] == {"l4_baseline_preconditions_met": True}
    assert report["authority"] == {
        "human_approval_created": False,
        "l4_provisioning_authorized": False,
        "semantic_repair_authorized": False,
        "dispatch_authority_created": False,
        "physical_execution_authorized": False,
    }


def test_missing_token_blocks_before_gated_cosmos_probe(monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        assert "Authorization" not in request.headers
        return _Response(request.full_url, revision=MODULE.CHECKPOINT_REVISION)

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is False
    assert report["cosmos_processor"]["failure_code"] == "huggingface_token_missing"
    assert len(calls) == 1


def test_gated_repository_rejection_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        if MODULE.COSMOS_REPOSITORY in request.full_url:
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        return _Response(request.full_url, revision=MODULE.CHECKPOINT_REVISION)

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is False
    assert report["cosmos_processor"]["failure_code"] == "http_401"
    assert report["next_stage_gate"]["l4_baseline_preconditions_met"] is False


def test_revision_mismatch_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        return _Response(request.full_url, revision="0" * 40)

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is False
    assert report["checkpoint"]["failure_code"] == "revision_mismatch"


def test_transport_error_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        raise URLError("offline")

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is False
    assert report["checkpoint"]["failure_code"] == "transport_error:URLError"


def test_token_is_never_written_to_report(monkeypatch) -> None:
    token = "sentinel-secret-token"
    monkeypatch.setenv("HF_TOKEN", token)

    def opener(request, *, timeout):
        revision = (
            MODULE.COSMOS_REVISION
            if MODULE.COSMOS_REPOSITORY in request.full_url
            else MODULE.CHECKPOINT_REVISION
        )
        return _Response(request.full_url, revision=revision)

    report = MODULE.run_preflight(opener=opener)

    assert token not in json.dumps(report)


def test_redirects_are_never_followed() -> None:
    handler = MODULE._RejectRedirects()

    assert handler.redirect_request(None, None, 307, "redirect", {}, "https://cdn") is None


def test_matching_revision_redirect_is_recorded_without_following(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        revision = (
            MODULE.COSMOS_REVISION
            if MODULE.COSMOS_REPOSITORY in request.full_url
            else MODULE.CHECKPOINT_REVISION
        )
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"X-Repo-Commit": revision},
            None,
        )

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is True
    assert report["checkpoint"]["accessible"] is True
    assert report["checkpoint"]["failure_code"] is None
    assert report["checkpoint"]["redirect_followed"] is False
    assert report["checkpoint"]["resolution"] == "redirect_not_followed"
    assert report["cosmos_processor"]["resolution"] == "redirect_not_followed"


def test_non_success_status_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def opener(request, *, timeout):
        return _Response(request.full_url, revision=MODULE.CHECKPOINT_REVISION, status=404)

    report = MODULE.run_preflight(opener=opener)

    assert report["gate_passed"] is False
    assert report["checkpoint"]["failure_code"] == "http_404"


def test_main_exit_codes(monkeypatch, capsys) -> None:
    monkeypatch.delenv(MODULE.OPT_IN, raising=False)
    assert MODULE.main() == 3
    assert json.loads(capsys.readouterr().out)["status"] == "not_run"

    monkeypatch.setenv(MODULE.OPT_IN, "1")
    monkeypatch.setattr(MODULE, "run_preflight", lambda: {"gate_passed": True})
    assert MODULE.main() == 0
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True

    monkeypatch.setattr(MODULE, "run_preflight", lambda: {"gate_passed": False})
    assert MODULE.main() == 2
    assert json.loads(capsys.readouterr().out)["gate_passed"] is False
