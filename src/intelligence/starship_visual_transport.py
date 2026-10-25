"""Bounded, opt-in Jev image HTTP; local fixtures never attest hosted inference.

This transport grants no mission authority. The caller owns the current request
and shared phase budget; ``audit`` can attach the existing M2 durable ledger.
An optional run-owned ledger also prevents replay across process restarts.
"""

from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import fcntl
import io
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import stat
from threading import Lock, Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request
import uuid

from PIL import Image

from .space_ops_investigator import _json, _provider_opener, canonical_bytes

JEV_MODEL = "jev-1.14.0"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
LIVE_OPT_IN_ENV = "MISSIONOS_STARSHIP_VISUAL_LIVE_ENABLED"
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 256 * 1024
MIN_PROVIDER_IMAGE_SIDE = 280
MAX_FRAMES = 6
MAX_PROVIDER_SECONDS = 8.0
MIN_PROVIDER_SECONDS = 3.0
APPLY_MARGIN_SECONDS = 2.0
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

# Keep categories finite: neither provider strings nor exception messages are
# diagnostic fields. The old broad names remain readable for prior receipts.
REJECTION_STAGES = {
    **dict.fromkeys(("visual_response_not_object", "visual_response_model_mismatch",
                     "visual_response_questions_or_model"), "response_envelope"),
    **dict.fromkeys(("visual_response_answers_shape", "visual_response_questions_mismatch"), "answers"),
    **dict.fromkeys(("visual_choice_not_object", "visual_choice_fields", "visual_choice_type",
                     "visual_choice_unknown", "visual_choice_probabilities_shape",
                     "visual_choice_probability_keys", "visual_choice_probability_values",
                     "visual_choice_probability_sum", "visual_choice_confidence",
                     "visual_choice_shape", "visual_choice_distribution"), "choice"),
    **dict.fromkeys(("visual_action_observation_mismatch", "visual_finding_without_visible_support"), "semantics"),
}


class VisualResponseRejected(ValueError):
    """Typed receiver failure containing only a fixed code and question index."""

    def __init__(self, category, question_index=None):
        if category not in REJECTION_STAGES or (question_index is not None and (
                type(question_index) is not int or not 0 <= question_index < 5 + MAX_FRAMES)):
            raise ValueError("visual_rejection_diagnostic_contract")
        super().__init__(category)
        self.category, self.question_index = category, question_index


def _json_shape(value):
    return {dict: "object", list: "array", str: "string", int: "number",
            float: "number", bool: "boolean", type(None): "null"}.get(type(value), "other")


def _response_diagnostics(response, questions, *, stage, question_index=None):
    """Topology and hashed model identity only; never retain response strings."""
    result = {"schema": "missionos.starship_visual_rejection_diagnostics.v1",
              "stage": stage, "response_json_shape": _json_shape(response),
              "expected_question_count": len(questions), "question_index": question_index}
    if type(response) is not dict:
        return result
    model, answers = response.get("model"), response.get("answers")
    result.update(response_model_shape=_json_shape(model), response_model_matches=model == JEV_MODEL,
                  response_model_utf8_bytes=None, response_model_sha256=None,
                  answers_json_shape=_json_shape(answers))
    if type(model) is str:
        encoded = model.encode("utf-8", errors="surrogatepass")
        result.update(response_model_utf8_bytes=len(encoded), response_model_sha256=sha256(encoded).hexdigest())
    if type(answers) is dict:
        result.update(received_question_count=len(answers),
                      missing_question_count=len(set(questions) - set(answers)),
                      unexpected_question_count=len(set(answers) - set(questions)))
    return result


def _safe_usage(response):
    usage = response.get("usage") if type(response) is dict else None
    if type(usage) is not dict:
        return None
    safe = {name: usage[name] for name in ("input_tokens", "output_tokens")
            if type(usage.get(name)) is int and 0 <= usage[name] <= 10**9}
    return safe or None


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_image(data, frame):
    """Inspect exact saved bytes; retain the historical 256-side read contract.

    New sends additionally require ``validate_provider_image``. This structural
    reader never resizes old evidence or authorizes a provider request.
    """
    if (type(data) is not bytes or not 0 < len(data) <= MAX_IMAGE_BYTES
            or type(frame) is not dict or frame.get("image_bytes") != len(data)
            or frame.get("image_sha256") != sha256(data).hexdigest()):
        raise ValueError("visual_image_byte_binding")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt, width, height = image.format, *image.size
            if (fmt not in ("PNG", "JPEG") or getattr(image, "n_frames", 1) != 1
                    or not 256 <= width <= 1568 or not 256 <= height <= 1568
                    or not 78400 <= width * height <= 2458624
                    or frame.get("width") != width or frame.get("height") != height
                    or frame.get("mime_type") != ("image/png" if fmt == "PNG" else "image/jpeg")):
                raise ValueError("visual_image_format_or_dimensions")
            image.verify()
        # verify() validates structure; load() catches incomplete pixel data.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("visual_image_invalid") from error
    return "png" if fmt == "PNG" else "jpg"



