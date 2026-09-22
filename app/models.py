"""领域模型与请求/响应 schema。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class EnvStatus(str, enum.Enum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    RECLAIMED = "reclaimed"


# 合法状态流转表：
#   provisioning -> ready / failed / reclaimed
#   failed       -> provisioning(重试) / reclaimed
#   ready        -> reclaimed(超时或显式回收)
#   reclaimed    -> 终态，不可再流转
ALLOWED_TRANSITIONS: dict[EnvStatus, frozenset[EnvStatus]] = {
    EnvStatus.PROVISIONING: frozenset({EnvStatus.READY, EnvStatus.FAILED, EnvStatus.RECLAIMED}),
    EnvStatus.FAILED: frozenset({EnvStatus.PROVISIONING, EnvStatus.RECLAIMED}),
    EnvStatus.READY: frozenset({EnvStatus.RECLAIMED}),
    EnvStatus.RECLAIMED: frozenset(),
}


@dataclass
class PreviewEnvironment:
    """单个预览环境的领域对象。"""

    env_id: str
    project: str
    branch: str
    request_key: str
    status: EnvStatus = EnvStatus.PROVISIONING
    created_at: float = 0.0
    ready_at: float | None = None
    expires_at: float | None = None
    ttl_seconds: float = 0.0
    failure_reason: str | None = None
    failure_count: int = 0
    slot_held: bool = False
    resource_identifiers: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    # 瞬态标记：本次返回是否来自幂等复用（不持久表达领域状态）
    reused: bool = False


class ProvisionRequest(BaseModel):
    project: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    request_key: str = Field(min_length=1)
    ttl_seconds: float | None = None
    idempotency_key: str | None = None


class ProvisionResponse(BaseModel):
    env_id: str
    request_key: str
    project: str
    branch: str
    status: str
    expires_at: float | None = None
    reused: bool = False


class EnvView(BaseModel):
    env_id: str
    request_key: str
    project: str
    branch: str
    status: str
    created_at: float
    ready_at: float | None = None
    expires_at: float | None = None
    failure_reason: str | None = None
    failure_count: int = 0
    history: list[str] = []


class ReclaimResponse(BaseModel):
    env_id: str
    request_key: str
    status: str
    released: bool
