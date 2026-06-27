"""
Skills ランタイム
起動時に skills ディレクトリからスキルをロードする
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Any

from src.skills.loader import SkillLoader
from src.skills.base import get_skill_registry
from src.skills.promoted import ensure_promoted_skills_loaded

_loaded = False
_last_report: Dict[str, Any] = {"loaded": False, "count": 0, "skills": []}


async def ensure_skills_loaded(skills_dir: str = "skills") -> Dict[str, Any]:
    """skills を一度だけロードする"""
    global _loaded, _last_report
    if _loaded:
        promoted_report = await ensure_promoted_skills_loaded(refresh=True)
        registry = get_skill_registry()
        meta = [m.name for m in registry.list_skills()]
        _last_report = {
            "loaded": True,
            "count": len(meta),
            "skills": meta,
            "promoted": promoted_report,
        }
        return _last_report

    loader = SkillLoader(skills_dir=skills_dir)
    loaded = await loader.load_skills_from_directory(Path(skills_dir))
    promoted_report = await ensure_promoted_skills_loaded(refresh=True)
    registry = get_skill_registry()
    meta = [m.name for m in registry.list_skills()]
    _loaded = True
    _last_report = {
        "loaded": True,
        "count": len(meta),
        "skills": meta,
        "disk_count": len(loaded),
        "promoted": promoted_report,
    }
    return _last_report


def ensure_skills_loaded_sync(skills_dir: str = "skills") -> Dict[str, Any]:
    """同期文脈から skills ロードを実行する"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure_skills_loaded(skills_dir=skills_dir))
    # 既にイベントループ内なら同期ロードはしない（非同期側で呼ぶ）
    return _last_report


def get_skills_report() -> Dict[str, Any]:
    return _last_report
