"""配额测试：耗尽拒绝、释放后可再申请、失败/回收不占槽、错误码可区分。"""

from __future__ import annotations

import pytest

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvStatus, ProvisionRequest


def _req(key: str, **kw):
    return ProvisionRequest(project="p", branch=kw.pop("branch", key), request_key=key, **kw)


def test_quota_exhaustion_is_rejected_with_distinct_code(orchestrator):
    orchestrator.provision(_req("k1"))
    orchestrator.provision(_req("k2"))
    orchestrator.provision(_req("k3"))
    assert orchestrator.store.used_slots() == 3

    with pytest.raises(OrchestratorError) as info:
        orchestrator.provision(_req("k4"))
    err = info.value
    assert err.code == ErrorCode.QUOTA_EXHAUSTED
    assert "3 of 3" in err.message


def test_quota_rejection_creates_nothing(orchestrator, backend):
    for i in range(3):
        orchestrator.provision(_req(f"k{i}"))
    with pytest.raises(OrchestratorError):
        orchestrator.provision(_req("denied"))
    assert orchestrator.store.get_by_key("denied") is None
    assert backend.active_count() == 9
    assert "denied" not in {e.env_id for e in orchestrator.store.all_envs()}


def test_slot_freed_after_reclaim_allows_new_request(orchestrator):
    envs = [orchestrator.provision(_req(f"k{i}")) for i in range(3)]
    orchestrator.reclaim(envs[1].request_key)
    assert orchestrator.store.used_slots() == 2

    replacement = orchestrator.provision(_req("k9", branch="new"))
    assert replacement.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 3


def test_failed_allocation_does_not_consume_quota(orchestrator, backend):
    for i in range(3):
        orchestrator.provision(_req(f"k{i}"))
    orchestrator.reclaim("k2")  # free exactly one slot
    assert orchestrator.store.available_slots() == 1

    backend.fail_once("p-broken")
    failed = orchestrator.provision(_req("kx", branch="broken"))
    assert failed.status is EnvStatus.FAILED
    # 失败后槽位被释放，配额池重新可用
    assert orchestrator.store.available_slots() == 1
    retry = orchestrator.retry_failed("kx")
    assert retry.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 3


def test_retry_denied_when_pool_fills_while_failed(orchestrator):
    orchestrator.provision(_req("a"))
    b = orchestrator.provision(_req("b"))
    # 制造一个 failed 环境（此时占用 2 槽 -> 回收 a 腾出槽 -> 创建失败）
    orchestrator.reclaim("a")
    orchestrator.backend.fail_once("p-f")
    fenv = orchestrator.provision(_req("f", branch="f"))
    assert fenv.status is EnvStatus.FAILED
    # 在重试之前，其他请求重新占满配额
    orchestrator.provision(_req("c", branch="c"))
    orchestrator.provision(_req("d", branch="d"))
    assert orchestrator.store.used_slots() == 3
    with pytest.raises(OrchestratorError) as info:
        orchestrator.retry_failed("f")
    assert info.value.code == ErrorCode.QUOTA_EXHAUSTED
    assert fenv.status is EnvStatus.FAILED  # 状态保持失败，未被半改动
    assert b.status is EnvStatus.READY


def test_quota_snapshot(orchestrator):
    orchestrator.provision(_req("k1"))
    snapshot = orchestrator.quota_snapshot()
    assert snapshot["quota"] == 3
    assert snapshot["used"] == 1
    assert snapshot["available"] == 2
    assert snapshot["status_ready"] == 1


def test_zero_quota_orchestrator(clock, backend):
    from app.orchestrator import PreviewEnvOrchestrator

    o = PreviewEnvOrchestrator(quota=0, backend=backend, clock=clock)
    with pytest.raises(OrchestratorError) as info:
        o.provision(_req("k1"))
    assert info.value.code == ErrorCode.QUOTA_EXHAUSTED
