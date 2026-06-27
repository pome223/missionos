"""
セッション管理
ADK InMemorySessionService を拡張
"""

from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from google.adk.sessions import InMemorySessionService, Session


class SessionManager:
    """セッション管理 (拡張版)"""

    def __init__(self, timeout_seconds: int = 3600):
        self.session_service = InMemorySessionService()
        self.timeout_seconds = timeout_seconds
        self.session_metadata: Dict[str, Dict[str, Any]] = {}

    async def create_session(
        self,
        app_name: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """セッション作成"""
        session = await self.session_service.create_session(
            app_name=app_name,
            user_id=user_id,
        )

        # メタデータ保存
        self.session_metadata[session.id] = {
            "user_id": user_id,
            "app_name": app_name,
            "created_at": datetime.now(),
            "last_active": datetime.now(),
            "metadata": metadata or {},
        }

        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """セッション取得"""
        session = await self.session_service.get_session(session_id)

        if session and session_id in self.session_metadata:
            # 最終アクティブ時刻更新
            self.session_metadata[session_id]["last_active"] = datetime.now()

        return session

    def is_session_active(self, session_id: str) -> bool:
        """セッションがアクティブかチェック"""
        if session_id not in self.session_metadata:
            return False

        metadata = self.session_metadata[session_id]
        last_active = metadata.get("last_active")

        if last_active:
            timeout = timedelta(seconds=self.timeout_seconds)
            return datetime.now() - last_active < timeout

        return False

    async def cleanup_expired_sessions(self):
        """期限切れセッションをクリーンアップ"""
        expired_sessions = []

        for session_id, metadata in self.session_metadata.items():
            if not self.is_session_active(session_id):
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            await self.delete_session(session_id)

        return len(expired_sessions)

    async def delete_session(self, session_id: str):
        """セッション削除"""
        if session_id in self.session_metadata:
            del self.session_metadata[session_id]

    def get_session_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        """セッションメタデータ取得"""
        return self.session_metadata.get(session_id)

    def get_all_sessions(self) -> Dict[str, Dict[str, Any]]:
        """全セッション情報取得"""
        return self.session_metadata.copy()

    def get_active_sessions_count(self) -> int:
        """アクティブセッション数取得"""
        return len([
            sid for sid in self.session_metadata
            if self.is_session_active(sid)
        ])
