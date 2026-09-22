"""并发测试：同键并发申请、配额争抢、失败重试与回收竞争。

结论必须确定且可复现：
- 同键 N 个并发申请 -> 恰好 1 个环境、1 份资源，其余全部复用；
- 不同键争抢配额 -> 成功数恒等于配额，资源/槽位恒等于 3*配额，拒绝数确定；
- 资源后端在任何时刻都不应观察到重复分配。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvStatus, ProvisionRequest


@dataclass
class Outcome:
    key: str
    env_id: str | None = None
    status: str | None = None
    error: str | None = None
    reused: bool = False


def _run_concurrently(fn, items, workers: int = 16):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def test_concurrent_duplicate_submission_creates_exactly_one(orchestrator, backend):
    req = ProvisionRequest(project="proj", branch="feat", request_key="same-key")

    def submit(_):
        try:
            env = orchestrator.provision(req.model_copy(deep=True))
            return Outcome("same-key", env.env_id, env.status.value, reused=env.reused)
        except OrchestratorError as exc:
            return Outcome("same-key", error=exc.code)

    outcomes = _run_concurrently(submit, range(32), workers=32)

    errors = [o for o in outcomes if o.error]
    assert errors == []
    env_ids = {o.env_id for o in outcomes}
    assert env_ids == {"proj-feat"}
    assert {o.status for o in outcomes} == {"ready"}
    creators = [o for o in outcomes if not o.reused]
    assert len(creators) == 1, f"期望恰好 1 个创建者，实际 {len(creators)}"
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3  # 一份资源，不重复


def test_concurrent_quota_contention_has_deterministic_outcome(orchestrator, backend):
    keys = [f"key-{i:03d}" for i in range(40)]

    def submit(key: str):
        try:
            env = orchestrator.provision(
                ProvisionRequest(project="p", branch=f"b-{key}", request_key=key)
            )
            return Outcome(key, env.env_id, env.status.value, reused=env.reused)
        except OrchestratorError as exc:
            return Outcome(key, error=exc.code)

    outcomes = _run_concurrently(submit, keys, workers=40)
    succeeded = [o for o in outcomes if o.status == "ready"]
    rejected = [o for o in outcomes if o.error == ErrorCode.QUOTA_EXHAUSTED]

    assert len(succeeded) == 3
    assert len(rejected) == 37
    assert len({o.env_id for o in succeeded}) == 3
    assert orchestrator.store.used_slots() == 3
    assert backend.active_count() == 9
    assert all(o.reused is False for o in succeeded)


def test_concurrent_reclaims_are_safe(orchestrator, backend):
    for i in range(3):
        orchestrator.provision(
            ProvisionRequest(project="p", branch=f"b{i}", request_key=f"k{i}")
        )

    def reclaim(key: str):
        try:
            return orchestrator.reclaim(key)[1]
        except OrchestratorError as exc:
            return exc.code

    jobs = [item for k in ("k0", "k1", "k2") for item in (k, k, k)]
    results = _run_concurrently(reclaim, jobs, workers=9)

    true_releases = [r for r in results if r is True]
    false_releases = [r for r in results if r is False]
    assert len(true_releases) == 3
    assert len(false_releases) == 6
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0


def test_concurrent_renew_and_reach_quota_consistency(orchestrator, clock):
    envs = [
        orchestrator.provision(
            ProvisionRequest(project="p", branch=f"b{i}", request_key=f"k{i}", ttl_seconds=10)
        )
        for i in range(3)
    ]

    def renew(i: int):
        try:
            return orchestrator.renew(f"k{i}", 100).expires_at
        except OrchestratorError as exc:
            return exc.code

    clock.advance(10)  # 全部恰好到期
    # 到期瞬间并发续期：要么续期成功，要么被确定性回收，绝不出现自相矛盾的状态
    results = _run_concurrently(renew, range(3), workers=3)
    ready_count = sum(env.status is EnvStatus.READY for env in envs)
    # 每个返回值都与最终状态自洽：就绪 -> 新过期时间；回收 -> ENV_RECLAIMED
    assert sorted(r == clock.t + 100 for r in results).count(True) == ready_count
    assert results.count(ErrorCode.ENV_RECLAIMED) == 3 - ready_count
    for env in envs:
        assert env.status in (EnvStatus.READY, EnvStatus.RECLAIMED)
        if env.status is EnvStatus.READY:
            assert env.expires_at == clock.t + 100
        else:
            assert env.expires_at is None
    # 状态与槽位/资源计数一致
    assert orchestrator.store.used_slots() == sum(
        1 for e in envs if e.status is EnvStatus.READY
    )
    assert orchestrator.backend.active_count() == 3 * orchestrator.store.used_slots()


def test_concurrent_retry_same_failed_env_allocates_once(orchestrator, backend):
    backend.fail_once("p-flaky")
    env = orchestrator.provision(
        ProvisionRequest(project="p", branch="flaky", request_key="fk")
    )
    assert env.status is EnvStatus.FAILED

    def retry(_):
        try:
            e = orchestrator.retry_failed("fk")
            return Outcome("fk", e.env_id, e.status.value, reused=e.reused)
        except OrchestratorError as exc:
            return Outcome("fk", error=exc.code)

    outcomes = _run_concurrently(retry, range(20), workers=20)
    assert all(o.error is None for o in outcomes)
    ready = [o for o in outcomes if o.status == "ready"]
    assert len(ready) == 20
    creators = [o for o in outcomes if not o.reused]
    assert len(creators) == 1
    assert backend.active_count() == 3
    assert orchestrator.store.used_slots() == 1


def test_retry_under_quota_contention_is_stable(orchestrator, backend):
    # 占满 3 槽
    holders = []
    for i in range(3):
        holders.append(
            orchestrator.provision(
                ProvisionRequest(project="p", branch=f"hold{i}", request_key=f"h{i}")
            )
        )
    # 制造一个 failed（先回收 h0 腾槽，创建失败后槽位被归还）
    orchestrator.reclaim("h0")
    backend.fail_once("p-victim")
    victim = orchestrator.provision(
        ProvisionRequest(project="p", branch="victim", request_key="v")
    )
    assert victim.status is EnvStatus.FAILED

    barrier = threading.Barrier(2)

    def fill_slot():
        barrier.wait()
        try:
            orchestrator.provision(
                ProvisionRequest(project="p", branch="filler", request_key="fill")
            )
            return "filled"
        except OrchestratorError as exc:
            return exc.code

    def do_retry():
        barrier.wait()
        try:
            env = orchestrator.retry_failed("v")
            return env.status.value
        except OrchestratorError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda f: f(), [fill_slot, do_retry]))

    # 确定且可解释：恰好一方成功（filler 建好 / victim 重试就绪），
    # 另一方得到 QUOTA_EXHAUSTED
    assert results.count(ErrorCode.QUOTA_EXHAUSTED) == 1, results
    success = [r for r in results if r != ErrorCode.QUOTA_EXHAUSTED]
    assert len(success) == 1 and success[0] in ("ready", "filled"), results
    assert orchestrator.store.used_slots() == 3
    # 无论谁赢，资源数与槽位一致，无泄漏
    assert backend.active_count() == 9


def test_concurrent_provision_and_reclaim_during_allocation(backend, clock):
    """创建进行中并发回收：最终环境要么就绪存在，要么已回收且资源归零。"""
    import time

    from app.orchestrator import PreviewEnvOrchestrator

    o = PreviewEnvOrchestrator(quota=2, backend=backend, clock=clock)

    # 用同步事件确定性地构造“回收先于分配结果提交”的交错：
    # allocate 进入后先等回收完成，再返回资源。
    orig = backend.allocate
    reclaimed = threading.Event()

    def delayed(env_id):
        reclaimed.wait(timeout=2)
        return orig(env_id)

    backend.allocate = delayed  # type: ignore[method-assign]
    try:
        req = ProvisionRequest(project="p", branch="slow", request_key="sk")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_prov = pool.submit(o.provision, req)
            # 等待环境进入 provisioning（后端已在等待回收事件）
            deadline = time.time() + 2
            while time.time() < deadline and o.store.get_by_key("sk") is None:
                time.sleep(0.001)
            f_rec = pool.submit(o.reclaim, "sk")
            reclaimed_env, released = f_rec.result(timeout=2)
            reclaimed.set()  # 放行分配
            env = f_prov.result(timeout=2)

        assert env.status is EnvStatus.RECLAIMED
        assert reclaimed_env is env
        # reclaim 发生在分配完成之前，因此本次返回 released=False
        assert released is False
        # 分配结果到达时发现环境已回收 -> 同步释放：无资源泄漏
        assert backend.active_count() == 0
        assert o.store.used_slots() == 0
    finally:
        backend.allocate = orig  # type: ignore[method-assign]


def test_reclaim_during_failing_allocation_is_consistent(backend, clock):
    """分配失败返回的瞬间环境已被回收：不抛非法流转，状态/资源一致。"""
    import time

    from app.orchestrator import PreviewEnvOrchestrator

    o = PreviewEnvOrchestrator(quota=2, backend=backend, clock=clock)
    orig = backend.allocate
    reclaimed = threading.Event()

    def event_failure(env_id):
        reclaimed.wait(timeout=2)  # 确保回收先于失败结果提交
        if env_id == "p-slowfail":
            from app.service import ResourceError
            raise ResourceError("boom")
        return orig(env_id)

    backend.allocate = event_failure  # type: ignore[method-assign]
    try:
        req = ProvisionRequest(project="p", branch="slowfail", request_key="sk")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_prov = pool.submit(o.provision, req)
            deadline = time.time() + 2
            while time.time() < deadline and o.store.get_by_key("sk") is None:
                time.sleep(0.001)
            f_rec = pool.submit(o.reclaim, "sk")
            rec_env, _ = f_rec.result(timeout=2)
            reclaimed.set()
            env = f_prov.result(timeout=2)  # 不得抛出未处理异常

        assert env is rec_env
        assert env.status is EnvStatus.RECLAIMED
        assert o.store.used_slots() == 0
        assert backend.active_count() == 0
    finally:
        backend.allocate = orig  # type: ignore[method-assign]
