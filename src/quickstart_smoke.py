"""No-model quickstart smoke for first-run validation."""

from __future__ import annotations

import argparse
import json
from typing import Any

from src.tools.tasks import append_task_event_record, create_task_record, update_task_record


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_USER_ID = "quickstart"
DEFAULT_SESSION_ID = "quickstart-local"


def _trim_gateway_url(gateway_url: str | None) -> str:
    return (gateway_url or DEFAULT_GATEWAY_URL).rstrip("/")


def run_quickstart_smoke(
    *,
    gateway_url: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> dict[str, Any]:
    """Create and complete a deterministic task visible through Gateway APIs."""

    base_url = _trim_gateway_url(gateway_url)
    task = create_task_record(
        kind="quickstart_smoke",
        title="Quickstart smoke task",
        status="running",
        owner_session_id=session_id,
        owner_user_id=user_id,
        artifacts={
            "quickstart": {
                "requires_model": False,
                "requires_google_api_key": False,
                "requires_browser_extension": False,
                "requires_host_bridge": False,
                "requires_desktop_bridge": False,
            }
        },
        metadata={
            "source": "quickstart",
            "gateway_url": base_url,
            "purpose": "first-run task store and timeline validation",
        },
    )
    task_id = task["task_id"]
    append_task_event_record(
        task_id,
        event_type="quickstart_smoke_started",
        payload={
            "checks": [
                "task_store_write",
                "timeline_append",
                "http_task_fetch",
                "http_timeline_fetch",
            ],
            "requires_model": False,
        },
        status="running",
    )
    completed = update_task_record(
        task_id,
        status="completed",
        artifacts={
            "result": {
                "success": True,
                "summary": "Quickstart smoke completed without model or browser bridge.",
            }
        },
    )
    append_task_event_record(
        task_id,
        event_type="quickstart_smoke_completed",
        payload={
            "success": True,
            "task_url": f"{base_url}/tasks/{task_id}",
            "timeline_url": f"{base_url}/tasks/{task_id}/timeline?limit=20",
        },
        status="completed",
    )

    return {
        "success": True,
        "task_id": task_id,
        "task": completed,
        "gateway_url": base_url,
        "task_url": f"{base_url}/tasks/{task_id}",
        "timeline_url": f"{base_url}/tasks/{task_id}/timeline?limit=20",
        "requires": {
            "google_api_key": False,
            "chrome_extension": False,
            "host_bridge": False,
            "desktop_bridge": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic quickstart task without the retired legacy CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--session-id", default=DEFAULT_SESSION_ID)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    result = run_quickstart_smoke(
        gateway_url=args.gateway_url,
        user_id=args.user_id,
        session_id=args.session_id,
    )
    if args.json_output:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print("Quickstart smoke completed.")
        print(f"Task: {result['task_url']}")
        print(f"Timeline: {result['timeline_url']}")
        print("No model, browser extension, Host Bridge, or Desktop Bridge required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
