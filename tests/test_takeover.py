"""标识轮换（接管）与到期实时生效测试。

覆盖：
- 同 project+branch 换 request_key：接管同一份环境而非并存两份；
- 接管后租约从接管时刻重新起算；过期环境不能被接管“救活”；
- 老标识在接管后的查询/续期/回收得到确定且一致的 IDENTIFIER_SUPERSEDED；
- 重复/并发换标识只生效一次，属主唯一；
- 接管与显式回收、创建途中回收并发时结论固定、终态不可再激活；
- 到期即时不可访问、容量即时让出，无需手动 reap；
- 容量上限、槽位、后端资源、状态计数始终一致，无空洞。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ErrorCode, OrchestratorError
from app.models import EnvStatus, ProvisionRequest


def _req(key: str, *, project: str = "proj", branch: str = "main", ttl: float | None = None):
    return ProvisionRequest(project=project, branch=branch, request_key=key, ttl_seconds=ttl)


def _superseded(exc_info) -> bool:
    return exc_info.value.code == ErrorCode.IDENTIFIER_SUPERSEDED


# ------------------------------------------------------- 接管的基本语义

def test_new_key_on_same_branch_takes_over_single_env(orchestrator, backend):
    first = orchestrator.provision(_req("ci-1", ttl=100))
    second = orchestrator.provision(_req("ci-2"))

    # 同一个环境对象、同一批资源、同一个 env_id：绝不并存第二份
    assert second is first
    assert second.env_id == "proj-main"
    assert second.reused is True
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3

    # 属主唯一：只有 ci-2 能在键索引上指向这份环境
    assert orchestrator.store.get_by_key("ci-2") is first
    assert orchestrator.store.get_by_key("ci-1") is None


def test_takeover_restarts_lease_from_takeover_moment(orchestrator, clock):
    first = orchestrator.provision(_req("ci-1", ttl=100))
    assert first.expires_at == 1000.0 + 100

    clock.advance(90)  # 原租约还剩 10
    second = orchestrator.provision(_req("ci-2", ttl=50))
    assert second is first
    # 租约从接管时刻重新起算，而不是在旧到期时间上叠加
    assert second.ready_at == clock()
    assert second.expires_at == clock() + 50
    assert first.status is EnvStatus.READY


# ----------------------------------------------- 老标识：确定且一致的结论

def test_old_key_gets_stable_superseded_result_everywhere(orchestrator):
    orchestrator.provision(_req("ci-1"))
    orchestrator.provision(_req("ci-2"))  # 接管

    # 反复查询结论一致，不会一会儿有效一会儿失效
    for _ in range(3):
        with pytest.raises(OrchestratorError) as info:
            orchestrator.get("ci-1")
        assert _superseded(info)
        assert "ci-2" in info.value.message
        assert "proj-main" in info.value.message

    for op in (
        lambda: orchestrator.renew("ci-1", 10),
        lambda: orchestrator.reclaim("ci-1"),
    ):
        with pytest.raises(OrchestratorError) as info:
            op()
        assert _superseded(info)

    # 新属主一切正常
    assert orchestrator.get("ci-2").status is EnvStatus.READY
    renewed = orchestrator.renew("ci-2", 100)
    assert renewed.expires_at > 0

    # 被接管的老标识再拿来申请，同样得到可区分的拒绝，不会重新指向该环境
    with pytest.raises(OrchestratorError) as info:
        orchestrator.provision(_req("ci-1"))
    assert _superseded(info)


def test_superseded_is_distinct_from_other_rejections(client):
    # HTTP 层：IDENTIFIER_SUPERSEDED -> 409，码体与 QUOTA/RECLAIMED 可区分
    client.post("/environments", json={
        "project": "proj", "branch": "main", "request_key": "ci-1",
    })
    client.post("/environments", json={
        "project": "proj", "branch": "main", "request_key": "ci-2",
    })
    resp = client.get("/environments/ci-1")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "IDENTIFIER_SUPERSEDED"
    assert body["error"]["request_key"] == "ci-1"
    assert "ci-2" in body["error"]["message"]


# ------------------------------------------------- 过期：实时生效、不可救活

def test_expired_env_is_inaccessible_immediately_without_reap(orchestrator, backend, clock):
    env = orchestrator.provision(_req("ci-1", ttl=10))
    clock.advance(10)  # 恰好到期，不调用 reap

    # 查询即视为已回收、不可访问
    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("ci-1")
    assert info.value.code == ErrorCode.ENV_RECLAIMED
    assert env.status is EnvStatus.RECLAIMED
    assert env.expires_at is None

    # 容量与资源立即让出
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0
    assert orchestrator.store.available_slots() == 3


def test_expired_env_cannot_be_taken_over_back_to_life(orchestrator, clock, backend):
    orchestrator.provision(_req("ci-1", ttl=10))
    clock.advance(10)  # 已到期

    # 新标识到来：不是接管一份尸体，而是回收旧的、新建一份（env_id 递增）
    new_env = orchestrator.provision(_req("ci-2", ttl=10))
    assert new_env.status is EnvStatus.READY
    assert new_env.env_id == "proj-main-2"
    assert new_env.request_key == "ci-2"
    assert new_env.expires_at == clock() + 10
    # 全系统仍只有一份存活环境、一份资源
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    live = [e for e in orchestrator.store.all_envs() if e.status is EnvStatus.READY]
    assert live == [new_env]


def test_expired_capacity_is_immediately_available_for_next_request(orchestrator, clock):
    # 占满 3 个不同分支
    envs = []
    for i, br in enumerate(("b0", "b1", "b2")):
        envs.append(orchestrator.provision(_req(f"old-{i}", branch=br, ttl=10)))
    assert orchestrator.store.available_slots() == 0
    clock.advance(10)  # 三份同时到期，不 reap

    # 新申请（不同分支）进入临界区时先清扫：容量当场让出来
    fresh = orchestrator.provision(_req("new-0", branch="b0", ttl=10))
    assert fresh.status is EnvStatus.READY
    assert fresh.env_id == "proj-b0-2"
    assert orchestrator.store.used_slots() == 1
    assert orchestrator.store.available_slots() == 2


def test_renew_on_expired_env_reclaims_and_releases_capacity(orchestrator, clock, backend):
    orchestrator.provision(_req("ci-1", ttl=10))
    clock.advance(11)
    with pytest.raises(OrchestratorError) as info:
        orchestrator.renew("ci-1", 100)
    assert info.value.code == ErrorCode.ENV_RECLAIMED
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0


# ------------------------------------------------------------- 并发：换标识

def test_concurrent_same_new_key_takeover_takes_effect_once(orchestrator, backend):
    orchestrator.provision(_req("ci-1"))

    def submit(_):
        try:
            env = orchestrator.provision(_req("ci-2"))
            return ("ok", env.env_id, env.reused)
        except OrchestratorError as exc:
            return ("err", exc.code, False)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(submit, range(32)))

    # 同一新标识并发：全部成功复用同一份环境，只有一个“非复用”创建语义，
    # 且底层没有任何第二次资源分配
    assert {o[0] for o in outcomes} == {"ok"}
    assert {o[1] for o in outcomes} == {"proj-main"}
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    assert orchestrator.store.get_by_key("ci-1") is None
    assert orchestrator.store.get_by_key("ci-2").env_id == "proj-main"


def test_concurrent_distinct_new_keys_single_surviving_owner(orchestrator, backend):
    orchestrator.provision(_req("ci-1"))

    keys = [f"ci-{i}" for i in range(2, 34)]  # 32 个不同的新标识同时来

    def submit(key):
        try:
            env = orchestrator.provision(_req(key))
            return ("ok", key, env.env_id)
        except OrchestratorError as exc:
            return ("err", key, exc.code)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(submit, keys))

    # 任何时刻只有一份存活环境：所有人拿到的都是同一个 env_id
    env_ids = {o[2] for o in outcomes}
    assert env_ids == {"proj-main"}
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3

    # 属主唯一：恰好有一个键能在键索引上指向该环境
    owners = [
        k for k in keys
        if (env := orchestrator.store.get_by_key(k)) is not None
        and env.env_id == "proj-main" and env.status is EnvStatus.READY
    ]
    assert len(owners) == 1
    owner = owners[0]

    # 其余每个键都留下了一致的“被接管”墓碑：只有属主能查，其余全部 superseded
    for k in keys:
        if k == owner:
            continue
        with pytest.raises(OrchestratorError) as info:
            orchestrator.get(k)
        assert _superseded(info)
    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("ci-1")
    assert _superseded(info)
    assert orchestrator.get(owner).env_id == "proj-main"


def test_takeover_during_provisioning_waits_and_new_owner_gets_ready_env(backend, clock):
    """创建进行中新标识接管：等待就绪后，环境只属于新标识，老标识被接管。"""
    import time

    from app.orchestrator import PreviewEnvOrchestrator

    o = PreviewEnvOrchestrator(quota=2, backend=backend, clock=clock)
    orig = backend.allocate
    entered = threading.Event()
    release = threading.Event()

    def delayed(env_id):
        entered.set()
        release.wait(timeout=2)
        return orig(env_id)

    backend.allocate = delayed  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_old = pool.submit(o.provision, _req("ci-1"))
            entered.wait(timeout=2)  # 老创建已进入后端分配
            f_new = pool.submit(o.provision, _req("ci-2"))  # 接管 provisioning 中的环境

            deadline = time.time() + 2
            while time.time() < deadline and o.store.get_by_key("ci-2") is None:
                time.sleep(0.001)
            # 接管已换绑（环境仍在 provisioning）：两个标识不可能同时指向它
            assert o.store.get_by_key("ci-1") is None
            release.set()

            new_env = f_new.result(timeout=2)
            old_exc = f_old.exception(timeout=2)

        # 接管方拿到唯一的 READY 环境
        assert new_env.status is EnvStatus.READY
        assert new_env.request_key == "ci-2"
        assert new_env.env_id == "proj-main"
        # 老创建者失去归属 -> 可区分的“被接管”，绝不返回它已不拥有的环境
        assert isinstance(old_exc, OrchestratorError)
        assert old_exc.code == ErrorCode.IDENTIFIER_SUPERSEDED

        # 终态一致：唯一存活、唯一属主、一份资源一个槽
        assert o.store.used_slots() == 1
        assert backend.active_count() == 3
        assert o.store.get_by_key("ci-1") is None
        assert o.store.get_by_key("ci-2") is new_env
        with pytest.raises(OrchestratorError) as info:
            o.get("ci-1")
        assert _superseded(info)
    finally:
        backend.allocate = orig  # type: ignore[method-assign]


# ----------------------------------------------- 接管 与 显式回收 并发

def test_reclaim_commits_first_takeover_cannot_revive_it(backend, clock):
    """固定交错：回收先于“接管方返回”生效 => 已回收不可激活，容量让出后可新建。"""
    from app.orchestrator import PreviewEnvOrchestrator

    o = PreviewEnvOrchestrator(quota=2, backend=backend, clock=clock)
    orig = backend.allocate
    entered = threading.Event()
    release = threading.Event()

    def delayed(env_id):
        entered.set()
        release.wait(timeout=2)
        return orig(env_id)

    backend.allocate = delayed  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_takeover = pool.submit(o.provision, _req("ci-2"))
            entered.wait(timeout=2)
            # 环境正处在 ci-2 接管的创建过程中（provisioning）
            assert o.store.get_by_key("ci-2").status is EnvStatus.PROVISIONING
            # 此时（当前属主 ci-2）显式回收先提交
            _, released = o.reclaim("ci-2")
            assert released is False  # 资源尚未分配完成，无资源可放
            release.set()
            env = f_takeover.result(timeout=2)

        # 创建结果到达时发现已回收：保持 reclaimed，绝不被“接管”救活
        assert env.status is EnvStatus.RECLAIMED
        assert env.env_id == "proj-main"
        assert o.store.used_slots() == 0
        assert backend.active_count() == 0
        for key, code in (("ci-2", ErrorCode.ENV_RECLAIMED),):
            with pytest.raises(OrchestratorError) as info:
                o.get(key)
            assert info.value.code == code
        # ci-1 在这个独立编排器里从未申请过
        with pytest.raises(OrchestratorError) as info:
            o.get("ci-1")
        assert info.value.code == ErrorCode.ENV_NOT_FOUND

        # 容量当场让出：再来的标识拿到的是全新一份，而非旧环境复活
        fresh = o.provision(_req("ci-3"))
        assert fresh.env_id == "proj-main-2"
        assert fresh.status is EnvStatus.READY
        assert o.store.used_slots() == 1
        assert backend.active_count() == 3
        assert o.store.get_by_id("proj-main").status is EnvStatus.RECLAIMED
    finally:
        backend.allocate = orig  # type: ignore[method-assign]


def test_takeover_commits_first_then_reclaim_and_no_inflight_reactivation(orchestrator, backend):
    """固定交错：接管先生效 => 老键回收得到 superseded；新属主回收后终态不可激活。"""
    env = orchestrator.provision(_req("ci-1"))
    env_id = env.env_id

    # 用锁顺序把“接管先、回收后”固定下来：回收线程拿到锁时接管已提交
    lock = orchestrator.store.lock()
    lock.acquire()

    def reclaim_old():
        with lock:  # 等接管线程释放锁后才进入，天然排在接管之后
            try:
                return orchestrator.reclaim("ci-1")[1]
            except OrchestratorError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        f_rec = pool.submit(reclaim_old)
        # 同一把 RLock，本线程可重入：在此期间完成接管并先提交
        taken = orchestrator.provision(_req("ci-2"))
        lock.release()  # 放行回收线程
        old_outcome = f_rec.result(timeout=2)

    # 接管赢：接管方持有同一 env_id；老键回收没碰到环境，得到 superseded
    assert taken.env_id == env_id and taken.status is EnvStatus.READY
    assert old_outcome == ErrorCode.IDENTIFIER_SUPERSEDED
    assert env.request_key == "ci-2"

    # 新属主显式回收
    _, released = orchestrator.reclaim("ci-2")
    assert released is True
    assert env.status is EnvStatus.RECLAIMED

    # 终态：所有在途标识都不能把它重新激活或重新占回容量
    for key in ("ci-1", "ci-2"):
        with pytest.raises(OrchestratorError):
            orchestrator.get(key)
    assert orchestrator.store.used_slots() == 0
    assert backend.active_count() == 0
    fresh = orchestrator.provision(_req("ci-3"))
    assert fresh.env_id == "proj-main-2" and fresh.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3


# ------------------------------------------------- 容量 / 资源 / 计数一致性

def _assert_invariants(o, backend, quota: int = 3):
    """对所有强不变式做一次对账：槽位、活跃资源、状态计数、分支单活、空洞检测。"""
    envs = o.store.all_envs()
    live = [e for e in envs if e.status in (EnvStatus.PROVISIONING, EnvStatus.READY)]
    ready = [e for e in envs if e.status is EnvStatus.READY]

    # 1) 槽位数 == 存活（非终态/非失败）环境数，且不超配额
    assert o.store.used_slots() == len(live)
    assert 0 <= o.store.used_slots() <= quota
    assert o.store.available_slots() == quota - o.store.used_slots()

    # 2) 后端资源 == 3 × 持有资源的环境（READY 持有）
    holders = [e for e in envs if e.resource_identifiers]
    assert backend.active_count() == 3 * len(holders)
    assert set(backend.active_resources()) == {r for e in holders for r in e.resource_identifiers}
    # slot_held 标记与实际槽位集合一致（不留下占着却没有环境的空洞）
    held_marked = {e.env_id for e in envs if e.slot_held}
    assert held_marked == set(o.store._held_ids)  # noqa: SLF001
    assert {e.env_id for e in live} == held_marked

    # 3) 每个 (project, branch) 至多一个存活环境，且分支索引指向的正是它
    seen: dict[tuple[str, str], str] = {}
    for e in live:
        assert (e.project, e.branch) not in seen, f"双份存活: {e.env_id}"
        seen[(e.project, e.branch)] = e.env_id
        assert o.store.live_for_branch(e.project, e.branch) is e
    # 已回收环境不得还挂在分支索引上
    for e in envs:
        if e.status is EnvStatus.RECLAIMED:
            assert o.store.live_for_branch(e.project, e.branch) is not e

    # 4) READY 环境都有未来的到期时间（过期已即时收敛，不留存活但已过期的）
    for e in ready:
        assert e.expires_at is not None and e.expires_at > o.now()

    # 5) 键属主唯一：任一 request_key 最多指向一份环境；被换走的老键不在键索引
    for key, e in o.store._envs_by_key.items():  # noqa: SLF001
        assert e.request_key == key
    for key, (env_id, owner) in o.store._superseded.items():  # noqa: SLF001
        assert o.store.get_by_key(key) is None  # 被接管键不再指向环境
        assert owner != key


def test_invariants_hold_through_takeover_chain_and_expiry(orchestrator, backend, clock):
    keys = ["ci-1", "ci-2", "ci-3", "ci-4", "ci-5"]
    for k in keys:
        orchestrator.provision(_req(k, ttl=100))
        _assert_invariants(orchestrator, backend)

    env = orchestrator.get("ci-5")
    assert env.env_id == "proj-main"  # 从头到尾同一份
    for k in keys[:-1]:
        with pytest.raises(OrchestratorError) as info:
            orchestrator.get(k)
        assert _superseded(info)
    _assert_invariants(orchestrator, backend)

    clock.advance(101)  # 到期
    with pytest.raises(OrchestratorError):
        orchestrator.get("ci-5")
    _assert_invariants(orchestrator, backend)

    # 到期回收后同分支换新标识：新建并重新占位，计数重新对得上
    orchestrator.provision(_req("ci-6", ttl=100))
    _assert_invariants(orchestrator, backend)
    snap = orchestrator.quota_snapshot()
    assert snap["used"] == 1 and snap["available"] == 2 and snap["status_ready"] == 1


def test_invariants_hold_under_mixed_concurrent_rotation_and_reclaim(orchestrator, backend):
    # 先铺 2 个不同分支的存活环境
    orchestrator.provision(_req("k-a", branch="a"))
    orchestrator.provision(_req("k-b", branch="b"))

    def job(item):
        kind, key, branch = item
        try:
            if kind == "rotate":
                return orchestrator.provision(_req(key, branch=branch)).env_id
            if kind == "reclaim":
                orchestrator.reclaim(key)
                return "reclaimed"
            return orchestrator.get(key).env_id
        except OrchestratorError as exc:
            return exc.code

    items = []
    for i in range(20):
        items.append(("rotate", f"ra-{i}", "a"))   # 对分支 a 反复换标识
        items.append(("rotate", f"rb-{i}", "b"))   # 对分支 b 反复换标识
    items += [("reclaim", "ra-5", "a"), ("reclaim", "rb-7", "b"),
              ("get", "ra-19", "a"), ("get", "rb-19", "b")]

    import random
    random.Random(42).shuffle(items)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(job, items))

    _assert_invariants(orchestrator, backend)


# ---------------------------------------------------- FAILED 环境也受单活约束

def test_failed_env_on_branch_is_taken_over_not_duplicated(orchestrator, backend):
    backend.fail_once("proj-bug")
    first = orchestrator.provision(_req("ci-1", branch="bug"))
    assert first.status is EnvStatus.FAILED
    assert orchestrator.store.used_slots() == 0  # 失败不占槽

    # 同分支新标识：接管这份 failed 环境（仍只有一条记录），而不是并存
    second = orchestrator.provision(_req("ci-2", branch="bug"))
    assert second is first
    assert second.status is EnvStatus.FAILED
    assert second.request_key == "ci-2"
    with pytest.raises(OrchestratorError) as info:
        orchestrator.get("ci-1")
    assert _superseded(info)

    # 新属主重试成功
    ok = orchestrator.retry_failed("ci-2")
    assert ok.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 1
    assert backend.active_count() == 3
    _assert_invariants(orchestrator, backend)


def test_takeover_does_not_consume_quota_when_full(orchestrator, backend):
    # 占满 3 槽（3 个不同分支）
    orchestrator.provision(_req("h0", branch="h0"))
    orchestrator.provision(_req("h1", branch="h1"))
    orchestrator.provision(_req("old", branch="h2"))
    assert orchestrator.store.available_slots() == 0

    # 同分支换标识接管：复用既有环境与槽位，不新增配额消耗
    taken = orchestrator.provision(_req("new", branch="h2"))
    assert taken.status is EnvStatus.READY
    assert orchestrator.store.used_slots() == 3
    assert backend.active_count() == 9
    _assert_invariants(orchestrator, backend)
