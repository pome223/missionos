from __future__ import annotations

from typing import Any

import missionos_cli.cli as missionos_cli


class _FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"ok": True, "summary": {"task_id": "task_timeout_floor"}}


def test_live_sitl_client_uses_long_timeout_floor(monkeypatch: Any) -> None:
    observed_timeouts: list[float] = []

    class CapturingClient:
        def __init__(self, *, timeout: float) -> None:
            observed_timeouts.append(timeout)

        def __enter__(self) -> "CapturingClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(missionos_cli.httpx, "Client", CapturingClient)

    client = missionos_cli.MissionOSGatewayClient(
        base_url="http://127.0.0.1:18791",
        timeout=45.0,
    )

    client.start_sitl(task_id="task_timeout_floor")
    client.execute_sitl(task_id="task_timeout_floor", live_flight_mode=True)

    assert observed_timeouts == [
        missionos_cli.SITL_DISPATCH_TIMEOUT,
        missionos_cli.SITL_DISPATCH_TIMEOUT,
    ]
    assert missionos_cli.SITL_DISPATCH_TIMEOUT >= 3600.0
    assert "timed out" not in missionos_cli.LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE
