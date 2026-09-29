"""FastAPI 适配层：HTTP 接口与错误到状态码的映射。

业务错误 -> HTTP 状态码映射：
- INVALID_PARAMETER        -> 400
- IDENTIFIER_CONFLICT      -> 409
- IDENTIFIER_SUPERSEDED    -> 409
- QUOTA_EXHAUSTED          -> 429
- ENV_RECLAIMED            -> 410
- ENV_NOT_FOUND            -> 404
- NOT_READY                -> 409
- INVALID_STATE_TRANSITION -> 409
- PROVISION_FAILED         -> 200（环境以 failed 状态返回，便于客户端读取后重试）
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvView, ProvisionRequest, ProvisionResponse, ReclaimResponse
from app.orchestrator import PreviewEnvOrchestrator

_HTTP_STATUS = {
    ErrorCode.INVALID_PARAMETER: 400,
    ErrorCode.IDENTIFIER_CONFLICT: 409,
    ErrorCode.IDENTIFIER_SUPERSEDED: 409,
    ErrorCode.QUOTA_EXHAUSTED: 429,
    ErrorCode.ENV_RECLAIMED: 410,
    ErrorCode.ENV_NOT_FOUND: 404,
    ErrorCode.NOT_READY: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
}


class RenewRequest(BaseModel):
    ttl_seconds: float | None = None


def create_app(
    orchestrator: PreviewEnvOrchestrator | None = None,
    *,
    logger: logging.Logger | None = None,
) -> FastAPI:
    orch = orchestrator or PreviewEnvOrchestrator()
    app = FastAPI(title="Preview Environment Orchestrator", version="0.1.0")

    @app.exception_handler(OrchestratorError)
    async def _on_business_error(_request, exc: OrchestratorError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=_HTTP_STATUS.get(exc.code, 422),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_key": exc.request_key,
                }
            },
        )

    def _view(env) -> EnvView:  # type: ignore[no-untyped-def]
        return EnvView(
            env_id=env.env_id,
            request_key=env.request_key,
            project=env.project,
            branch=env.branch,
            status=env.status.value,
            created_at=env.created_at,
            ready_at=env.ready_at,
            expires_at=env.expires_at,
            failure_reason=env.failure_reason,
            failure_count=env.failure_count,
            history=list(env.history),
        )

    @app.post("/environments", response_model=ProvisionResponse, status_code=201)
    def provision(req: ProvisionRequest):  # type: ignore[no-untyped-def]
        env = orch.provision(req)
        return ProvisionResponse(
            env_id=env.env_id,
            request_key=env.request_key,
            project=env.project,
            branch=env.branch,
            status=env.status.value,
            expires_at=env.expires_at,
            reused=env.reused,
        )

    @app.get("/environments/{request_key}", response_model=EnvView)
    def get_env(request_key: str):  # type: ignore[no-untyped-def]
        return _view(orch.get(request_key))

    @app.post("/environments/{request_key}/renew", response_model=EnvView)
    def renew_env(request_key: str, req: RenewRequest):  # type: ignore[no-untyped-def]
        return _view(orch.renew(request_key, req.ttl_seconds))

    @app.post("/environments/{request_key}/retry", response_model=EnvView)
    def retry_env(request_key: str):  # type: ignore[no-untyped-def]
        return _view(orch.retry_failed(request_key))

    @app.delete("/environments/{request_key}", response_model=ReclaimResponse)
    def reclaim_env(request_key: str):  # type: ignore[no-untyped-def]
        env, released = orch.reclaim(request_key)
        return ReclaimResponse(
            env_id=env.env_id,
            request_key=env.request_key,
            status=env.status.value,
            released=released,
        )

    @app.post("/reap")
    def reap():  # type: ignore[no-untyped-def]
        return {"reclaimed": orch.reap_expired()}

    @app.get("/quota")
    def quota():  # type: ignore[no-untyped-def]
        return orch.quota_snapshot()

    return app
