"""One deadline-bounded streaming diagnostic for a frozen capability request.

Arrival timings are client observations, not the provider's inference start.
No raw stream, reasoning transcript, or free-form operational summary is saved.
This diagnostic neither approves nor executes a mission action.
"""
from contextlib import contextmanager
from hashlib import sha256
import http.client
import math
import signal
import threading
import time

from .space_ops_investigator import _json, canonical_bytes

SCHEMA = "missionos.ship_capability_stream_probe.v1"
HOST = "api.deepseek.com"
PATH = "/chat/completions"
MODEL = "deepseek-flash"
MAX_REQUEST_BYTES = 16*1024
MAX_RAW_BYTES = 8*1024*1024
MAX_EVENT_BYTES = 64*1024
MAX_ANSWER_CHARS = 8000
MAX_WALL_S = 300.0
_PAYLOAD_FIELDS = {"model", "stream", "thinking", "reasoning_effort", "max_tokens", "response_format", "messages"}
_ACTIONS = {"continue_nominal_assumption", "collect_status", "adopt_estimated_capability"}
_FINISHES = {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}
_TIMINGS = ("connect_done_s", "request_sent_s", "headers_s", "first_byte_s", "first_event_s",
            "first_reasoning_delta_s", "first_answer_delta_s", "finish_s", "done_s", "eof_s", "closed_s")


class _Deadline(Exception):
    pass


class _InvalidStream(Exception):
    def __init__(self, category):
        self.category = category


def _payload(wire, stream):
    if type(wire) is not bytes or not 0 < len(wire) <= MAX_REQUEST_BYTES:
        raise ValueError("invalid_stream_probe_request")
    try:
        value = _json(wire)
        if (type(value) is not dict or set(value) != _PAYLOAD_FIELDS
                or value["model"] != MODEL or value["stream"] is not stream
                or value["thinking"] != {"type": "enabled"} or value["reasoning_effort"] != "low"
                or type(value["max_tokens"]) is not int or value["max_tokens"] != 16384
                or value["response_format"] != {"type": "json_object"}):
            raise ValueError
        messages = value["messages"]
        if (type(messages) is not list or len(messages) != 2
                or any(type(m) is not dict or set(m) != {"role", "content"}
                    or type(m["content"]) is not str for m in messages)
                or messages[0]["role"] != "system" or messages[1]["role"] != "user"):
            raise ValueError
        public = _json(messages[1]["content"])
        actions = public["allowed_actions"]
        evidence = public["evidence"]
        if (type(actions) is not list or not actions or len(actions) > len(_ACTIONS)
                or any(type(a) is not str or a not in _ACTIONS for a in actions)
                or len(set(actions)) != len(actions) or type(evidence) is not dict or not evidence
                or any(type(k) is not str or not k or len(k) > 256 for k in evidence)):
            raise ValueError
        if any(v is not None and type(v) not in (str, bool, int, float) for v in evidence.values()):
            raise ValueError
        # Canonicalization independently rejects non-finite values anywhere.
        canonical_bytes(value)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("invalid_stream_probe_request") from None
    return value, public


def make_stream_request(original):
    """Only change the frozen nonstream request's stream flag."""
    value, _ = _payload(original, False)
    original_semantics = canonical_bytes(value)
    value["stream"] = True
    wire = canonical_bytes(value)
    if len(wire) > MAX_REQUEST_BYTES:
        raise ValueError("invalid_stream_probe_request")
    metadata = {"schema": "missionos.ship_capability_stream_request.v1",
        "original_sha256": sha256(original).hexdigest(), "original_request_bytes": len(original),
        "original_canonical_sha256": sha256(original_semantics).hexdigest(),
        "stream_request_sha256": sha256(wire).hexdigest(), "stream_request_bytes": len(wire),
        "changed_fields": ["stream"], "all_other_fields_equal": True,
        "raw_prompt_recorded": False, "provider_calls": 0, "physical_runs": 0}
    return wire, metadata