def validate_provider_image(data, frame):
    """Current provider limits, distinct from historical byte reconstruction.

    Official image contract: each side >=280, independently of total pixels.
    https://docs.typesafe.ai/go/images (checked 2026-10-22).
    """
    fmt = validate_image(data, frame)
    if min(frame["width"], frame["height"]) < MIN_PROVIDER_IMAGE_SIDE:
        raise ValueError("visual_provider_image_dimensions")
    return fmt


def _http_error_diagnostics(body, status):
    """Bounded shape/hash and allowlisted code only; never retain error prose."""
    result = {"schema": "missionos.starship_visual_http_error.v1",
              "http_status": status, "body_bytes_read": len(body),
              "body_complete_within_limit": len(body) <= MAX_RESPONSE_BYTES,
              "body_sha256": None, "json_shape": None,
              "category": "unclassified_http_error", "raw_body_recorded": False}
    if len(body) > MAX_RESPONSE_BYTES:
        result["category"] = "http_error_body_too_large"
        return result
    result["body_sha256"] = sha256(body).hexdigest()
    try:
        value = _json(body)
    except Exception:
        result["category"] = "http_error_body_not_json"
        return result
    result["json_shape"] = _json_shape(value)
    # Only this documented exact code is interpreted. Arbitrary provider error
    # strings (including explanations and echoed input) must not enter receipts.
    if type(value) is dict:
        error = value.get("error")
        codes = [value.get("code"), error]
        if type(error) is dict:
            codes.append(error.get("code"))
        if any(type(code) is str and code == "max_tokens_exceeded" for code in codes):
            result["category"] = "max_tokens_exceeded"
    return result

def _fixture_endpoint(endpoint):
    try:
        parts = urlsplit(endpoint)
        valid = (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1")
                 and parts.port is not None and 1 <= parts.port <= 65535
                 and parts.username is None and parts.password is None
                 and not parts.query and not parts.fragment and parts.path == "/v1/systemone")
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("visual_fixture_endpoint_must_be_literal_loopback")
    return endpoint


def _validate_payload_assets(payload, request, binding):
    """Check the serialized assets against the request, including direct callers."""
    from src.runtime.starship_visual_contract import validate_request
    request = validate_request(request)
    visual = request["evidence_kind"] == "visual"
    expected = {"model", "state", "questions"} | ({"assets"} if visual else set())
    if (set(payload) != expected or type(payload["state"]) is not dict
            or type(payload["questions"]) is not dict
            or any(type(question) is not dict or question.get("type") != "choice"
                   for question in payload["questions"].values())
            or binding["public_input_sha256"] != digest(payload["state"])
            or binding["question_contract_sha256"] != digest(payload["questions"])
            or binding["manifest_sha256"] != digest(request["manifest"])):
        raise ValueError("visual_wire_semantic_binding")
    frames = request["manifest"]["frames"]
    if not visual:
        if frames or binding["image_count"] != 0 or binding["image_bytes"] != 0:
            raise ValueError("numeric_request_contains_images")
        return
    assets, state_frames = payload["assets"], payload["state"].get("frames")
    if (type(assets) is not dict or type(state_frames) is not list
            or not 1 <= len(frames) == len(assets) == len(state_frames) <= MAX_FRAMES
            or binding["image_count"] != len(frames)):
        raise ValueError("visual_wire_asset_count")
    references = [row["image"] for row in state_frames]
    if len(set(references)) != len(references) or set(references) != set(assets):
        raise ValueError("visual_wire_reference_binding")
    hashes, total = [], 0
    for frame, reference in zip(frames, references):
        asset = assets[reference]
        if (type(asset) is not dict or set(asset) != {"type", "format", "data_base64"}
                or asset["type"] != "image" or type(asset["data_base64"]) is not str):
            raise ValueError("visual_wire_asset_shape")
        data = base64.b64decode(asset["data_base64"], validate=True)
        if asset["format"] != validate_provider_image(data, frame):
            raise ValueError("visual_wire_image_format")
        hashes.append(sha256(data).hexdigest())
        total += len(data)
    if (hashes != binding["ordered_frame_sha256"] or total != binding["image_bytes"]
            or total > MAX_FRAMES * MAX_IMAGE_BYTES):
        raise ValueError("visual_wire_image_hashes")


