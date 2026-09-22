"""测试与服务共用的结构化日志辅助。

日志统一携带 request_key、env_id、状态流转与拒绝依据，
便于在单测输出中回放每一次申请、续期、回收与配额拒绝。
"""

from __future__ import annotations

import logging

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("preview_orchestrator")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def transition_log(
    logger: logging.Logger,
    request_key: str,
    env_id: str,
    frm: str,
    to: str,
    note: str = "",
) -> None:
    suffix = f" note={note}" if note else ""
    logger.info("key=%s env=%s transition: %s -> %s%s", request_key, env_id, frm, to, suffix)


def action_log(
    logger: logging.Logger,
    action: str,
    request_key: str,
    env_id: str = "-",
    **fields: object,
) -> None:
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    suffix = f" {extras}" if extras else ""
    logger.info("key=%s env=%s action=%s%s", request_key, env_id, action, suffix)


def reject_log(
    logger: logging.Logger,
    request_key: str,
    code: str,
    reason: str,
    *,
    env_id: str = "-",
) -> None:
    logger.warning("key=%s env=%s rejected code=%s reason=%s", request_key, env_id, code, reason)
