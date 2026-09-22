"""pytest 公共夹具：可控时钟、全新编排器与 HTTP 测试客户端。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import configure_logging
from app.orchestrator import PreviewEnvOrchestrator
from app.service import ResourceBackend
from app.web import create_app


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _show_logs(caplog):
    """让状态流转/拒绝依据在单测日志中可见。"""
    logger = configure_logging(logging.INFO)
    with caplog.at_level(logging.INFO, logger="preview_orchestrator"):
        yield logger


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def backend() -> ResourceBackend:
    return ResourceBackend()


@pytest.fixture
def orchestrator(clock, backend) -> PreviewEnvOrchestrator:
    return PreviewEnvOrchestrator(
        quota=3,
        default_ttl_seconds=60.0,
        backend=backend,
        clock=clock,
    )


@pytest.fixture
def client(orchestrator) -> TestClient:
    return TestClient(create_app(orchestrator))