def _send_once(endpoint, wire, headers, *, timeout_s, active):
    """One HTTP attempt; cancellation retires an answer, never retries it."""
    deadline, completed = time.monotonic() + timeout_s, Queue(maxsize=1)

    def send():
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not active():
                return
            request = Request(endpoint, data=wire, method="POST", headers=headers)
            with _provider_opener().open(request, timeout=remaining) as response:
                code = response.status
                body = response.read(MAX_RESPONSE_BYTES + 1)
            result = ("response", body, code)
        except HTTPError as error:
            try:
                body = error.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                body = None
            finally:
                error.close()
            result = ("http_error", body, error.code)
        except Exception:
            result = ("transport_failed", None, None)
        if time.monotonic() < deadline and active():
            completed.put_nowait(result)

    if not active():
        return "cancelled_request", None, None
    Thread(target=send, daemon=True, name="starship-visual-http").start()
    while True:
        if not active():
            return "cancelled_request", None, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "transport_timeout", None, None
        try:
            result = completed.get(timeout=min(remaining, 0.05))
        except Empty:
            continue
        if time.monotonic() >= deadline:
            return "transport_timeout", None, None
        return result if active() else ("cancelled_request", None, None)


class VisualAttemptLedger:
    """A run-bound optional ledger. A failed or interrupted send stays consumed."""

    def __init__(self, path, *, run_id, authority_sha256, mode, maximum_calls=2):
        if (not isinstance(path, (str, Path)) or type(run_id) is not str
                or not _ID.fullmatch(run_id) or type(authority_sha256) is not str
                or not _HASH.fullmatch(authority_sha256)
                or mode not in ("live", "loopback_fixture")
                or type(maximum_calls) is not int or not 0 <= maximum_calls <= 2):
            raise ValueError("visual_ledger_configuration")
        self.path = Path(path)
        self.identity = {"schema": "missionos.starship_visual_attempts.v1", "run_id": run_id,
                         "authority_sha256": authority_sha256, "transport_mode": mode,
                         "maximum_calls": maximum_calls}
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("visual_ledger_owned_directory_required")

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("visual_ledger_lock_not_regular")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if self.path.is_symlink():
                raise ValueError("visual_ledger_symlink")
            if self.path.exists():
                if self.path.stat().st_size > 128 * 1024:
                    raise ValueError("visual_ledger_too_large")
                record = _json(self.path.read_bytes())
                if (type(record) is not dict or set(record) != set(self.identity) | {"attempts"}
                        or any(record.get(k) != v for k, v in self.identity.items())
                        or type(record["attempts"]) is not list
                        or len(record["attempts"]) > self.identity["maximum_calls"]):
                    raise ValueError("visual_ledger_binding")
            else:
                record = {**self.identity, "attempts": []}
            yield record
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(canonical_bytes(record))
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def reserve(self, request, receipt):
        if (request["run_id"] != self.identity["run_id"]
                or request["authority_sha256"] != self.identity["authority_sha256"]):
            raise ValueError("visual_ledger_request_binding")
        with self._locked() as record:
            attempts = record["attempts"]
            if (len(attempts) >= self.identity["maximum_calls"]
                    or any(row["request_id"] == request["request_id"] for row in attempts)):
                raise ValueError("visual_attempt_consumed_or_budget_exhausted")
            attempts.append({"attempt_id": receipt["attempt_id"], "request_id": request["request_id"],
                             "episode_id": request["episode_id"],
                             "semantic_request_sha256": receipt["semantic_request_sha256"],
                             "wire_sha256": receipt["request_sha256"], "status": "reserved",
                             "completed": False, "model_inference_invoked": False})

    def finish(self, receipt):
        with self._locked() as record:
            rows = [row for row in record["attempts"] if row["attempt_id"] == receipt["attempt_id"]]
            if len(rows) != 1:
                raise ValueError("visual_attempt_not_reserved")
            rows[0].update(status=receipt["status"], completed=True,
                           model_inference_invoked=receipt["model_inference_invoked"])


