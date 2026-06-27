from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter

from src.gateway.api_schema import AuditQueryResponse

if TYPE_CHECKING:
    from src.gateway.server import GatewayServer


def build_audit_router(server: "GatewayServer") -> APIRouter:
    router = APIRouter(tags=["audit"])

    @router.get("/audit", response_model=AuditQueryResponse)
    async def audit_list_endpoint(
        actor_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tool: Optional[str] = None,
        source: Optional[str] = None,
        result: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: Optional[int] = None,
        limit: int = 20,
    ):
        resolved_page_size = max(1, min(int(page_size or limit or 20), 100))
        return server.audit_logger.query_logs(
            actor_user_id=actor_user_id,
            session_id=session_id,
            tool=tool,
            source=source,
            result=result,
            q=q,
            page=page,
            page_size=resolved_page_size,
        )

    return router