@contextmanager
def _hard_deadline(wall_limit_s):
    """Bound DNS, TLS, writes and reads; restore the caller's alarm state."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise ValueError("stream_probe_main_thread_required")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(_signum, _frame):
        raise _Deadline
    signal.signal(signal.SIGALRM, expired)
    first = min(wall_limit_s, old_timer[0]) if old_timer[0] > 0 else wall_limit_s
    signal.setitimer(signal.ITIMER_REAL, first)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(1e-6, old_timer[0]-(time.monotonic()-started)), old_timer[1])
        elif old_timer[1] > 0:
            signal.setitimer(signal.ITIMER_REAL, 0, old_timer[1])


def _safe_usage(value, receipt):
    if value is None:
        return
    if type(value) is not dict:
        raise _InvalidStream("usage_shape")
    usage = receipt["provider_usage"]
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if name in value:
            count = value[name]
            if type(count) is not int or not 0 <= count <= 10**9:
                raise _InvalidStream("usage_count")
            usage[name] = count
    details = value.get("completion_tokens_details")
    if details is not None:
        if type(details) is not dict:
            raise _InvalidStream("usage_shape")
        if "reasoning_tokens" in details:
            count = details["reasoning_tokens"]
            if type(count) is not int or not 0 <= count <= 10**9:
                raise _InvalidStream("usage_count")
            usage["reasoning_tokens"] = count
    if "completion_tokens" in usage and usage.get("reasoning_tokens", 0) > usage["completion_tokens"]:
        raise _InvalidStream("usage_count")


def probe(wire, key, *, wall_limit_s, mode="live", connection_factory=None, clock=time.monotonic):
    """Observe one response. A fixture connection can never count as inference."""
    receipt = {"schema": SCHEMA, "mode": mode if mode in ("live", "fixture") else "invalid",
        "status": "invalid_configuration", "phase": "configuration", "error_type": None,
        "validation_error": None, "request_sha256": sha256(wire).hexdigest() if type(wire) is bytes else None,
        "request_bytes": len(wire) if type(wire) is bytes else None, "requested_model_id": MODEL,
        "reported_model_id": None, "finish_reason": None, "provider_usage": {},
        "timings": {name: None for name in _TIMINGS},
        "counts": {"raw_bytes": 0, "sse_events": 0, "reasoning_chars": 0, "answer_chars": 0},
        "http_status": None, "http_request_sent": False, "call_attempted": False,
        "response_received": False, "complete_response_observed": False,
        "valid_answer_observed": False, "model_inference_invoked": False,
        "inference_response_observed": False,
        "model_inference_evidence_kind": "client_observed_provider_output_delta",
        "action": None, "reference_ids": [], "summary_char_count": 0,
        "retries": 0, "redirects": 0, "raw_prompt_recorded": False,
        "raw_response_recorded": False, "raw_reasoning_recorded": False,
        "free_prose_recorded": False, "timings_are_client_arrivals": True,
        "internal_inference_start_observed": False, "dispatch": False, "physical_execution": False}
    connection = None
    try:
        if (mode not in ("live", "fixture") or (connection_factory is not None and mode != "fixture")
                or (mode == "fixture" and connection_factory is None)
                or (connection_factory is not None and not callable(connection_factory))
                or type(key) is not str or not key or len(key) > 4096 or any(c.isspace() for c in key)
                or type(wall_limit_s) not in (int, float) or not math.isfinite(wall_limit_s)
                or not 0 < wall_limit_s <= MAX_WALL_S or not callable(clock)):
            return receipt
        receipt["phase"] = "request_validation"
        try:
            _, public = _payload(wire, True)
        except ValueError:
            receipt.update(status="invalid_request", validation_error="request_shape")
            return receipt
        if key.encode() in wire:
            receipt.update(status="invalid_request", validation_error="credential_in_request")
            return receipt
        started = clock()
        def elapsed():
            result = clock()-started
            if type(result) not in (int, float) or not math.isfinite(result) or result < 0 or result >= wall_limit_s:
                raise _Deadline
            return result
        def stamp(name):
            if receipt["timings"][name] is None:
                receipt["timings"][name] = elapsed()
        def timeout_socket():
            remaining = max(.001, wall_limit_s-elapsed())
            if getattr(connection, "sock", None) is not None:
                connection.sock.settimeout(remaining)

        answer_parts = []
        finished = done = False
        saw_model = False
        with _hard_deadline(wall_limit_s):
            receipt["phase"] = "connect"
            connection = (connection_factory(HOST, timeout=wall_limit_s) if connection_factory is not None
                          else http.client.HTTPSConnection(HOST, timeout=wall_limit_s))
            receipt["call_attempted"] = mode == "live"
            connection.connect()
            stamp("connect_done_s")
            timeout_socket()
            receipt["phase"] = "request_send"
            connection.request("POST", PATH, body=wire, headers={"Content-Type": "application/json",
                "Accept": "text/event-stream", "Authorization": "Bearer "+key})
            receipt["http_request_sent"] = True
            stamp("request_sent_s")
            timeout_socket()
            receipt["phase"] = "headers"
            response = connection.getresponse()
            stamp("headers_s")
            status = response.status
            receipt["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
            if status != 200:
                raise _InvalidStream("http_status")
            header = response.getheader("Content-Type", "")
            content_type = header.split(";", 1)[0].strip().lower() if type(header) is str else None
            if content_type != "text/event-stream":
                raise _InvalidStream("content_type")
            receipt["response_received"] = True
            receipt["phase"] = "body"
            buffer = b""
            data_lines = []
            event_bytes = 0

            def event():
                nonlocal finished, done, saw_model
                if not data_lines:
                    return
                stamp("first_event_s")
                receipt["counts"]["sse_events"] += 1
                if done:
                    raise _InvalidStream("event_after_done")
                body = "\n".join(data_lines)
                if body == "[DONE]":
                    if not finished:
                        raise _InvalidStream("done_before_finish")
                    done = True
                    stamp("done_s")
                    return
                try:
                    value = _json(body)
                except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
                    raise _InvalidStream("event_json") from None
                if type(value) is not dict or value.get("model") != MODEL:
                    reported = value.get("model") if type(value) is dict else None
                    receipt["reported_model_id"] = reported if reported in (MODEL, "deepseek-v4-flash", "deepseek-v4-pro") else "unknown"
                    raise _InvalidStream("model_mismatch")
                saw_model = True
                receipt["reported_model_id"] = MODEL
                _safe_usage(value.get("usage"), receipt)
                choices = value.get("choices")
                if type(choices) is not list or len(choices) > 1:
                    raise _InvalidStream("choices_shape")
                if not choices:  # A terminal usage-only chunk has no delta.
                    if value.get("usage") is None:
                        raise _InvalidStream("empty_chunk")
                    return
                choice = choices[0]
                if type(choice) is not dict or type(choice.get("index")) is not int or choice["index"] != 0:
                    raise _InvalidStream("choice_shape")
                delta = choice.get("delta")
                if type(delta) is not dict or set(delta)-{"role", "content", "reasoning_content", "tool_calls", "function_call"}:
                    raise _InvalidStream("delta_shape")
                if delta.get("tool_calls") or delta.get("function_call"):
                    raise _InvalidStream("tool_call")
                if "role" in delta and delta["role"] not in (None, "assistant"):
                    raise _InvalidStream("role")
                for field, timing, count in (("reasoning_content", "first_reasoning_delta_s", "reasoning_chars"),
                                             ("content", "first_answer_delta_s", "answer_chars")):
                    text = delta.get(field)
                    if text is None:
                        continue
                    if type(text) is not str:
                        raise _InvalidStream("delta_text_shape")
                    if text:
                        if finished:
                            raise _InvalidStream("delta_after_finish")
                        stamp(timing)
                        if mode == "live":
                            # A valid provider output delta is observable evidence;
                            # this is not a measurement of internal inference start.
                            receipt["inference_response_observed"] = True
                            receipt["model_inference_invoked"] = True
                        receipt["counts"][count] += len(text)
                        if field == "content":
                            if receipt["counts"][count] > MAX_ANSWER_CHARS:
                                raise _InvalidStream("answer_size")
                            answer_parts.append(text)
                finish = choice.get("finish_reason")
                if finish is not None:
                    if type(finish) is not str or finish not in _FINISHES:
                        raise _InvalidStream("finish_reason")
                    receipt["finish_reason"] = finish
                    stamp("finish_s")
                    if finished or finish != "stop":
                        raise _InvalidStream("finish_reason")
                    finished = True

            while True:
                elapsed()
                timeout_socket()
                block = response.read1(4096)
                if not block:
                    stamp("eof_s")
                    break
                if type(block) is not bytes:
                    raise _InvalidStream("body_type")
                stamp("first_byte_s")
                receipt["counts"]["raw_bytes"] += len(block)
                if receipt["counts"]["raw_bytes"] > MAX_RAW_BYTES:
                    raise _InvalidStream("stream_size")
                buffer += block
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    if len(raw) > MAX_EVENT_BYTES:
                        raise _InvalidStream("line_size")
                    if not raw:
                        event()
                        data_lines = []
                        event_bytes = 0
                        if done:
                            # [DONE] ends the protocol. HTTP keepalive/EOF is not
                            # a prerequisite for a complete answer; close below.
                            buffer = b""
                            break
                        continue
                    event_bytes += len(raw)+1
                    if event_bytes > MAX_EVENT_BYTES:
                        raise _InvalidStream("event_size")
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeError:
                        raise _InvalidStream("event_encoding") from None
                    if line.startswith("data:"):
                        content = line[5:]
                        data_lines.append(content[1:] if content.startswith(" ") else content)
                    # SSE comments and non-data fields carry no answer content.
                if done:
                    break
                if len(buffer) > MAX_EVENT_BYTES:
                    raise _InvalidStream("line_size")
            if buffer or data_lines:
                raise _InvalidStream("incomplete_event")
            if not (saw_model and finished and done):
                raise _InvalidStream("incomplete_stream")
            receipt["complete_response_observed"] = True
            receipt["phase"] = "answer_validation"
            answer_text = "".join(answer_parts)
            if key in answer_text:
                raise _InvalidStream("credential_in_answer")
            try:
                answer = _json(answer_text)
            except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
                raise _InvalidStream("answer_json") from None
            if (type(answer) is not dict or set(answer) != {"action", "summary", "reference_ids"}
                    or type(answer["action"]) is not str or answer["action"] not in public["allowed_actions"]
                    or type(answer["summary"]) is not str or not 1 <= len(answer["summary"]) <= 600
                    or type(answer["reference_ids"]) is not list or not 1 <= len(answer["reference_ids"]) <= 16
                    or any(type(ref) is not str or ref not in public["evidence"] for ref in answer["reference_ids"])
                    or len(set(answer["reference_ids"])) != len(answer["reference_ids"])):
                raise _InvalidStream("answer_shape_or_ids")
            receipt.update(status="succeeded", phase="complete", valid_answer_observed=True,
                model_inference_invoked=mode == "live", action=answer["action"],
                reference_ids=answer["reference_ids"], summary_char_count=len(answer["summary"]))
    except _Deadline:
        receipt.update(status="deadline_exceeded", error_type="Deadline")
    except _InvalidStream as exc:
        receipt.update(status="invalid_stream", validation_error=exc.category, error_type="ProtocolValidation")
    except TimeoutError:
        receipt.update(status="deadline_exceeded", error_type="Timeout")
    except (OSError, http.client.HTTPException):
        receipt.update(status="transport_failed", error_type="Transport")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        receipt.update(status="invalid_configuration", error_type="Configuration")
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
            try:
                closed = clock()-started
                if type(closed) in (int, float) and math.isfinite(closed) and closed >= 0:
                    receipt["timings"]["closed_s"] = closed
            except Exception:
                pass
    return receipt
