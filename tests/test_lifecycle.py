"""生命周期测试：申请中->就绪->失败->已回收、幂等复用、失败重试、TTL 回收。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvStatus, ProvisionRequest


def _req(key: str, *, project: str = "proj", branch: str = "main", ttl: float | None = None):
    return ProvisionRequest(project=project, branch=branch, request_key=key, ttl_seconds=ttl)


def test_provision_then_ready_and_logs_transition(orchestrator, caplog):
    env = orchestrator.provision(_req("key-1"))
    assert env.status is EnvStatus.READY
    assert env.env_id == "proj-main"
    assert env.reused is False
    assert env.expires_at == env.ready_at + 60.0
    assert any("provisioning -> ready" in r.message and "key-1" in r.message
               for r in caplog.records)


def test_duplicate_submission_reuses_env(orchestrator, backend):
    first = orchestrator.provision(_req("key-1"))
    second = orchestrator.provision(_req("key-1"))
    assert first is second
    assert second.reused is True
    assert backend.active_count() == 3  # 只创建过一份资源


def test_same_key_with_different_identity_is_conflict(orchestrator):
    orchestrator.provision(_req("key-1", branch="a"))
    with pytest.raises(OrchestratorError) as info:
        orchestrator.provision(_req("key-1", branch="b"))
    assert info.value.code == ErrorCode.IDENTIFIER_CONFLICT


def test_invalid_parameters_are_rejected(orchestrator):
    # 空白但非空的字符串穿过 pydantic，由编排层以 INVALID_PARAMETER 拒绝
    for kwargs in (
        {"project": " ", "branch": "b", "request_key": "k"},
        {"project": "a", "branch": "  ", "request_key": "k"},
        {"project": "a", "branch": "b", "request_key": "\t"},
    ):
        with pytest.raises(OrchestratorError) as info:
            orchestrator.provision(ProvisionRequest(**kwargs))
        assert info.value.code == ErrorCode.INVALID_PARAMETER

    env = orchestrator.provision(_req("key-1"))
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew(env.request_key, 0)
    assert info.value.code == ErrorCode.INVALID_PARAMETER
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew(env.request_key, 10 ** 9)
    assert info.value.code == ErrorCode.INVALID_PARAMETER


def test_failed_provision_then_retry_succeeds_without_duplicate_resources(
    orchestrator, backend, caplog
):
    backend.fail_once("proj-buggy")
    env = orchestrator.provision(_req("key-fail", branch="buggy"))

    assert env.status is EnvStatus.FAILED
    assert env.failure_reason == "injected"
    assert env.failure_count == 1
    assert env.resource_identifiers == []
    assert orchestrator.store.used_slots() == 0  # 失败后槽位立即释放
    assert backend.active_count() == 0
    assert any("PROVISION_FAILED" in r.message and "key-fail" in r.message
               for r in caplog.records if r.levelno == logging.WARNING)

    # 对 failed 环境重复提交仍然是复用，不会偷偷重试
    assert orchestrator.provision(_req("key-fail", branch="buggy")) is env

    retried = orchestrator.retry_failed("key-fail")
    assert retried.status is EnvStatus.READY
    assert retried.failure_count == 1
    assert retried.resource_identifiers == [
        "proj-buggy/database",
        "proj-buggy/service",
        "proj-buggy/route",
    ]
    assert backend.active_count() == 3  # 没有重复资源


def test_repeated_failure_then_retry(orchestrator, backend):
    backend.fail_once("proj-flaky")
    env = orchestrator.provision(_req("k", branch="flaky"))
    assert env.status is EnvStatus.FAILED

    backend.fail_once("proj-flaky", reason="second")
    again = orchestrator.retry_failed("k")
    assert again.status is EnvStatus.FAILED
    assert again.failure_count == 2
    assert again.failure_reason == "second"
    assert orchestrator.store.used_slots() == 0

    ok = orchestrator.retry_failed("k")
    assert ok.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 1


def test_retry_rules(orchestrator):
    env = orchestrator.provision(_req("key-ready"))
    # 对已就绪环境重试 = 幂等返回，不改动状态也不重复分配
    same = orchestrator.retry_failed("key-ready")
    assert same is env and same.status is EnvStatus.READY

    with pytest.raises(OrchestratorError) as info:
        orchestrator.retry_failed("missing")
    assert info.value.code == ErrorCode.ENV_NOT_FOUND

    orchestrator.reclaim(env.request_key)
    with pytest.raises(OrchestratorError) as info:
        orchestrator.retry_failed(env.request_key)
    assert info.value.code == ErrorCode.ENV_RECLAIMED


def test_explicit_reclaim_releases_resources_and_is_idempotent(orchestrator, backend):
    env = orchestrator.provision(_req("key-1"))
    assert orchestrator.store.used_slots() == 1

    _, released = orchestrator.reclaim("key-1")
    assert released is True
    assert env.status is EnvStatus.RECLAIMED
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0

    _, released_again = orchestrator.reclaim("key-1")
    assert released_again is False  # 重复回收安全且不再释放

    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("key-1")
    assert info.value.code == ErrorCode.ENV_RECLAIMED


def test_reclaimed_env_can_be_recreated_with_new_identifier(orchestrator):
    first = orchestrator.provision(_req("key-1", branch="feat"))
    first_id = first.env_id
    orchestrator.reclaim("key-1")
    second = orchestrator.provision(_req("key-1", branch="feat"))
    assert second.env_id != first_id
    assert second.env_id == "proj-feat-2"
    assert orchestrator.store.get_by_id(first_id).status is EnvStatus.RECLAIMED
    assert orchestrator.store.get_by_key("key-1") is second


def test_ttl_expiry_reaper(orchestrator, backend, clock):
    a = orchestrator.provision(_req("a", branch="ta", ttl=10))
    b = orchestrator.provision(_req("b", branch="tb", ttl=30))

    clock.advance(10)
    gone = orchestrator.reap_expired()
    assert gone == [a.env_id]
    assert a.status is EnvStatus.RECLAIMED
    assert b.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3

    # 再次扫描没有副作用
    assert orchestrator.reap_expired() == []

    clock.advance(21)
    assert orchestrator.reap_expired() == [b.env_id]
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0


def test_renew_extends_lease_and_expired_renew_reclaims(orchestrator, clock):
    orchestrator.provision(_req("k", ttl=10))
    clock.advance(8)
    renewed = orchestrator.renew("k", 100)
    assert renewed.expires_at == clock() + 100

    clock.advance(9)
    assert orchestrator.reap_expired() == []  # 续期生效，未过期

    clock.advance(100)
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew("k")
    assert info.value.code == ErrorCode.ENV_RECLAIMED  # 续期时已过期 -> 被回收


def test_renew_requires_ready(orchestrator, backend):
    backend.fail_once("proj-x")
    orchestrator.provision(_req("k", branch="x"))
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew("k", 10)
    assert info.value.code == ErrorCode.NOT_READY


def test_history_records_every_transition(orchestrator, backend):
    backend.fail_once("proj-h")
    env = orchestrator.provision(_req("k", branch="h"))
    orchestrator.retry_failed("k")
    orchestrator.reclaim("k")
    kinds = [line.split(" ", 1)[1] for line in env.history]
    assert any("provisioning->failed" in line for line in kinds)
    assert any("failed->provisioning" in line for line in kinds)
    assert any("provisioning->ready" in line for line in kinds)
    assert any("ready->reclaimed" in line for line in kinds)
