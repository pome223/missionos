from __future__ import annotations

from typing import Any

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

        def __enter__(self) -> "CapturingClient":
            return self

        def __exit__(self, *_args: Any) -> None:
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

        def __enter__(self) -> "CapturingClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, _method: str, _url: str, **kwargs: Any) -> _FakeResponse:
            observed_headers.append(dict(kwargs.get("headers") or {}))
            return _FakeResponse({"status": "healthy"})

    monkeypatch.setenv("GATEWAY_API_KEY", "contract-test-key")
    monkeypatch.setattr(missionos_cli.httpx, "Client", CapturingClient)

    client = missionos_cli.make_client("http://127.0.0.1:18791", 45.0)
    client.health()

    assert observed_headers == [{"X-API-Key": "contract-test-key"}]
