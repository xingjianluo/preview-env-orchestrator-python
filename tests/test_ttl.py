"""存活时间（TTL）语义：惰性过期、后台回收、续期、资源释放。"""

from __future__ import annotations

import pytest

from preview_orchestrator.errors import EnvironmentNotFoundError
from preview_orchestrator.models import EnvironmentRequest, EnvironmentStatus


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTTL:
    def test_lazy_expiry_on_get(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(clock=clock)
        env = orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        assert env.status is EnvironmentStatus.READY
        clock.advance(59)
        assert orch.get_environment("k1").status is EnvironmentStatus.READY
        clock.advance(1)  # 到达过期时刻
        with pytest.raises(EnvironmentNotFoundError) as exc_info:
            orch.get_environment("k1")
        assert exc_info.value.code == "environment_not_found"
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()

    def test_reap_expired_collects_all_due(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(quota=5, clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b1", 30, "k1"))
        orch.request_environment(EnvironmentRequest("p", "b2", 60, "k2"))
        orch.request_environment(EnvironmentRequest("p", "b3", 90, "k3"))
        clock.advance(45)
        reaped = orch.reap_expired()
        assert reaped == ["k1"]
        assert orch.pool.used == 2
        clock.advance(100)
        reaped = orch.reap_expired()
        assert set(reaped) == {"k2", "k3"}
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()

    def test_renew_prevents_expiry(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        clock.advance(50)
        orch.renew("k1", 60)
        clock.advance(50)  # 自创建起 100s，但续期后仅过 50s
        assert orch.get_environment("k1").status is EnvironmentStatus.READY
        clock.advance(11)  # 续期时刻 + 61s
        with pytest.raises(EnvironmentNotFoundError):
            orch.get_environment("k1")

    def test_expired_environment_releases_identifier(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(quota=1, clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        clock.advance(60)
        orch.reap_expired()
        env = orch.request_environment(EnvironmentRequest("p", "b", 60, "k2"))
        assert env.status is EnvironmentStatus.READY
        assert orch.pool.used == 1

    def test_background_reaper_reclaims_automatically(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(quota=2, clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b1", 30, "k1"))
        orch.start_reaper(interval_seconds=0.02)
        clock.advance(30)
        # 等待后台线程完成一轮扫描（用真实时间等待）
        import time as _time

        deadline = _time.time() + 5
        while orch.pool.used == 1 and _time.time() < deadline:
            _time.sleep(0.02)
        assert orch.pool.used == 0
        assert orch.provisioner.active_ids == set()
        with pytest.raises(EnvironmentNotFoundError):
            orch.get_environment("k1")

    def test_expired_reclaim_is_idempotent(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        clock.advance(60)
        orch.reap_expired()
        # 已被 TTL 回收，显式回收仍然安全
        env = orch.reclaim("k1")
        assert env.status is EnvironmentStatus.RECLAIMED
        assert orch.pool.used == 0

    def test_submitting_to_expired_key_rejected_not_duplicated(self, make_orchestrator):
        clock = FakeClock()
        orch = make_orchestrator(quota=1, clock=clock)
        orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
        clock.advance(60)
        orch.reap_expired()
        # 用旧请求键重复提交：拒绝访问，而不是创建第二份资源
        with pytest.raises(EnvironmentNotFoundError):
            orch.request_environment(EnvironmentRequest("p", "b", 60, "k1"))
