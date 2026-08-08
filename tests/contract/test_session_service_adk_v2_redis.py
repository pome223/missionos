from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from google.adk.events import Event, EventActions
from google.adk.events.event import NodeInfo
from google.adk.sessions import InMemorySessionService

from src.runtime.session_service import (
    RedisSessionService,
    create_session_service,
    describe_session_backend,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.sorted_sets.pop(key, None)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.sorted_sets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key: str, start: int, stop: int) -> list[str]:
        ordered = sorted(
            self.sorted_sets.get(key, {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )
        values = [item[0] for item in ordered]
        return values[start:] if stop == -1 else values[start : stop + 1]

    async def zrem(self, key: str, *values: str) -> None:
        target = self.sorted_sets.setdefault(key, {})
        for value in values:
            target.pop(value, None)


def _settings(redis_url: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        redis_url=redis_url,
        redis_session_namespace="missionos:test:adk-v2",
    )


def test_required_redis_backend_rejects_memory_fallback() -> None:
    with pytest.raises(
        RuntimeError,
        match="redis_session_backend_required:REDIS_URL_not_configured",
    ):
        create_session_service(_settings(None), require_redis=True)

    with pytest.raises(
        RuntimeError,
        match="redis_session_backend_required:REDIS_URL_not_configured",
    ):
        create_session_service(_settings("   "), require_redis=True)

    fallback = create_session_service(_settings(None))
    assert isinstance(fallback, InMemorySessionService)
    assert describe_session_backend(_settings(None)) == {
        "backend": "memory",
        "namespace": None,
    }


def test_redis_roundtrip_preserves_adk_v2_node_metadata_and_output() -> None:
    asyncio.run(_verify_redis_roundtrip())


async def _verify_redis_roundtrip() -> None:
    client = FakeRedis()
    settings = _settings("redis://fixture:6379/0")
    descriptor = describe_session_backend(settings)
    assert descriptor == {
        "backend": "redis",
        "namespace": "missionos:test:adk-v2",
    }
    writer = create_session_service(
        settings,
        client=client,
        require_redis=True,
    )
    assert isinstance(writer, RedisSessionService)
    session = await writer.create_session(
        app_name="missionos_adk_v2",
        user_id="operator_fixture",
        session_id="session_fixture",
        state={"missionos_task_id": "task_fixture"},
    )
    event = Event(
        id="event_fixture",
        invocation_id="invocation_fixture",
        author="missionos_adk_v2_graph",
        node_info=NodeInfo(
            path="workflow.invoke_shadow_safety_critic",
            output_for=["finalize_shadow_proposal"],
            message_as_output=False,
        ),
        output={
            "schema_version": "missionos_adk_v2_persisted_node_output.v1",
            "mission_response_candidate_ref": "candidate:fixture",
            "approval_created": False,
            "dispatch_authority_created": False,
            "executor_invoked": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        custom_metadata={
            "missionos_correlation": {
                "task_id": "task_fixture",
                "proposal_ref": "candidate:fixture",
            }
        },
        actions=EventActions(
            state_delta={
                "missionos_adk_v2_checkpoint_ref": "checkpoint:fixture",
            }
        ),
    )
    await writer.append_event(session=session, event=event)

    reader = create_session_service(
        settings,
        client=client,
        require_redis=True,
    )
    restored = await reader.get_session(
        app_name="missionos_adk_v2",
        user_id="operator_fixture",
        session_id="session_fixture",
    )

    assert restored is not None
    assert restored.state["missionos_task_id"] == "task_fixture"
    assert restored.state["missionos_adk_v2_checkpoint_ref"] == (
        "checkpoint:fixture"
    )
    assert len(restored.events) == 1
    restored_event = restored.events[0]
    assert restored_event.id == "event_fixture"
    assert restored_event.node_info.path == "workflow.invoke_shadow_safety_critic"
    assert restored_event.node_info.output_for == ["finalize_shadow_proposal"]
    assert restored_event.output == event.output
    assert restored_event.custom_metadata == event.custom_metadata
    output = restored_event.output
    assert isinstance(output, dict)
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "executor_invoked",
        "physical_execution_invoked",
        "progress_counted",
    ):
        assert output[field] is False
