"""Opt-in loopback smoke for the GR00T-compatible policy-client boundary."""

from __future__ import annotations

import argparse
import io
import socket
import threading
from datetime import datetime
from typing import Any

import numpy as np

from src.runtime.groot_policy_client import (
    GrootPolicyBinding,
    GrootPolicyBoundaryError,
    GrootZmqPolicyTransport,
    build_groot_sim_freshness_policy,
    request_groot_action_chunk,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _encode(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        buffer = io.BytesIO()
        np.save(buffer, value, allow_pickle=False)
        return {"__ndarray_class__": True, "as_npy": buffer.getvalue()}
    raise TypeError(type(value).__name__)


def _decode(value: dict[str, Any]) -> Any:
    if value.get("__ndarray_class__") is True:
        return np.load(io.BytesIO(value["as_npy"]), allow_pickle=False)
    return value


class _SmokeClock:
    def __init__(self) -> None:
        self._values = iter(
            (
                datetime.fromisoformat("2026-07-26T00:00:01+00:00"),
                datetime.fromisoformat("2026-07-26T00:00:01.1+00:00"),
            )
        )

    def __call__(self) -> datetime:
        return next(self._values)


def _serve_once(endpoint: str, ready: threading.Event) -> None:
    import msgpack
    import zmq

    context = zmq.Context()
    server = context.socket(zmq.REP)
    server.setsockopt(zmq.LINGER, 0)
    server.bind(endpoint)
    ready.set()
    request = msgpack.unpackb(server.recv(), object_hook=_decode)
    if request.get("endpoint") != "get_action":
        response: dict[str, Any] = {"error": "unsupported endpoint"}
    else:
        response = {
            "action.left_arm": np.zeros((16, 7), dtype=np.float32),
            "action.left_hand": np.zeros((16, 6), dtype=np.float32),
            "action.right_arm": np.zeros((16, 7), dtype=np.float32),
            "action.right_hand": np.zeros((16, 6), dtype=np.float32),
        }
    server.send(msgpack.packb(response, default=_encode))
    server.close()
    context.term()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("loopback network smoke is opt-in; pass --run")

    endpoint = f"tcp://127.0.0.1:{_free_port()}"
    ready = threading.Event()
    server = threading.Thread(
        target=_serve_once,
        args=(endpoint, ready),
        daemon=True,
    )
    server.start()
    if not ready.wait(timeout=2):
        raise RuntimeError("loopback server did not become ready")

    request = {
        "annotation.human.action.task_description": ["place item in bin"],
        "state.left_arm": np.zeros((1, 7), dtype=np.float64),
        "state.left_hand": np.zeros((1, 6), dtype=np.float64),
        "state.right_arm": np.zeros((1, 7), dtype=np.float64),
        "state.right_hand": np.zeros((1, 6), dtype=np.float64),
        "video.ego_view": np.zeros((1, 256, 256, 3), dtype=np.uint8),
    }
    transport = GrootZmqPolicyTransport(
        endpoint=endpoint,
        timeout_ms=1_000,
    )
    proposal = request_groot_action_chunk(
        transport=transport,
        payload=request,
        binding=GrootPolicyBinding(
            instruction_ref="loopback-smoke-instruction",
            preparation_sha256="a" * 64,
            observed_at="2026-07-26T00:00:00Z",
            freshness_deadline="2026-07-26T00:00:03Z",
            freshness_policy=build_groot_sim_freshness_policy(),
        ),
        clock=_SmokeClock(),
    )
    server.join(timeout=2)
    if server.is_alive():
        raise RuntimeError("loopback server did not terminate")
    if proposal.verification_basis != "model_inferred":
        raise RuntimeError("proposal basis changed")
    if proposal.dispatch_request_sent or proposal.physical_execution_invoked:
        raise RuntimeError("proposal crossed the authority boundary")
    invocation_evidence = transport.collect_runtime_invocation_evidence()
    if (
        len(invocation_evidence) != 1
        or invocation_evidence[0].get("request_sha256")
        != proposal.request_sha256
        or invocation_evidence[0].get("response_sha256")
        != proposal.response_sha256
    ):
        raise RuntimeError("runtime invocation evidence was not source-bound")

    timed_out = False
    try:
        GrootZmqPolicyTransport(
            endpoint=f"tcp://127.0.0.1:{_free_port()}",
            timeout_ms=50,
        ).get_action(request)
    except GrootPolicyBoundaryError as exc:
        timed_out = exc.reason == "groot_transport_timeout"
    if not timed_out:
        raise RuntimeError("external transport deadline did not fail closed")
    print(
        "loopback proposal validated: "
        f"basis={proposal.verification_basis} "
        f"actions={sorted(proposal.actions)} "
        "dispatch=false physical_execution=false timeout=fail_closed "
        "runtime_evidence=source_bound"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
