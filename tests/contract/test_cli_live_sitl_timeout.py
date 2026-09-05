from __future__ import annotations

from typing import Any, Self

import missionos_cli.cli as missionos_cli


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_live_sitl_client_uses_long_timeout_floor(monkeypatch: Any) -> None:
    observed_timeouts: list[float] = []

    class CapturingClient:
        def __init__(self, *, timeout: float) -> None:
            observed_timeouts.append(timeout)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, _method: str, url: str, **_kwargs: Any) -> _FakeResponse:
            if url.endswith(missionos_cli.SITL_EXECUTION_APPROVAL_ROUTE):
                return _FakeResponse(
                    {
                        "execution_operator_approval": {
                            "approval_id": "approval_timeout_floor"
                        }
                    }
                )
            return _FakeResponse(
                {"ok": True, "summary": {"task_id": "task_timeout_floor"}}
            )

    monkeypatch.setattr(missionos_cli.httpx, "Client", CapturingClient)

    client = missionos_cli.MissionOSGatewayClient(
        base_url="http://127.0.0.1:18791",
        timeout=45.0,
    )

    client.start_sitl(task_id="task_timeout_floor")
    client.execute_sitl(task_id="task_timeout_floor", live_flight_mode=True)

    assert observed_timeouts == [
        missionos_cli.SITL_DISPATCH_TIMEOUT,
        45.0,
        missionos_cli.SITL_DISPATCH_TIMEOUT,
    ]
    assert missionos_cli.SITL_DISPATCH_TIMEOUT >= 3600.0
    assert "timed out" not in missionos_cli.LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE


def test_cli_client_sends_configured_gateway_api_key(monkeypatch: Any) -> None:
    observed_headers: list[dict[str, str]] = []

    class CapturingClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, _method: str, _url: str, **kwargs: Any) -> _FakeResponse:
            observed_headers.append(dict(kwargs.get("headers") or {}))
            return _FakeResponse({"status": "healthy"})

    monkeypatch.setenv("GATEWAY_API_KEY", "contract-test-key")
    monkeypatch.setattr(missionos_cli.httpx, "Client", CapturingClient)

    client = missionos_cli.make_client("http://127.0.0.1:18791", 45.0)
    client.health()

    assert observed_headers == [{"X-API-Key": "contract-test-key"}]


def test_cli_binds_mission_assurance_to_approval_and_execution(
    monkeypatch: Any,
) -> None:
    observed_requests: list[dict[str, Any]] = []

    class CapturingClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
            observed_requests.append(
                {"method": method, "url": url, "json": dict(kwargs.get("json") or {})}
            )
            if url.endswith(missionos_cli.SITL_EXECUTION_APPROVAL_ROUTE):
                return _FakeResponse(
                    {
                        "execution_operator_approval": {
                            "approval_id": "approval_mission_assurance"
                        }
                    }
                )
            return _FakeResponse({"summary": {"task_id": "task_mission_assurance"}})

    monkeypatch.setattr(missionos_cli.httpx, "Client", CapturingClient)
    client = missionos_cli.MissionOSGatewayClient(
        base_url="http://127.0.0.1:18791",
        timeout=45.0,
    )

    client.execute_sitl(
        task_id="task_mission_assurance",
        live_flight_mode=True,
        mission_assurance_on_deviation=True,
    )

    assert observed_requests == [
        {
            "method": "POST",
            "url": (
                "http://127.0.0.1:18791"
                "/px4-gazebo/mission-scenarios/approve-sitl-execution"
            ),
            "json": {
                "task_id": "task_mission_assurance",
                "explicit_execution_approval": True,
                "mission_assurance_on_deviation": True,
                "missionos_client_surface": "missionos_cli",
            },
        },
        {
            "method": "POST",
            "url": (
                "http://127.0.0.1:18791"
                "/px4-gazebo/mission-scenarios/execute-sitl"
            ),
            "json": {
                "task_id": "task_mission_assurance",
                "execution_approval_id": "approval_mission_assurance",
                "live_flight_mode": True,
                "mission_assurance_on_deviation": True,
                "missionos_client_surface": "missionos_cli",
            },
        },
    ]
