"""配额耗尽、拒绝原因可区分与回收释放配额测试。"""

from __future__ import annotations

import pytest

from preview_orchestrator.errors import (
    IdentifierConflictError,
    InvalidParameterError,
    QuotaExhaustedError,
)
from preview_orchestrator.models import EnvironmentRequest


def _req(key: str, project: str = "proj", branch: str | None = None, ttl: int = 60):
    return EnvironmentRequest(project, branch or f"branch-{key}", ttl, key)


class TestQuota:
    def test_quota_exhaustion_rejected_with_distinct_code(self, make_orchestrator):
        orch = make_orchestrator(quota=2)
        orch.request_environment(_req("k1"))
        orch.request_environment(_req("k2"))
        with pytest.raises(QuotaExhaustedError) as exc_info:
            orch.request_environment(_req("k3"))
        assert exc_info.value.code == "quota_exhausted"
        assert "2/2" in str(exc_info.value)

    def test_three_rejection_causes_are_distinguishable(self, make_orchestrator):
        orch = make_orchestrator(quota=1)
        # 1) 参数非法
        with pytest.raises(InvalidParameterError) as e1:
            orch.request_environment(EnvironmentRequest("", "b", 60, "bad"))
        # 2) 标识冲突
        orch.request_environment(_req("k1", branch="shared"))
        with pytest.raises(IdentifierConflictError) as e2:
            orch.request_environment(
                EnvironmentRequest("proj", "shared", 60, "k2")
            )
        # 3) 配额耗尽（不同项目分支）
        with pytest.raises(QuotaExhaustedError) as e3:
            orch.request_environment(_req("k3", project="other", branch="b3"))
        codes = {e1.value.code, e2.value.code, e3.value.code}
        assert codes == {
            "invalid_parameter",
            "identifier_conflict",
            "quota_exhausted",
        }

    def test_reclaim_frees_quota_for_new_request(self, make_orchestrator):
        orch = make_orchestrator(quota=2)
        orch.request_environment(_req("k1"))
        orch.request_environment(_req("k2"))
        orch.reclaim("k1")
        assert orch.pool.used == 1
        env = orch.request_environment(_req("k9"))
        assert env.status.value == "ready"
        assert orch.pool.used == 2

    def test_failed_creation_does_not_consume_quota(self, make_orchestrator):
        from preview_orchestrator.resources import Provisioner

        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        orch.request_environment(_req("k1"))
        assert orch.pool.used == 0
        provisioner.always_fail_ids.clear()
        env = orch.request_environment(
            EnvironmentRequest("proj", "other", 60, "k2")
        )
        assert env.status.value == "ready"
        assert orch.pool.used == 1

    def test_quota_refused_leaves_no_record(self, make_orchestrator):
        """被配额拒绝的请求不落任何记录，之后可用同键重试成功。"""
        orch = make_orchestrator(quota=1)
        orch.request_environment(_req("k1"))
        with pytest.raises(QuotaExhaustedError):
            orch.request_environment(_req("k2"))
        orch.reclaim("k1")
        env = orch.request_environment(_req("k2"))
        assert env.status.value == "ready"
        assert env.attempts == 1

    def test_quota_view_consistent(self, make_orchestrator):
        orch = make_orchestrator(quota=3)
        orch.request_environment(_req("k1"))
        orch.request_environment(_req("k2"))
        assert orch.pool.capacity == 3
        assert orch.pool.used == 2
        assert orch.pool.available == 1
