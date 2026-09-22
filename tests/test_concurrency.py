"""并发竞争测试：配额不超卖、不泄漏、同键不重复、结论确定可复现。

每个场景都多轮重复执行（默认 20 轮），因为关键区由单一可重入锁串行化，
虽然线程进入顺序可能变化，但不变量与成功者数量每轮必须完全一致。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from preview_orchestrator.errors import (
    EnvironmentFailedError,
    EnvironmentNotFoundError,
    IdentifierConflictError,
    OrchestratorError,
    QuotaExhaustedError,
)
from preview_orchestrator.models import EnvironmentRequest, EnvironmentStatus
from preview_orchestrator.resources import Provisioner

ROUNDS = 20


def _run_threads(fn, n: int, *, barrier: threading.Barrier | None = None):
    """启动 n 个线程在同一个屏障点同时释放，返回 (结果, 异常) 列表。"""
    results: list[object] = [None] * n
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            if barrier is not None:
                barrier.wait()
            results[i] = fn(i)
        except BaseException as exc:  # 收集所有错误，便于断言分类
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker, i) for i in range(n)]
        for f in futures:
            f.result()
    return results, errors


class TestConcurrentAcquire:
    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_quota_never_exceeded_under_burst(self, make_orchestrator, round):
        quota, n_threads = 3, 12
        orch = make_orchestrator(quota=quota)
        barrier = threading.Barrier(n_threads)

        def submit(i: int):
            return orch.request_environment(
                EnvironmentRequest("proj", f"feat-{i}", 60, f"k{i}")
            )

        envs, errors = _run_threads(submit, n_threads, barrier=barrier)

        # 确定结论：恰好 quota 个成功，其余全部是 quota_exhausted
        ready = [e for e in envs if e is not None]
        assert len(ready) == quota
        quota_errors = [e for e in errors if isinstance(e, QuotaExhaustedError)]
        assert len(quota_errors) == n_threads - quota
        assert all(e.code == "quota_exhausted" for e in quota_errors)
        # 没有其他类型的错误
        assert all(isinstance(e, QuotaExhaustedError) for e in errors)
        # 物理资源与配额计数严格一致，不超卖、不泄漏、不重复
        assert orch.pool.used == quota
        assert len(orch.provisioner.active_ids) == quota
        ids = [e.environment_id for e in ready]
        assert len(set(ids)) == quota

    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_same_key_concurrent_submission_creates_once(self, make_orchestrator, round):
        n_threads = 16
        orch = make_orchestrator(quota=n_threads)
        barrier = threading.Barrier(n_threads)

        def submit(_i: int):
            return orch.request_environment(
                EnvironmentRequest("proj", "feat-1", 60, "shared-key")
            )

        envs, errors = _run_threads(submit, n_threads, barrier=barrier)
        assert errors == []
        ids = {e.environment_id for e in envs}
        assert ids == {envs[0].environment_id}
        assert orch.pool.used == 1
        assert orch.provisioner.active_ids == {envs[0].environment_id}

    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_same_project_branch_different_keys_one_winner(self, make_orchestrator, round):
        n_threads = 10
        orch = make_orchestrator(quota=n_threads)
        barrier = threading.Barrier(n_threads)

        def submit(i: int):
            return orch.request_environment(
                EnvironmentRequest("proj", "shared-branch", 60, f"k{i}")
            )

        envs, errors = _run_threads(submit, n_threads, barrier=barrier)
        # 恰好一个成功，其余全部标识冲突，没有任何重复资源
        assert len([e for e in envs if e is not None]) == 1
        assert len([e for e in errors if isinstance(e, IdentifierConflictError)]) == n_threads - 1
        assert orch.pool.used == 1
        assert len(orch.provisioner.active_ids) == 1


class TestConcurrentRetry:
    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_concurrent_retry_of_failed_env_succeeds_once(self, make_orchestrator, round):
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        orch = make_orchestrator(quota=3, provisioner=provisioner)
        env = orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        assert env.status is EnvironmentStatus.FAILED
        provisioner.always_fail_ids.clear()

        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def retry(_i: int):
            return orch.retry_failed("k1")

        envs, errors = _run_threads(retry, n_threads, barrier=barrier)
        assert errors == []
        assert all(e.status is EnvironmentStatus.READY for e in envs)
        assert env.attempts == 2  # 并发重试只触发一次底层创建
        assert orch.pool.used == 1
        assert orch.provisioner.active_ids == {env.environment_id}

    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_concurrent_retry_with_competing_quota(self, make_orchestrator, round):
        """失败环境的并发重试与其他申请竞争同一配额池：总量绝不超限。"""
        provisioner = Provisioner()
        provisioner.always_fail_ids.add("env-000001")
        quota = 2
        orch = make_orchestrator(quota=quota, provisioner=provisioner)
        env = orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        assert env.status is EnvironmentStatus.FAILED
        provisioner.always_fail_ids.clear()

        n_retry, n_new = 6, 6
        barrier = threading.Barrier(n_retry + n_new)

        def op(i: int):
            if i < n_retry:
                return orch.retry_failed("k1")
            return orch.request_environment(
                EnvironmentRequest("p", f"new-{i}", 60, f"new-{i}")
            )

        results, errors = _run_threads(op, n_retry + n_new, barrier=barrier)

        # 不变量：存活 READY 总数 + 配额占用 == quota
        assert orch.pool.used == quota
        assert len(orch.provisioner.active_ids) == quota
        # k1 最终要么 READY（占一个槽位），要么仍 FAILED（不占槽位）
        if env.status is EnvironmentStatus.READY:
            assert env.environment_id in orch.provisioner.active_ids
            assert env.attempts == 2
        else:
            assert env.status is EnvironmentStatus.FAILED
        # 所有错误只能是配额耗尽（确定可分类），不能有状态机/重复释放类错误
        assert all(isinstance(e, QuotaExhaustedError) for e in errors)
        # 按唯一环境统计 READY（同键并发重试会返回同一对象），必须恰好占满配额
        ready_ids = {
            r.environment_id
            for r in results
            if r is not None and r.status is EnvironmentStatus.READY
        }
        assert ready_ids == set(orch.provisioner.active_ids)
        assert len(ready_ids) == quota


class TestConcurrentRenewAndReclaim:
    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_concurrent_renew_reclaim_no_inconsistent_state(self, make_orchestrator, round):
        n_each = 8
        orch = make_orchestrator(quota=1)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        barrier = threading.Barrier(n_each * 2)

        def op(i: int):
            if i % 2 == 0:
                return orch.renew("k1", 120)
            return orch.reclaim("k1")

        results, errors = _run_threads(op, n_each * 2, barrier=barrier)

        # 最终确定结论：环境已回收；配额为 0；物理资源为空
        record = orch.store.get_including_reclaimed("k1")
        assert record.status is EnvironmentStatus.RECLAIMED
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()
        # 并发中续期若晚于回收，只能得到 not_found；不能出现非法流转/重复释放
        assert all(
            isinstance(e, EnvironmentNotFoundError) for e in errors
        ), errors
        # 所有回收操作都返回 RECLAIMED，没有一次抛错
        reclaim_results = [r for i, r in enumerate(results) if i % 2 == 1]
        assert all(r.status is EnvironmentStatus.RECLAIMED for r in reclaim_results if r)

    @pytest.mark.parametrize("round", range(ROUNDS))
    def test_concurrent_reclaim_idempotent(self, make_orchestrator, round):
        n_threads = 12
        orch = make_orchestrator(quota=1)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        barrier = threading.Barrier(n_threads)

        def reclaim(_i: int):
            return orch.reclaim("k1")

        results, errors = _run_threads(reclaim, n_threads, barrier=barrier)
        assert errors == []
        assert all(r.status is EnvironmentStatus.RECLAIMED for r in results)
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()


class TestMixedWorkloadStress:
    @pytest.mark.parametrize("round", range(5))
    def test_mixed_request_renew_reclaim_loop_invariants(self, make_orchestrator, round):
        """混合工作负载压力：申请/续期/回收交错，全程配额与资源集合保持一致。"""
        quota = 4
        orch = make_orchestrator(quota=quota)
        n_threads = 12
        stop = threading.Event()
        barrier = threading.Barrier(n_threads)
        counter = {"id": 0}
        counter_lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait()
            while not stop.is_set():
                with counter_lock:
                    counter["id"] += 1
                    uid = counter["id"]
                key = f"k{i}-{uid % 20}"
                req = EnvironmentRequest(
                    f"proj-{i % 3}", f"feat-{uid % 20}", 30, key
                )
                try:
                    env = orch.request_environment(req)
                    if env.status is EnvironmentStatus.READY:
                        try:
                            orch.renew(key, 60)
                        except (EnvironmentNotFoundError, EnvironmentFailedError):
                            pass
                        if uid % 2 == 0:
                            orch.reclaim(key)
                except OrchestratorError:
                    # 标识冲突/配额耗尽都是预期内的确定拒绝
                    pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        threading.Event().wait(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        # 全局不变量
        assert 0 <= orch.pool.used <= quota
        assert orch.pool.used == len(orch.provisioner.active_ids)
        for env in orch.store.all():
            if env.status is EnvironmentStatus.READY:
                assert env.environment_id in orch.provisioner.active_ids
        # 全部回收后资源必须归零（验证无泄漏）
        for env in orch.store.all():
            orch.reclaim(env.request_key)
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()
