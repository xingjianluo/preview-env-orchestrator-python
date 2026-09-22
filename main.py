"""服务入口：uvicorn main:app --reload"""

from preview_orchestrator.api import app

__all__ = ["app"]
