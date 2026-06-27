"""
メモリツール - ベクトル検索とSQLite
OpenClaw のメモリシステムを参考
"""

import json
import sqlite3
import time
from typing import List, Dict, Any, Optional
from pathlib import Path
import numpy as np

from google.adk.agents.context import Context as ToolContext
from google import genai
from google.genai import types as genai_types
from src.config.settings import get_settings
from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context

DEFAULT_VECTOR_DIM = 768
VALID_MEMORY_KINDS = {
    "fact",
    "trajectory",
    "approved_improvement",
    "approved_skill",
    "capability_patch",
    "policy_patch",
}


def _safe_json_loads(value: Optional[str], fallback: Any) -> Any:
    """JSON文字列を安全にデコードする"""
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


_embedding_client: Optional[genai.Client] = None


def _get_embedding_client() -> genai.Client:
    """Google GenAI クライアントを取得する"""
    global _embedding_client
    if _embedding_client is None:
        settings = get_settings()
        _embedding_client = genai.Client(
            api_key=settings.google_api_key,
            vertexai=settings.google_genai_use_vertexai,
        )
    return _embedding_client


async def _embed_with_google(
    text: str,
    *,
    task_type: str,
    output_dimensionality: int,
) -> List[float]:
    """Google 埋め込みモデルでテキストをベクトル化する"""
    if output_dimensionality <= 0:
        raise ValueError("Embedding dimension must be positive")

    cleaned = text.strip()
    if not cleaned:
        return [0.0] * output_dimensionality

    settings = get_settings()
    client = _get_embedding_client()
    response = await client.aio.models.embed_content(
        model=settings.memory_embedding_model,
        contents=cleaned,
        config=genai_types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=output_dimensionality,
        ),
    )

    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError("Google embedding response is empty")

    values = response.embeddings[0].values
    return [float(v) for v in values]


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """コサイン類似度を計算する"""
    if not vec_a or not vec_b:
        return 0.0

    a = np.array(vec_a, dtype=np.float32)
    b = np.array(vec_b, dtype=np.float32)

    if a.shape != b.shape:
        return 0.0

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0

    return float(np.dot(a, b) / denom)


