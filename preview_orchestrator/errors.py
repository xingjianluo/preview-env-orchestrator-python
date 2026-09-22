"""预览环境编排的错误分类。

所有业务错误都带稳定的 ``code``，调用方可以据此区分拒绝原因，
而无需解析人类可读的消息文本。
"""

from __future__ import annotations


class OrchestratorError(Exception):
    """所有编排错误的基类。"""

    code = "orchestrator_error"

    def __init__(self, message: str, *, request_key: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_key = request_key

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        prefix = f"[{self.code}]"
        if self.request_key is not None:
            prefix += f" key={self.request_key}"
        return f"{prefix} {self.message}"


class InvalidParameterError(OrchestratorError):
    """请求参数非法（项目/分支名为空、TTL 超出允许范围等）。"""

    code = "invalid_parameter"


class IdentifierConflictError(OrchestratorError):
    """环境标识冲突：同一 (project, branch) 已被其他请求键占用。"""

    code = "identifier_conflict"


class QuotaExhaustedError(OrchestratorError):
    """配额池已耗尽，无法再创建新环境。"""

    code = "quota_exhausted"


class EnvironmentNotFoundError(OrchestratorError):
    """访问或操作了不存在 / 已回收的环境。"""

    code = "environment_not_found"


class EnvironmentFailedError(OrchestratorError):
    """环境处于 FAILED 状态，操作不被允许。"""

    code = "environment_failed"


class InvalidStateTransitionError(OrchestratorError):
    """请求的生命周期状态流转不合法。"""

    code = "invalid_state_transition"
