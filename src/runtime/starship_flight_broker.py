"""Gateway-owned, one-request provider broker for an approved simulator run.

This thread never controls the simulator. It puts a bounded proposal into a
private mailbox; child-side Rules consume it at a later integration step.
Provider credentials stay in the Gateway, outside both worker processes.
"""
from __future__ import annotations

import json
from pathlib import Path
import time


def serve_flight_request(mailbox: Path, run_id: str, mode: str, process, source_is_current=lambda: True,
                         *, observation_collection=False) -> None:
    # Imported in the host only; the executor has no provider dependency.
    from src.intelligence.starship_flight_supervisor import FlightSupervisor
    from .starship_mission_control import _write

    deadline = time.monotonic() + 300
    def active():
        return process.poll() is None and time.monotonic() < deadline and source_is_current()
    request_path, response_path = mailbox / "request.json", mailbox / "response.json"
    first_request, followup = None, False
    while active():
        if request_path.exists():
            try:
                if request_path.is_symlink() or request_path.stat().st_size > 16384:
                    return
                request = json.loads(request_path.read_text())
                if request.get("request_id") != run_id:
                    return
                if first_request is None:
                    # Bound provider work as well as child-side dispatch. A
                    # delayed/stale followup must not spend another call while
                    # the simulator has already expired the decision window.
                    deadline = min(deadline, time.monotonic()+75.)
                if followup:
                    previous = first_request["observations"][-1]
                    fresh = request.get("observations", [])
                    if (len(fresh) != 2 or any(o.get("time_s", 0) <= previous["time_s"] for o in fresh)
                            or fresh[-1]["time_s"]-previous["time_s"] > 75):
                        return
                # One adapter instance, one assessment, no automatic retry.
                options = {"fixture_route": "need_observation"} if mode == "fixture" and observation_collection and not followup else {}
                response = FlightSupervisor(mode, active=active, **options).assess(request)
                if active():
                    _write(response_path, response)
            except (OSError, ValueError, TypeError, KeyError):
                # Missing reply expires in the running simulator. Never turn
                # provider failure into an execution or success statement.
                return
            if (observation_collection and not followup and response.get("route") == "need_observation"
                    and response.get("action") == "hold"):
                first_request, followup = request, True
                request_path, response_path = mailbox / "request-followup.json", mailbox / "response-followup.json"
            else:
                return
        time.sleep(.05)
