"""领域模型：环境状态、请求与环境记录。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from .errors import InvalidStateTransitionError


class EnvironmentStatus(str, Enum):
    """预览环境生命周期状态。

    合法流转::

        PENDING -> READY           创建成功
        PENDING -> FAILED          创建失败（可重试）
        FAILED  -> PENDING         重试，重新进入创建中
        READY   -> RECLAIMED       显式回收 / TTL 超时
        PENDING -> RECLAIMED       创建中也允许回收（释放租约）
        FAILED  -> RECLAIMED       失败环境被回收
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    RECLAIMED = "reclaimed"


# 允许的状态流转表，状态机的唯一事实来源。
_ALLOWED_TRANSITIONS: dict[EnvironmentStatus, frozenset[EnvironmentStatus]] = {
    EnvironmentStatus.PENDING: frozenset(
        {EnvironmentStatus.READY, EnvironmentStatus.FAILED, EnvironmentStatus.RECLAIMED}
    ),
    EnvironmentStatus.READY: frozenset({EnvironmentStatus.RECLAIMED}),
    EnvironmentStatus.FAILED: frozenset(
        {EnvironmentStatus.PENDING, EnvironmentStatus.RECLAIMED}
    ),
    EnvironmentStatus.RECLAIMED: frozenset(),
}


@dataclass(frozen=True)
class EnvironmentRequest:
    """一次预览环境申请的输入。"""

    project: str
    branch: str
    ttl_seconds: int = 300
    request_key: str = ""

    def validated(self) -> "EnvironmentRequest":
        """校验并归一化参数，非法时抛 InvalidParameterError。"""
        from .errors import InvalidParameterError

        project = self.project.strip()
        branch = self.branch.strip()
        if not project:
            raise InvalidParameterError("project 不能为空")
        if not branch:
            raise InvalidParameterError("branch 不能为空")
        if not _is_valid_label(project):
            raise InvalidParameterError(
                f"project 非法: {project!r}，只允许字母、数字、'-'、'_'、'.'"
            )
        if not _is_valid_branch(branch):
            raise InvalidParameterError(
                f"branch 非法: {branch!r}，只允许字母、数字、'-'、'_'、'.'、'/'"
            )
        if self.ttl_seconds <= 0:
            raise InvalidParameterError("ttl_seconds 必须为正整数")
        if self.ttl_seconds > 24 * 3600:
            raise InvalidParameterError("ttl_seconds 不能超过 86400（24 小时）")
        key = self.request_key.strip() or default_request_key(project, branch)
        return EnvironmentRequest(project, branch, self.ttl_seconds, key)


_LABEL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_BRANCH_CHARS = _LABEL_CHARS | {"/"}


def _is_valid_label(value: str) -> bool:
    return all(ch in _LABEL_CHARS for ch in value)


def _is_valid_branch(value: str) -> bool:
    return all(ch in _BRANCH_CHARS for ch in value) and not value.startswith("/")


def default_request_key(project: str, branch: str) -> str:
    """未显式提供 request_key 时的默认键。"""
    return f"{project}:{branch}"


@dataclass
class PreviewEnvironment:
    """一个预览环境及其运行时状态。"""

    request_key: str
    project: str
    branch: str
    environment_id: str
    status: EnvironmentStatus = EnvironmentStatus.PENDING
    ttl_seconds: int = 300
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    ready_at: float | None = None
    failure_reason: str | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.expires_at == 0.0:
            self.expires_at = self.created_at + self.ttl_seconds

    def transition_to(self, new_status: EnvironmentStatus) -> None:
        """按状态机执行流转，非法流转抛 InvalidStateTransitionError。"""
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"环境 {self.environment_id} 不允许 {self.status.value} -> "
                f"{new_status.value}",
                request_key=self.request_key,
            )
        self.status = new_status

    def is_expired(self, now: float | None = None) -> bool:
        """是否超过存活时间。RECLAIMED 之外的状态才可能过期。"""
        if self.status is EnvironmentStatus.RECLAIMED:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def renew(self, ttl_seconds: int, now: float | None = None) -> None:
        """续期：从当前时间重新计算过期时刻。"""
        current = now if now is not None else time.time()
        self.ttl_seconds = ttl_seconds
        self.expires_at = current + ttl_seconds
