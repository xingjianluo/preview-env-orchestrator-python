"""标识轮换（接管）与到期即时生效测试。

结论必须确定且可复现：
- 同项目分支任何时刻至多一份存活环境；新标识接管同一份环境，不并存第二份；
- 被替换的旧标识进入永久墓碑，一切访问得到一致且可区分的 REQUEST_KEY_SUPERSEDED；
- 过期环境在任意操作入口即被回收，容量立即让出，且不能被接管“救活”；
- 接管与回收/续期/并发申请交织时结论固定，容量与资源计数始终对齐。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvStatus, ProvisionRequest


def _req(key: str, *, project: str = "proj", branch: str = "feat", ttl: float | None = None):
    return ProvisionRequest(project=project, branch=branch, request_key=key, ttl_seconds=ttl)


def _assert_invariants(orchestrator, backend):
    """容量、占用数量、活跃资源与状态计数必须两两对齐，且无占槽空洞。"""
    snap = orchestrator.quota_snapshot()
    envs = orchestrator.store.all_envs()
    live = [e for e in envs if e.status in (EnvStatus.PROVISIONING, EnvStatus.READY)]
    assert snap["used"] == len(live), snap
    assert 0 <= snap["used"] <= snap["quota"]
    assert snap["available"] == snap["quota"] - snap["used"]
    assert backend.active_count() == 3 * sum(1 for e in envs if e.resource_identifiers)
    # 同项目分支至多一份存活环境
    branches = [(e.project, e.branch) for e in live]
    assert len(branches) == len(set(branches))
    # 状态计数与 quota_snapshot 一致
    for status in EnvStatus:
        assert snap.get(f"status_{status.value}", 0) == sum(
            1 for e in envs if e.status is status
        )


# ------------------------------------------------------------- 基本接管语义


def test_new_identifier_takes_over_same_branch_env(orchestrator, backend, clock):
    first = orchestrator.provision(_req("ci-1", ttl=60))
    assert first.status is EnvStatus.READY
    clock.advance(10)

    second = orchestrator.provision(_req("ci-2", ttl=100))

    # 同一份环境被接管：不并存第二份、不重复分配资源、不重复占槽
    assert second is first
    assert second.env_id == "proj-feat"
    assert second.taken_over is True
    assert second.reused is False
    assert second.request_key == "ci-2"
    # 存活时间从接管（新的就绪）时刻重新起算
    assert second.expires_at == clock() + 100
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    # 老标识不再指向这份环境：键索引里只有新标识
    assert orchestrator.store.get_by_key("ci-1") is None
    assert orchestrator.store.get_by_key("ci-2") is second
    assert orchestrator.store.get_superseded("ci-1") == "ci-2"
    assert any("takeover: ci-1 -> ci-2" in line for line in second.history)
    _assert_invariants(orchestrator, backend)


def test_old_identifier_consistently_superseded(orchestrator):
    orchestrator.provision(_req("ci-1"))
    orchestrator.provision(_req("ci-2"))

    # 老标识的每一种访问都得到同一个可区分的结论，不会一会儿有效一会儿失效
    for _ in range(3):
        with pytest.raises(OrchestratorError) as info:
            orchestrator.get("ci-1")
        assert info.value.code == ErrorCode.REQUEST_KEY_SUPERSEDED
        assert info.value.code != ErrorCode.ENV_RECLAIMED
    for op in (
        lambda: orchestrator.renew("ci-1", 100),
        lambda: orchestrator.retry_failed("ci-1"),
        lambda: orchestrator.reclaim("ci-1"),
        lambda: orchestrator.provision(_req("ci-1")),  # 防新旧标识来回翻转
    ):
        with pytest.raises(OrchestratorError) as info:
            op()
        assert info.value.code == ErrorCode.REQUEST_KEY_SUPERSEDED

    # 新标识一切正常
    assert orchestrator.get("ci-2").status is EnvStatus.READY


def test_takeover_succeeds_even_when_quota_full(orchestrator, backend):
    for i in range(3):
        orchestrator.provision(_req(f"k{i}", branch=f"br{i}"))
    assert orchestrator.store.used_slots() == 3

    # 新分支被配额拒绝；同分支换标识接管不需要新槽位，必须成功
    with pytest.raises(OrchestratorError) as info:
        orchestrator.provision(_req("other", branch="elsewhere"))
    assert info.value.code == ErrorCode.QUOTA_EXHAUSTED

    taken = orchestrator.provision(_req("k0-next", branch="br0"))
    assert taken.taken_over is True
    assert taken.env_id == "proj-br0"  # 环境标识不变，不新建
    assert orchestrator.store.used_slots() == 3
    assert backend.active_count() == 9
    _assert_invariants(orchestrator, backend)


def test_takeover_failed_env_then_retry_with_new_key(orchestrator, backend):
    backend.fail_once("proj-flaky")
    failed = orchestrator.provision(_req("ci-1", branch="flaky"))
    assert failed.status is EnvStatus.FAILED
    assert orchestrator.store.used_slots() == 0

    taken = orchestrator.provision(_req("ci-2", branch="flaky"))
    assert taken is failed
    assert taken.taken_over is True
    assert taken.status is EnvStatus.FAILED  # 接管不偷偷重试

    retried = orchestrator.retry_failed("ci-2")
    assert retried.status is EnvStatus.READY
    assert retried.expires_at == retried.ready_at + retried.ttl_seconds
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("ci-1")
    assert info.value.code == ErrorCode.REQUEST_KEY_SUPERSEDED
    _assert_invariants(orchestrator, backend)


def test_takeover_then_full_lifecycle_on_new_key(orchestrator, backend, clock):
    orchestrator.provision(_req("ci-1", ttl=50))
    orchestrator.provision(_req("ci-2", ttl=50))

    renewed = orchestrator.renew("ci-2", 200)
    assert renewed.expires_at == clock() + 200

    _, released = orchestrator.reclaim("ci-2")
    assert released is True
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0

    # 回收后同分支可以再全新申请（live 索引无残留空洞）
    fresh = orchestrator.provision(_req("ci-3"))
    assert fresh.env_id == "proj-feat-2"
    assert fresh.taken_over is False
    _assert_invariants(orchestrator, backend)


# ------------------------------------------------------------- 并发接管


def test_concurrent_takeover_with_same_new_key_happens_once(orchestrator, backend):
    orchestrator.provision(_req("ci-old"))

    def submit(_):
        return orchestrator.provision(_req("ci-new"))

    with ThreadPoolExecutor(max_workers=32) as pool:
        envs = list(pool.map(submit, range(32)))

    assert {e.env_id for e in envs} == {"proj-feat"}
    env = envs[0]
    # 接管只生效一次：历史里恰好一条 takeover 记录，墓碑只有一条
    assert sum("takeover:" in line for line in env.history) == 1
    assert orchestrator.store.get_superseded("ci-old") == "ci-new"
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    _assert_invariants(orchestrator, backend)


def test_concurrent_takeover_with_distinct_keys_is_deterministic(orchestrator, backend):
    orchestrator.provision(_req("ci-0"))
    new_keys = [f"ci-{i}" for i in range(1, 9)]

    def submit(key):
        return orchestrator.provision(_req(key))

    with ThreadPoolExecutor(max_workers=8) as pool:
        envs = list(pool.map(submit, new_keys))

    # 无论到达顺序如何：只有一份环境、一份资源、一个槽位
    assert {e.env_id for e in envs} == {"proj-feat"}
    env = envs[0]
    all_keys = ["ci-0", *new_keys]
    # 恰好一个标识最终指向环境，其余全部进入墓碑，绝不出现两个有效标识
    assert env.request_key in all_keys
    tombstoned = [k for k in all_keys if orchestrator.store.get_superseded(k) is not None]
    assert sorted(tombstoned) == sorted(k for k in all_keys if k != env.request_key)
    assert orchestrator.store.get_by_key(env.request_key) is env
    # 最终标识的重复提交是幂等复用，不再触发接管
    again = orchestrator.provision(_req(env.request_key))
    assert again.reused is True and again.taken_over is False
    assert sum("takeover:" in line for line in env.history) == 8
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    _assert_invariants(orchestrator, backend)


def test_takeover_during_provisioning(orchestrator, backend, clock):
    orig_allocate = backend.allocate
    entered = threading.Event()
    release = threading.Event()

    def delayed(env_id):
        entered.set()
        release.wait(timeout=2)
        return orig_allocate(env_id)

    backend.allocate = delayed  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(orchestrator.provision, _req("ci-1", ttl=60))
            assert entered.wait(timeout=2)  # 环境处于 provisioning

            taken = orchestrator.provision(_req("ci-2", ttl=300))
            assert taken.taken_over is True
            assert taken.status is EnvStatus.PROVISIONING  # 接管创建中的环境

            release.set()
            env = future.result(timeout=2)

        assert env is taken
        assert env.status is EnvStatus.READY
        assert env.request_key == "ci-2"
        # 存活时间从就绪时刻按新标识的 ttl 起算
        assert env.expires_at == env.ready_at + 300
        assert orchestrator.store.used_slots() == 1
        assert backend.active_count() == 3
        with pytest.raises(OrchestratorError) as info:
            orchestrator.get("ci-1")
        assert info.value.code == ErrorCode.REQUEST_KEY_SUPERSEDED
        _assert_invariants(orchestrator, backend)
    finally:
        backend.allocate = orig_allocate  # type: ignore[method-assign]


def test_takeover_vs_reclaim_race_has_fixed_outcome(clock, backend):
    """接管与显式回收并发：要么回收先生效（新标识全新创建），
    要么接管先生效（回收老标识得到 SUPERSEDED）；两种结局都确定且自洽。"""
    from app.orchestrator import PreviewEnvOrchestrator
    from app.service import ResourceBackend

    outcomes = set()
    for _ in range(50):
        orch = PreviewEnvOrchestrator(quota=2, backend=ResourceBackend(), clock=clock)
        orch.provision(_req("ci-1"))
        barrier = threading.Barrier(2)

        def do_reclaim():
            barrier.wait()
            try:
                return ("reclaimed", orch.reclaim("ci-1")[1])
            except OrchestratorError as exc:
                return ("error", exc.code)

        def do_takeover():
            barrier.wait()
            env = orch.provision(_req("ci-2"))
            return ("taken_over", env.taken_over, env.env_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_rec = pool.submit(do_reclaim)
            f_prov = pool.submit(do_takeover)
            r_rec, r_prov = f_rec.result(), f_prov.result()

        if r_rec[0] == "reclaimed":
            assert r_rec[1] is True  # 回收生效
            assert r_prov[1] is False  # 接管未发生：全新创建
            assert r_prov[2] == "proj-feat-2"
            outcomes.add("reclaim-first")
        else:
            assert r_rec == ("error", ErrorCode.REQUEST_KEY_SUPERSEDED)
            assert r_prov[1] is True
            assert r_prov[2] == "proj-feat"
            outcomes.add("takeover-first")

        # 无论哪种结局：恰好一份存活环境，容量与资源对齐
        assert orch.store.used_slots() == 1
        assert orch.backend.active_count() == 3
        _assert_invariants(orch, orch.backend)
    assert outcomes <= {"reclaim-first", "takeover-first"}
    assert outcomes  # 至少出现一种确定结局


# ------------------------------------------------------------- 到期即时生效


def test_expired_env_is_inaccessible_immediately_without_manual_reap(
    orchestrator, backend, clock
):
    env = orchestrator.provision(_req("ci-1", ttl=10))
    clock.advance(10)  # 到期，不调用任何清理入口

    # 查询即按已回收对待，容量与资源立即让出
    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("ci-1")
    assert info.value.code == ErrorCode.ENV_RECLAIMED
    assert env.status is EnvStatus.RECLAIMED
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0

    # 后续续期/重试也得到一致结论
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew("ci-1", 100)
    assert info.value.code == ErrorCode.ENV_RECLAIMED
    _assert_invariants(orchestrator, backend)


def test_expired_env_cannot_be_revived_by_takeover(orchestrator, backend, clock):
    env = orchestrator.provision(_req("ci-1", ttl=10))
    clock.advance(10)  # 已到期

    fresh = orchestrator.provision(_req("ci-2", ttl=10))
    # 不是接管：过期环境先被回收，新标识获得全新环境
    assert fresh.taken_over is False
    assert fresh.env_id == "proj-feat-2"
    assert env.status is EnvStatus.RECLAIMED
    assert orchestrator.store.get_superseded("ci-1") is None  # 无墓碑，是回收而非接管
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    _assert_invariants(orchestrator, backend)


def test_expiry_frees_capacity_for_waiting_request(orchestrator, backend, clock):
    for i in range(3):
        orchestrator.provision(_req(f"k{i}", branch=f"br{i}", ttl=10))
    assert orchestrator.store.used_slots() == 3

    clock.advance(10)  # 全部到期
    # 不手动清理：新申请入口的惰性回收让容量立即可用
    env = orchestrator.provision(_req("new", branch="fresh"))
    assert env.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    _assert_invariants(orchestrator, backend)


# ------------------------------------------------------------- 混合操作不变式


def test_capacity_and_resource_invariants_after_mixed_operations(orchestrator, backend):
    orchestrator.provision(_req("a1", branch="br-a"))
    orchestrator.provision(_req("b1", branch="br-b"))
    orchestrator.provision(_req("c1", branch="br-c"))
    assert orchestrator.store.used_slots() == 3

    orchestrator.provision(_req("a2", branch="br-a"))  # 接管 a1，不占新槽
    orchestrator.reclaim("b1")  # 显式回收
    orchestrator.provision(_req("d1", branch="br-d"))  # 补位
    orchestrator.reclaim("c1")

    backend.fail_once("proj-br-e")
    failed = orchestrator.provision(_req("e1", branch="br-e"))
    assert failed.status is EnvStatus.FAILED  # 失败不占槽

    # 槽位被其他申请占满时，failed 环境重试被拒绝且状态不被半改动
    orchestrator.provision(_req("f1", branch="br-f"))
    assert orchestrator.store.used_slots() == 3
    with pytest.raises(OrchestratorError) as info:
        orchestrator.retry_failed("e1")
    assert info.value.code == ErrorCode.QUOTA_EXHAUSTED
    assert failed.status is EnvStatus.FAILED

    orchestrator.reclaim("f1")
    retried = orchestrator.retry_failed("e1")
    assert retried.status is EnvStatus.READY

    _assert_invariants(orchestrator, backend)
    assert orchestrator.store.used_slots() == 3
    assert backend.active_count() == 9


# ------------------------------------------------------------- HTTP 层


def test_http_takeover_response_and_superseded_error(client):
    resp = client.post("/environments", json={
        "project": "proj", "branch": "feat", "request_key": "ci-1", "ttl_seconds": 60,
    })
    assert resp.status_code == 201
    assert resp.json()["taken_over"] is False

    resp2 = client.post("/environments", json={
        "project": "proj", "branch": "feat", "request_key": "ci-2", "ttl_seconds": 60,
    })
    assert resp2.status_code == 201
    body = resp2.json()
    assert body["taken_over"] is True
    assert body["reused"] is False
    assert body["env_id"] == resp.json()["env_id"]

    old = client.get("/environments/ci-1")
    assert old.status_code == 410
    assert old.json()["error"]["code"] == "REQUEST_KEY_SUPERSEDED"

    new = client.get("/environments/ci-2")
    assert new.status_code == 200
    assert new.json()["request_key"] == "ci-2"
