"""FastAPI HTTP 接口，把编排能力暴露为本地服务。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    EnvironmentFailedError,
    EnvironmentNotFoundError,
    IdentifierConflictError,
    InvalidParameterError,
    InvalidStateTransitionError,
    OrchestratorError,
    QuotaExhaustedError,
)
from .models import EnvironmentRequest, EnvironmentStatus, PreviewEnvironment
from .orchestrator import Orchestrator

# 错误 code -> HTTP 状态码
_STATUS_MAP = {
    InvalidParameterError.code: 400,
    IdentifierConflictError.code: 409,
    QuotaExhaustedError.code: 429,
    EnvironmentNotFoundError.code: 404,
    EnvironmentFailedError.code: 409,
    InvalidStateTransitionError.code: 409,
}


class RequestBody(BaseModel):
    project: str = Field(..., description="项目名")
    branch: str = Field(..., description="分支名")
    request_key: str = Field("", description="请求键；缺省为 'project:branch'")
    ttl_seconds: int = Field(300, gt=0, le=86400, description="存活时间（秒）")


class RenewBody(BaseModel):
    ttl_seconds: int = Field(..., gt=0, le=86400)


def _serialize(env: PreviewEnvironment) -> dict:
    return {
        "request_key": env.request_key,
        "environment_id": env.environment_id,
        "project": env.project,
        "branch": env.branch,
        "status": env.status.value,
        "ttl_seconds": env.ttl_seconds,
        "created_at": env.created_at,
        "expires_at": env.expires_at,
        "ready_at": env.ready_at,
        "failure_reason": env.failure_reason,
        "attempts": env.attempts,
    }


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    """构造 FastAPI 应用（可注入编排器，便于测试）。"""
    app = FastAPI(
        title="本地预览环境编排服务",
        version="0.1.0",
    )
    orch = orchestrator or Orchestrator()
    app.state.orchestrator = orch

    @app.exception_handler(OrchestratorError)
    async def _on_orchestrator_error(_request, exc: OrchestratorError):
        http_status = _STATUS_MAP.get(exc.code, 500)
        return JSONResponse(
            status_code=http_status,
            content={
                "error": exc.code,
                "message": exc.message,
                "request_key": exc.request_key,
            },
        )

    @app.post("/environments", status_code=201)
    def request_environment(body: RequestBody):
        env = orch.request_environment(
            EnvironmentRequest(
                project=body.project,
                branch=body.branch,
                ttl_seconds=body.ttl_seconds,
                request_key=body.request_key,
            )
        )
        return _serialize(env)

    @app.get("/environments/{request_key:path}")
    def get_environment(request_key: str):
        return _serialize(orch.get_environment(request_key))

    @app.post("/environments/{request_key:path}/retry")
    def retry_environment(request_key: str):
        return _serialize(orch.retry_failed(request_key))

    @app.post("/environments/{request_key:path}/renew")
    def renew_environment(request_key: str, body: RenewBody):
        return _serialize(orch.renew(request_key, body.ttl_seconds))

    @app.delete("/environments/{request_key:path}")
    def reclaim_environment(request_key: str):
        return _serialize(orch.reclaim(request_key))

    @app.post("/reap-expired")
    def reap_expired():
        keys = orch.reap_expired()
        return {"reclaimed": keys}

    @app.get("/quota")
    def quota_view():
        return {
            "capacity": orch.pool.capacity,
            "used": orch.pool.used,
            "available": orch.pool.available,
            "active_resources": sorted(orch.provisioner.active_ids),
        }

    return app


app = create_app()

