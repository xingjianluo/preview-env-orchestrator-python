"""测试公共配置：让单测日志打印请求键、状态流转与拒绝依据。"""

from __future__ import annotations

import logging
import os

import pytest

from preview_orchestrator.orchestrator import Orchestrator
from preview_orchestrator.resources import Provisioner, ResourcePool
from preview_orchestrator.store import EnvironmentStore


def _configure_logging() -> None:
    if os.environ.get("TEST_LOG_CONFIGURED"):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    logger = logging.getLogger("preview_orchestrator")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [handler]
    logger.propagate = False
    os.environ["TEST_LOG_CONFIGURED"] = "1"


_configure_logging()


@pytest.fixture
def make_orchestrator():
    """工厂夹具：每个用例获得全新、可注入时钟的编排器。"""

    created: list[Orchestrator] = []

    def _make(quota: int = 3, default_ttl_seconds: int = 300, clock=None,
              provisioner: Provisioner | None = None):
        store = EnvironmentStore()
        pool = ResourcePool(quota)
        orch = Orchestrator(
            quota=quota,
            default_ttl_seconds=default_ttl_seconds,
            resource_pool=pool,
            provisioner=provisioner or Provisioner(),
            store=store,
            clock=clock,
        )
        created.append(orch)
        return orch

    yield _make

    for orch in created:
        orch.stop_reaper()