class MemoryStore:
    """シンプルなメモリストア (SQLite + ベクトル検索)"""

    def __init__(self, db_path: str = "data/memory.db", vector_dim: int = DEFAULT_VECTOR_DIM):
        self.db_path = Path(db_path)
        self.vector_dim = vector_dim
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """データベースを初期化"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # メモリテーブル
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                embedding TEXT,
                metadata TEXT,
                tags TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        columns = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "kind" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")

        # タグインデックス
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tags ON memories(tags)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_kind ON memories(kind)
        """)

        # 作成日インデックス
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON memories(created_at DESC)
        """)

        conn.commit()
        conn.close()

    def store(
        self,
        content: str,
        kind: str = "fact",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
    ) -> int:
        """メモリを保存"""
        if kind not in VALID_MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind: {kind}")
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        now = time.time()
        tags_str = json.dumps(tags or [])
        metadata_str = json.dumps(metadata or {})
        embedding_str = json.dumps(embedding) if embedding else None

        cursor.execute("""
            INSERT INTO memories (content, kind, embedding, metadata, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (content, kind, embedding_str, metadata_str, tags_str, now, now))

        memory_id = cursor.lastrowid
        conn.commit()
        conn.close()

        return memory_id

    def search(
        self,
        query: Optional[str] = None,
        tags: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        limit: int = 10,
        embedding: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """メモリを検索"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        tag_params: List[Any] = []
        conditions: List[str] = []
        if tags:
            tag_conditions = " OR ".join(["tags LIKE ?" for _ in tags])
            conditions.append(f"({tag_conditions})")
            tag_params = [f'%"{tag}"%' for tag in tags]
        kind_params: List[Any] = []
        if kinds:
            invalid = [kind for kind in kinds if kind not in VALID_MEMORY_KINDS]
            if invalid:
                raise ValueError(f"Unsupported memory kind(s): {', '.join(invalid)}")
            kind_conditions = " OR ".join(["kind = ?" for _ in kinds])
            conditions.append(f"({kind_conditions})")
            kind_params = list(kinds)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        where_params = [*tag_params, *kind_params]

        # クエリ埋め込みがある場合は、候補集合に対してコサイン類似度でランキング
        if embedding:
            candidate_limit = max(limit * 25, 200)
            vector_sql = f"""
                SELECT id, content, kind, embedding, metadata, tags, created_at
                FROM memories
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """
            cursor.execute(vector_sql, [*where_params, candidate_limit])
            rows = cursor.fetchall()
            conn.close()

            scored_results: List[Dict[str, Any]] = []
            for row in rows:
                row_embedding = _safe_json_loads(row[3], None)
                if not row_embedding:
                    continue
                if not isinstance(row_embedding, list):
                    continue

                score = _cosine_similarity(embedding, row_embedding)
                scored_results.append({
                    "id": row[0],
                    "content": row[1],
                    "kind": row[2],
                    "metadata": _safe_json_loads(row[4], {}),
                    "tags": _safe_json_loads(row[5], []),
                    "created_at": row[6],
                    "score": round(score, 6),
                })

            # クエリ指定時は0未満の結果を落としてノイズを減らす
            if query:
                scored_results = [r for r in scored_results if r["score"] > 0]

            scored_results.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
            if scored_results:
                return scored_results[:limit]

            # 既存データに埋め込みが無いケース向けフォールバック
            if query:
                text_sql = f"""
                    SELECT id, content, kind, metadata, tags, created_at
                    FROM memories
                    {where_clause}
                    {"AND" if where_clause else "WHERE"} content LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                cursor = sqlite3.connect(self.db_path).cursor()
                cursor.execute(text_sql, [*where_params, f"%{query}%", limit])
                text_rows = cursor.fetchall()
                cursor.connection.close()
                return [
                    {
                        "id": row[0],
                        "content": row[1],
                        "kind": row[2],
                        "metadata": _safe_json_loads(row[3], {}),
                        "tags": _safe_json_loads(row[4], []),
                        "created_at": row[5],
                        "score": 0.0,
                    }
                    for row in text_rows
                ]

            return []

        # 埋め込み検索なし: 従来のテキスト/タグ検索
        if query:
            text_sql = f"""
                SELECT id, content, kind, metadata, tags, created_at
                FROM memories
                {where_clause}
                {"AND" if where_clause else "WHERE"} content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            cursor.execute(text_sql, [*where_params, f"%{query}%", limit])
        else:
            list_sql = f"""
                SELECT id, content, kind, metadata, tags, created_at
                FROM memories
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """
            cursor.execute(list_sql, [*where_params, limit])

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row[0],
                "content": row[1],
                "kind": row[2],
                "metadata": _safe_json_loads(row[3], {}),
                "tags": _safe_json_loads(row[4], []),
                "created_at": row[5],
            }
            for row in rows
        ]

    def delete(self, memory_id: int) -> bool:
        """メモリを削除"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        deleted = cursor.rowcount > 0

        conn.commit()
        conn.close()

        return deleted

    def get_stats(self) -> Dict[str, Any]:
        """メモリ統計を取得"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM memories")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        with_embedding = cursor.fetchone()[0]

        cursor.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind")
        by_kind = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT created_at FROM memories ORDER BY created_at ASC LIMIT 1")
        oldest = cursor.fetchone()

        cursor.execute("SELECT created_at FROM memories ORDER BY created_at DESC LIMIT 1")
        newest = cursor.fetchone()

        conn.close()

        return {
            "total_memories": total,
            "with_embedding": with_embedding,
            "by_kind": by_kind,
            "oldest": oldest[0] if oldest else None,
            "newest": newest[0] if newest else None,
        }


# グローバルインスタンス
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """メモリストアインスタンスを取得"""
    global _memory_store
    if _memory_store is None:
        settings = get_settings()
        _memory_store = MemoryStore(
            db_path=str(settings.memory_db_path),
            vector_dim=settings.memory_vector_dim,
        )
    return _memory_store


# ADK ツール関数


async def memory_store(
    content: str,
    tags: Optional[str] = None,
    metadata: Optional[str] = None,
    kind: str = "fact",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    情報をメモリに保存する

    Args:
        content: 保存する内容
        tags: タグ (カンマ区切り)
        metadata: メタデータ (JSON文字列)

    Returns:
        保存結果
    """
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    try:
        store = get_memory_store()

        tags_list = [t.strip() for t in tags.split(",")] if tags else None
        metadata_dict = json.loads(metadata) if metadata else None
        if kind not in VALID_MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind: {kind}")
        embedding = await _embed_with_google(
            content,
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=store.vector_dim,
        )

        memory_id = store.store(
            content=content,
            kind=kind,
            tags=tags_list,
            metadata=metadata_dict,
            embedding=embedding,
        )

        payload = {
            "memory_id": memory_id,
            "content": content,
            "kind": kind,
            "tags": tags_list,
            "success": True,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_STORE,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="store",
            resource=str(memory_id),
            result="success",
            metadata={"tags": tags_list or [], "kind": kind},
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "success": False,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_STORE,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="store",
            resource=None,
            result=f"error:{e}",
            metadata={"tags": tags or "", "kind": kind},
        )
        return payload


async def memory_search(
    query: Optional[str] = None,
    tags: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 10,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    メモリから情報を検索する

    Args:
        query: 検索クエリ
        tags: タグフィルタ (カンマ区切り)
        limit: 最大取得件数

    Returns:
        検索結果
    """
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    try:
        store = get_memory_store()

        tags_list = [t.strip() for t in tags.split(",")] if tags else None
        kinds_list = [t.strip() for t in kind.split(",")] if kind else None
        query_embedding = (
            await _embed_with_google(
                query,
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=store.vector_dim,
            )
            if query else None
        )

        results = store.search(
            query=query,
            tags=tags_list,
            kinds=kinds_list,
            limit=limit,
            embedding=query_embedding,
        )

        payload = {
            "results": results,
            "count": len(results),
            "query": query,
            "tags": tags_list,
            "kind": kinds_list,
            "success": True,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_SEARCH,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="search",
            resource=query or "",
            result="success",
            metadata={"tags": tags_list or [], "kind": kinds_list or [], "count": len(results), "limit": limit},
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "success": False,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_SEARCH,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="search",
            resource=query or "",
            result=f"error:{e}",
            metadata={"tags": tags or "", "kind": kind or "", "limit": limit},
        )
        return payload


async def memory_stats(
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    メモリ統計を取得する

    Returns:
        メモリ統計
    """
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    try:
        store = get_memory_store()
        stats = store.get_stats()

        payload = {
            "stats": stats,
            "success": True,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_SEARCH,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="stats",
            resource="memory",
            result="success",
            metadata=stats,
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "success": False,
        }
        audit_logger.log(
            event_type=AuditEventType.MEMORY_SEARCH,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="stats",
            resource="memory",
            result=f"error:{e}",
            metadata={},
        )
        return payload


async def memory_delete(
    memory_id: int,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    指定IDのメモリを削除する

    Args:
        memory_id: 削除するメモリのID

    Returns:
        削除結果
    """
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    try:
        store = get_memory_store()
        deleted = store.delete(memory_id)
        payload = {"memory_id": memory_id, "deleted": deleted, "success": True}
        audit_logger.log(
            event_type=AuditEventType.MEMORY_STORE,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="delete",
            resource=str(memory_id),
            result="success" if deleted else "missing",
            metadata={},
        )
        return payload
    except Exception as e:
        payload = {"error": str(e), "success": False}
        audit_logger.log(
            event_type=AuditEventType.MEMORY_STORE,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            action="delete",
            resource=str(memory_id),
            result=f"error:{e}",
            metadata={},
        )
        return payload