class VisualTransport:
    """The same bounded serializer for fixture HTTP and explicitly enabled Jev."""

    def __init__(self, *, mode="off", active=lambda: True, remaining=lambda: 0.0,
                 fixture_endpoint=None, audit=None, ledger=None, maximum_calls=2,
                 live_authorized=lambda: False):
        if (mode not in ("off", "loopback_fixture", "live")
                or not all(callable(f) for f in (active, remaining, live_authorized))
                or (audit is not None and not callable(audit))
                or type(maximum_calls) is not int or not 0 <= maximum_calls <= 2):
            raise ValueError("visual_transport_configuration")
        if mode == "loopback_fixture":
            self.endpoint = _fixture_endpoint(fixture_endpoint)
        elif fixture_endpoint is not None:
            raise ValueError("visual_live_endpoint_override_forbidden")
        else:
            self.endpoint = JEV_ENDPOINT
        self.mode, self.active, self.remaining = mode, active, remaining
        self.audit, self.ledger, self.maximum_calls = audit, ledger, maximum_calls
        self.live_authorized, self.calls, self.seen = live_authorized, 0, set()
        self.lock = Lock()

    def _active(self):
        try:
            return self.active() is True
        except Exception:
            return False

    def _remaining(self):
        try:
            value = self.remaining()
            return float(value) if _number(value) else 0.0
        except Exception:
            return 0.0

    def invoke(self, payload, *, request, binding, decode):
        fixture = self.mode == "loopback_fixture"
        receipt = {
            "schema": "missionos.starship_visual_invocation.v1", "transport_mode": self.mode,
            "provider": "loopback_fixture" if fixture else "typesafe" if self.mode == "live" else None,
            "status": "not_invoked", "attempt_id": None,
            "semantic_request_sha256": digest(request), **binding,
            "requested_model_id": JEV_MODEL, "model_id": None,
            "request_sha256": None, "request_bytes": 0, "response_sha256": None,
            "response_bytes_read": 0, "local_http_attempted": False,
            "hosted_model_attempted": False, "call_attempted": False,
            "response_received": False, "complete_response_observed": False,
            "call_succeeded": False, "model_inference_invoked": False,
            "model_inference_status": "not_invoked", "http_status": None,
            "retries": 0, "redirects": 0, "application_margin_s": APPLY_MARGIN_SECONDS,
            "timeout_seconds": 0.0, "remaining_budget_s": 0.0, "latency_ms": 0.0,
            "maximum_request_bytes": MAX_REQUEST_BYTES, "maximum_response_bytes": MAX_RESPONSE_BYTES,
            "maximum_provider_s": MAX_PROVIDER_SECONDS, "provider_usage": None,
            "raw_prompt_recorded": False, "raw_response_recorded": False,
            "approval_granted": False, "dispatch": False, "execution_authorized": False,
            "physical_execution": False,
        }
        if self.mode == "off":
            receipt["status"] = "off"
            return None, receipt
        if not self._active():
            receipt["status"] = "cancelled_request"
            return None, receipt
        if type(payload) is not dict or payload.get("model") != JEV_MODEL:
            raise ValueError("visual_payload_model")
        wire = canonical_bytes(payload)
        receipt.update(request_sha256=sha256(wire).hexdigest(), request_bytes=len(wire))
        if len(wire) > MAX_REQUEST_BYTES:
            receipt["status"] = "request_too_large"
            return None, receipt
        try:
            _validate_payload_assets(payload, request, binding)
        except (ValueError, TypeError, KeyError, OSError):
            receipt["status"] = "invalid_payload_binding"
            return None, receipt
        remaining = self._remaining()
        timeout = min(MAX_PROVIDER_SECONDS, remaining - APPLY_MARGIN_SECONDS)
        receipt.update(timeout_seconds=max(0.0, timeout), remaining_budget_s=remaining)
        if timeout < MIN_PROVIDER_SECONDS:
            receipt["status"] = "insufficient_deadline_headroom"
            return None, receipt
        headers = {"Content-Type": "application/json"}
        if payload.get("assets"):
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        if fixture:
            headers["X-MissionOS-Transport"] = "loopback-fixture"
        else:
            if (os.environ.get(LIVE_OPT_IN_ENV) != "1" or self.live_authorized() is not True):
                receipt["status"] = "live_not_authorized"
                return None, receipt
            key = os.environ.get("TYPESAFE_API_KEY", "").strip()
            if not key or len(key) > 4096 or any(c.isspace() for c in key):
                receipt["status"] = "credential_unavailable"
                return None, receipt
            headers["Authorization"] = "Bearer " + key
        with self.lock:
            identity = (request["run_id"], request["request_id"])
            if self.calls >= self.maximum_calls or identity in self.seen:
                receipt["status"] = "attempt_consumed_or_budget_exhausted"
                return None, receipt
            self.calls += 1
            self.seen.add(identity)
            receipt["attempt_id"] = uuid.uuid4().hex
            try:
                if self.ledger:
                    self.ledger.reserve(request, receipt)
                if self.audit:
                    self.audit("started", "typesafe", receipt)
            except (ValueError, OSError):
                receipt["status"] = "attempt_ledger_rejected"
                return None, receipt
        # Account for reservation/fsync time as part of the same request budget.
        remaining = self._remaining()
        timeout = min(timeout, remaining - APPLY_MARGIN_SECONDS)
        receipt.update(timeout_seconds=max(0.0, timeout), remaining_budget_s=remaining)
        started = time.monotonic()
        try:
            if not self._active() or timeout < MIN_PROVIDER_SECONDS:
                receipt["status"] = "cancelled_after_reservation"
                return None, receipt
            receipt.update(local_http_attempted=fixture, hosted_model_attempted=not fixture,
                           call_attempted=not fixture,
                           model_inference_status="fixture_not_inference" if fixture else "unconfirmed_after_attempt")
            status, body, code = _send_once(
                self.endpoint, wire, headers, timeout_s=timeout,
                active=lambda: self._active() and self._remaining() > APPLY_MARGIN_SECONDS)
            receipt.update(status=status, http_status=code)
            if status == "http_error" and type(body) is bytes:
                receipt["http_error_diagnostics"] = _http_error_diagnostics(body, code)
            if status != "response" or type(body) is not bytes:
                return None, receipt
            receipt.update(response_received=True, response_bytes_read=len(body))
            if len(body) > MAX_RESPONSE_BYTES:
                receipt["status"] = "response_too_large"
                return None, receipt
            receipt.update(complete_response_observed=True, response_sha256=sha256(body).hexdigest())
            parsed, response = False, None
            try:
                response = _json(body)
                parsed = True
                # Usage is independent of semantic acceptance. Only bounded
                # native integers are retained, even for a rejected answer.
                receipt["provider_usage"] = _safe_usage(response)
                result = decode(response)
            except Exception as error:
                receipt["status"] = "invalid_response"
                # Never call str(error): arbitrary exception messages can echo
                # response content. Read only fixed, validated error codes.
                category = error.args[0] if type(error) is ValueError and error.args else None
                index = None
                if isinstance(error, VisualResponseRejected):
                    category, index = error.category, error.question_index
                category = category if type(category) is str and category in REJECTION_STAGES else None
                receipt["rejection_category"] = category or "response_json_or_structure"
                if type(index) is not int or not 0 <= index < len(payload["questions"]):
                    index = None
                receipt["rejection_diagnostics"] = (_response_diagnostics(response, payload["questions"],
                    stage=REJECTION_STAGES.get(category, "semantic_decode"), question_index=index)
                    if parsed else {"schema": "missionos.starship_visual_rejection_diagnostics.v1",
                                    "stage": "response_json"})
                return None, receipt
            if not self._active() or self._remaining() <= APPLY_MARGIN_SECONDS:
                receipt["status"] = "response_after_deadline"
                return None, receipt
            receipt.update(status="fixture_succeeded" if fixture else "succeeded",
                           model_id=JEV_MODEL, call_succeeded=not fixture,
                           model_inference_invoked=not fixture,
                           model_inference_status="fixture_not_inference" if fixture else "confirmed_by_valid_response")
            return result, receipt
        finally:
            receipt["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                if self.ledger:
                    self.ledger.finish(receipt)
                if self.audit:
                    self.audit("finished", "typesafe", receipt)
            except (ValueError, OSError):
                # Evidence failure is visible to the result consumer; never silently
                # adopt a success whose durable accounting could not be completed.
                receipt["status"] = "attempt_ledger_completion_failed"
                receipt["call_succeeded"] = False


@contextmanager
def fixture_provider(scripted_answers):
    """Real localhost HTTP with predeclared answers; never inspects scene truth.

``scripted_answers`` supplies one map of question IDs to selected choices per
request. ``frame_support`` may supply the fixed choice for all frame questions.
The captured audit contains request hashes/sizes and the fixture's structured
answer envelope, allowing an independent verifier to bind the decoded diagnosis
to the exact response bytes. It never records input image bytes or secrets.
"""
    scripts = list(scripted_answers)
    if not 0 <= len(scripts) <= 2 or any(type(script) is not dict for script in scripts):
        raise ValueError("visual_fixture_script_budget")
    observed, lock = [], Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            code, response = 400, {"error": "fixture_request_rejected"}
            observed_record = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (self.path != "/v1/systemone" or not 0 < size <= MAX_REQUEST_BYTES
                        or self.headers.get("Authorization") is not None
                        or self.headers.get("Content-Type") != "application/json"
                        or self.headers.get("X-MissionOS-Transport") != "loopback-fixture"):
                    raise ValueError("fixture_http_shape")
                wire = self.rfile.read(size)
                payload = _json(wire)
                if payload.get("model") != JEV_MODEL:
                    raise ValueError("fixture_model")
                assets = payload.get("assets", {})
                if type(assets) is not dict or len(assets) > MAX_FRAMES:
                    raise ValueError("fixture_assets")
                if bool(assets) != (self.headers.get("TypeSafe-Danger-Zone-Images") == "true"):
                    raise ValueError("fixture_image_header")
                hashes, total = [], 0
                for reference, asset in assets.items():
                    if (type(reference) is not str or len(reference) != 36
                            or set(asset) != {"type", "format", "data_base64"}
                            or asset["type"] != "image" or asset["format"] not in ("png", "jpg")):
                        raise ValueError("fixture_asset_shape")
                    data = base64.b64decode(asset["data_base64"], validate=True)
                    if not 0 < len(data) <= MAX_IMAGE_BYTES:
                        raise ValueError("fixture_image_size")
                    with Image.open(io.BytesIO(data)) as image:
                        width, height = image.size
                    frame = {"image_bytes": len(data), "image_sha256": sha256(data).hexdigest(),
                             "width": width, "height": height,
                             "mime_type": "image/png" if asset["format"] == "png" else "image/jpeg"}
                    if validate_provider_image(data, frame) != asset["format"]:
                        raise ValueError("fixture_image_format")
                    hashes.append(sha256(data).hexdigest())
                    total += len(data)
                with lock:
                    index = len(observed)
                    if index >= len(scripts):
                        raise ValueError("fixture_script_exhausted")
                    script = scripts[index]
                    observed_record = {"request_sha256": sha256(wire).hexdigest(), "request_bytes": size,
                                       "image_hashes": hashes, "image_bytes": total,
                                       "question_ids": sorted(payload["questions"]),
                                       "image_header": bool(assets)}
                    observed.append(observed_record)
                answers = {}
                for name, question in payload["questions"].items():
                    choice = script.get(name, script.get("frame_support") if name.startswith("frame_") else None)
                    if choice not in question["criteria"]:
                        raise ValueError("fixture_script_choice")
                    answers[name] = {"type": "choice", "choice": choice,
                                     "probabilities": {k: float(k == choice) for k in question["criteria"]},
                                     "confidence": 1.0}
                code, response = 200, {"model": JEV_MODEL, "answers": answers,
                                       "usage": {"input_tokens": 0, "output_tokens": 0}}
            except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
                pass
            body = canonical_bytes(response)
            if len(body) > MAX_RESPONSE_BYTES:
                code, response = 400, {"error": "fixture_response_too_large"}
                body = canonical_bytes(response)
            if observed_record is not None:
                with lock:
                    observed_record.update(native_response=_json(body), response_sha256=sha256(body).hexdigest(),
                                           response_bytes=len(body), response_http_status=code)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"endpoint": f"http://127.0.0.1:{server.server_port}/v1/systemone", "requests": observed}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
