"""幂等重复提交、创建失败与重试测试。"""

from __future__ import annotations

import pytest

from preview_orchestrator.errors import (
    EnvironmentFailedError,
    EnvironmentNotFoundError,
    InvalidStateTransitionError,
    QuotaExhaustedError,
)
from preview_orchestrator.models import EnvironmentRequest, EnvironmentStatus
from preview_orchestrator.resources import Provisioner


class TestIdempotency:
    def test_duplicate_submission_reuses_environment(self, make_orchestrator):
        orch = make_orchestrator()
        e1 = orch.request_environment(
            EnvironmentRequest("proj", "feat-1", 60, "k1")
        )
        e2 = orch.request_environment(
            EnvironmentRequest("proj", "feat-1", 999, "k1")
        )
        assert e1 is e2
        assert e1.environment_id == e2.environment_id
        # 重复提交不改写原 TTL
        assert e2.ttl_seconds == 60
        assert orch.pool.used == 1

    def test_repeated_requests_never_create_duplicate_resources(self, make_orchestrator):
        orch = make_orchestrator(quota=5)
        for _ in range(20):
            orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        assert len(orch.provisioner.active_ids) == 1
        assert orch.pool.used == 1

    def test_double_reclaim_is_safe_idempotent(self, make_orchestrator):
        orch = make_orchestrator()
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        r1 = orch.reclaim("k1")
        r2 = orch.reclaim("k1")
        assert r1 is r2
        assert r1.status is EnvironmentStatus.RECLAIMED
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()


class TestFailureAndRetry:
    def test_provision_failure_marks_failed_and_releases_quota(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)

        env = orch.request_environment(
            EnvironmentRequest("proj", "feat-1", 60, "k1")
        )
        assert env.status is EnvironmentStatus.FAILED
        assert env.attempts == 1
        assert "确定性" in (env.failure_reason or "")
        # 失败不占配额、不留底层资源
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()

    def test_failed_environment_remains_accessible_for_retry(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        got = orch.get_environment("k1")
        assert got.status is EnvironmentStatus.FAILED

    def test_retry_reuses_same_environment_id(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        old_id = env.environment_id

        provisioner.always_fail_ids.clear()
        retried = orch.retry_failed("k1")
        assert retried is env
        assert retried.environment_id == old_id  # 不产生重复资源
        assert retried.status is EnvironmentStatus.READY
        assert retried.attempts == 2
        assert orch.pool.used == 1
        assert orch.provisioner.active_ids == {old_id}

    def test_retry_keeps_failed_when_provisioner_still_failing(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        retried = orch.retry_failed("k1")
        assert retried.status is EnvironmentStatus.FAILED
        assert retried.attempts == 2
        assert orch.pool.used == 0

    def test_retry_twice_transient_then_success(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        env_id = env.environment_id

        r1 = orch.retry_failed("k1")
        assert r1.status is EnvironmentStatus.FAILED
        provisioner.always_fail_ids.clear()
        r2 = orch.retry_failed("k1")
        assert r2.status is EnvironmentStatus.READY
        assert orch.provisioner.active_ids == {env_id}
        assert orch.pool.used == 1

    def test_resubmit_same_key_while_failed_reuses_failed_record(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=2, provisioner=provisioner)
        e1 = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        e2 = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        assert e1 is e2
        assert e2.attempts == 1  # 重复提交不会再次尝试创建

    def test_retry_when_quota_exhausted_returns_distinguishable_error(
        self, make_orchestrator
    ):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=2, provisioner=provisioner)
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        # 另一项目占满剩余配额
        orch.request_environment(EnvironmentRequest("proj", "feat-2", 60, "k2"))
        orch.request_environment(EnvironmentRequest("proj", "feat-3", 60, "k3"))
        assert orch.pool.used == 2
        with pytest.raises(QuotaExhaustedError) as exc_info:
            orch.retry_failed("k1")
        assert exc_info.value.code == "quota_exhausted"
        # 重试被配额拒绝后环境仍是 FAILED，可稍后再次重试
        assert orch.get_environment("k1").status is EnvironmentStatus.FAILED
        assert orch.pool.used == 2

    def test_retry_ready_environment_is_noop(self, make_orchestrator):
        orch = make_orchestrator()
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        env = orch.retry_failed("k1")
        assert env.status is EnvironmentStatus.READY
        assert env.attempts == 1

    def test_renew_failed_environment_rejected(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        with pytest.raises(EnvironmentFailedError) as exc_info:
            orch.renew("k1", 60)
        assert exc_info.value.code == "environment_failed"

    def test_retry_reclaimed_environment_raises_not_found(self, make_orchestrator):
        orch = make_orchestrator()
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        orch.reclaim("k1")
        with pytest.raises(EnvironmentNotFoundError):
            orch.retry_failed("k1")

    def test_illegal_transition_rejected(self, make_orchestrator):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=1, provisioner=provisioner)
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        assert env.status is EnvironmentStatus.FAILED
        # FAILED -> READY 不是合法流转
        with pytest.raises(InvalidStateTransitionError):
            env.transition_to(EnvironmentStatus.READY)
