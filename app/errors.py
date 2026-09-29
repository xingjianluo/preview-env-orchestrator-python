"""错误类型与错误码定义。"""

from __future__ import annotations


class ErrorCode:
    INVALID_PARAMETER = "INVALID_PARAMETER"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    IDENTIFIER_CONFLICT = "IDENTIFIER_CONFLICT"
    IDENTIFIER_SUPERSEDED = "IDENTIFIER_SUPERSEDED"
    PROVISION_FAILED = "PROVISION_FAILED"
    ENV_NOT_FOUND = "ENV_NOT_FOUND"
    ENV_RECLAIMED = "ENV_RECLAIMED"
    NOT_READY = "NOT_READY"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"


class OrchestratorError(Exception):
    """所有业务错误的基类，携带可区分的错误码与请求键。"""

    def __init__(self, code: str, message: str, *, request_key: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_key = request_key

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"[{self.code}] {self.message}"
