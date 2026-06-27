"""Session service factory with optional Redis-backed persistence."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional, Protocol

from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
    State,
)
from google.adk.sessions.session import Session

from src.config.settings import Settings, get_settings


class RedisLike(Protocol):
    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: str) -> Any: ...

    async def delete(self, *keys: str) -> Any: ...

    async def zadd(self, key: str, mapping: dict[str, float]) -> Any: ...

    async def zrevrange(self, key: str, start: int, stop: int) -> Any: ...

    async def zrem(self, key: str, *values: str) -> Any: ...


def describe_session_backend(settings: Optional[Settings] = None) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    redis_url = getattr(resolved_settings, "redis_url", None)
    if redis_url:
        return {
            "backend": "redis",
            "namespace": getattr(
                resolved_settings,
                "redis_session_namespace",
                "boiled-claw:sessions",
            ),
        }
    return {"backend": "memory", "namespace": None}


def _decode_redis_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, list):
        return [_decode_redis_value(item) for item in value]
    return value


def _create_redis_client(redis_url: str) -> RedisLike:
    try:
        import redis.asyncio as redis_async
    except ImportError as exc:  # pragma: no cover - exercised via factory tests
        raise RuntimeError(
            "REDIS_URL is set but the redis extra is not installed. "
            "Install with `pip install \"boiled-claw[redis]\"`."
        ) from exc
    return redis_async.from_url(redis_url, decode_responses=True)


class RedisSessionService(BaseSessionService):
    """Persist ADK sessions in Redis while preserving app/user state merging semantics."""

    def __init__(
        self,
        *,
        redis_url: Optional[str] = None,
        client: Optional[RedisLike] = None,
        namespace: str = "boiled-claw:sessions",
    ) -> None:
        if client is None:
            if not redis_url:
                raise ValueError("redis_url is required when client is not provided")
            client = _create_redis_client(redis_url)
        self._client = client
        self._namespace = namespace.rstrip(":")

    def _session_key(self, app_name: str, user_id: str, session_id: str) -> str:
        return f"{self._namespace}:session:{app_name}:{user_id}:{session_id}"

    def _session_index_key(self, app_name: str, user_id: str) -> str:
        return f"{self._namespace}:sessions:{app_name}:{user_id}"

    def _app_state_key(self, app_name: str) -> str:
        return f"{self._namespace}:app_state:{app_name}"

    def _user_state_key(self, app_name: str, user_id: str) -> str:
        return f"{self._namespace}:user_state:{app_name}:{user_id}"

    async def _load_json_map(self, key: str) -> dict[str, Any]:
        raw = await self._client.get(key)
        if raw is None:
            return {}
        return json.loads(str(_decode_redis_value(raw)))

    async def _store_json_map(self, key: str, payload: dict[str, Any]) -> None:
        await self._client.set(key, json.dumps(payload, ensure_ascii=True, sort_keys=True))

    async def _load_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> Session | None:
        raw = await self._client.get(self._session_key(app_name, user_id, session_id))
        if raw is None:
            return None
        return Session.model_validate(json.loads(str(_decode_redis_value(raw))))

    async def _store_session(self, session: Session) -> None:
        payload = json.dumps(session.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
        await self._client.set(
            self._session_key(session.app_name, session.user_id, session.id),
            payload,
        )
        await self._client.zadd(
            self._session_index_key(session.app_name, session.user_id),
            {session.id: float(session.last_update_time)},
        )

    async def _merge_state(self, session: Session) -> Session:
        merged = Session.model_validate(session.model_dump(mode="json"))
        app_state = await self._load_json_map(self._app_state_key(session.app_name))
        for key, value in app_state.items():
            merged.state[State.APP_PREFIX + key] = value
        user_state = await self._load_json_map(self._user_state_key(session.app_name, session.user_id))
        for key, value in user_state.items():
            merged.state[State.USER_PREFIX + key] = value
        return merged

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        resolved_session_id = (
            session_id.strip() if session_id and session_id.strip() else str(uuid.uuid4())
        )
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=resolved_session_id,
            state=state or {},
            last_update_time=time.time(),
        )
        await self._store_session(session)
        return await self._merge_state(session)

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        session = await self._load_session(app_name=app_name, user_id=user_id, session_id=session_id)
        if session is None:
            return None

        if config:
            if config.num_recent_events:
                session.events = session.events[-config.num_recent_events :]
            if config.after_timestamp:
                session.events = [
                    event for event in session.events if event.timestamp >= config.after_timestamp
                ]
        return await self._merge_state(session)

    async def list_sessions(self, *, app_name: str, user_id: str) -> ListSessionsResponse:
        session_ids = [
            str(item)
            for item in _decode_redis_value(
                await self._client.zrevrange(self._session_index_key(app_name, user_id), 0, -1)
            )
        ]
        sessions: list[Session] = []
        for session_id in session_ids:
            session = await self._load_session(app_name=app_name, user_id=user_id, session_id=session_id)
            if session is None:
                continue
            session.events = []
            session.state = {}
            sessions.append(session)
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        await self._client.delete(self._session_key(app_name, user_id, session_id))
        await self._client.zrem(self._session_index_key(app_name, user_id), session_id)

    async def append_event(self, session: Session, event: Event) -> Event:
        await super().append_event(session=session, event=event)
        session.last_update_time = event.timestamp

        stored_session = await self._load_session(
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )
        if stored_session is None:
            stored_session = Session.model_validate(session.model_dump(mode="json"))
        else:
            await super().append_event(session=stored_session, event=event)
            stored_session.last_update_time = event.timestamp

        if event.actions and event.actions.state_delta:
            app_state = await self._load_json_map(self._app_state_key(session.app_name))
            user_state = await self._load_json_map(
                self._user_state_key(session.app_name, session.user_id)
            )
            for key, value in event.actions.state_delta.items():
                if key.startswith(State.APP_PREFIX):
                    app_state[key.removeprefix(State.APP_PREFIX)] = value
                elif key.startswith(State.USER_PREFIX):
                    user_state[key.removeprefix(State.USER_PREFIX)] = value
            await self._store_json_map(self._app_state_key(session.app_name), app_state)
            await self._store_json_map(
                self._user_state_key(session.app_name, session.user_id),
                user_state,
            )
        await self._store_session(stored_session)
        return event


def create_session_service(
    settings: Optional[Settings] = None,
    *,
    client: Optional[RedisLike] = None,
) -> BaseSessionService:
    resolved_settings = settings or get_settings()
    redis_url = getattr(resolved_settings, "redis_url", None)
    if redis_url:
        return RedisSessionService(
            redis_url=redis_url,
            client=client,
            namespace=getattr(
                resolved_settings,
                "redis_session_namespace",
                "boiled-claw:sessions",
            ),
        )
    return InMemorySessionService()
