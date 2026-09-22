"""StateStore 单元测试：槽位不超发、幂等占用/释放、索引一致性。"""

from __future__ import annotations

import threading

import pytest

from app.models import EnvStatus, PreviewEnvironment
from app.store import StateStore


def _env(env_id: str, key: str) -> PreviewEnvironment:
    return PreviewEnvironment(
        env_id=env_id, project="p", branch="b", request_key=key,
        status=EnvStatus.READY,
    )


def test_quota_cannot_be_over_subscribed():
    store = StateStore(quota=2)
    assert store.acquire_slot("a")
    assert store.acquire_slot("b")
    assert store.used_slots() == 2
    assert not store.acquire_slot("c")
    assert store.used_slots() == 2


def test_slot_acquire_and_release_are_idempotent():
    store = StateStore(quota=1)
    assert store.acquire_slot("a")
    assert store.acquire_slot("a")  # 重复占用不重复计数
    assert store.used_slots() == 1
    store.release_slot("a")
    store.release_slot("a")  # 重复释放安全
    assert store.used_slots() == 0


def test_put_and_indexes():
    store = StateStore()
    env = _env("e1", "k1")
    store.put(env)
    assert store.get_by_key("k1") is env
    assert store.get_by_id("e1") is env
    assert store.all_envs() == [env]


def test_key_index_tracks_latest_env_but_id_history_kept():
    store = StateStore()
    old = _env("e1", "k1")
    old.status = EnvStatus.RECLAIMED
    store.put(old)
    new = _env("e2", "k1")
    store.put(new)
    assert store.get_by_key("k1") is new
    assert store.get_by_id("e1") is old
    assert store.get_by_id("e2") is new


def test_invalid_quota_rejected():
    with pytest.raises(ValueError):
        StateStore(quota=-1)


def test_concurrent_slot_acquisition_never_exceeds_quota():
    store = StateStore(quota=10)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        ok = store.acquire_slot(f"id-{i}")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 10
    assert store.used_slots() == 10
