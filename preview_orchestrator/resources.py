"""本地资源层：模拟真实的环境资源（计算槽位）与配额。

资源池只关心"物理资源"的占用/释放，不知道请求键与状态机；
状态机与幂等逻辑由 orchestrator 负责。
"""

from __future__ import annotations

import threading


class ResourcePool:
    """固定容量的本地资源池，用锁保证并发下不超卖。"""

    def __init__(self, capacity: int) -> None:
        if capacity < 0:
            raise ValueError("capacity 不能为负数")
        self._capacity = capacity
        self._used = 0
        self._cond = threading.Condition(threading.Lock())

    def acquire(self) -> bool:
        """尝试占用一个资源槽位，成功返回 True，配额耗尽返回 False。"""
        with self._cond:
            if self._used >= self._capacity:
                return False
            self._used += 1
            return True

    def release(self) -> None:
        """释放一个槽位。释放超过占用数属于编程错误，抛出异常。"""
        with self._cond:
            if self._used <= 0:
                raise RuntimeError("资源池占用数已为 0，不能重复释放")
            self._used -= 1
            self._cond.notify_all()

    @property
    def capacity(self) -> int:
        with self._cond:
            return self._capacity

    @property
    def used(self) -> int:
        with self._cond:
            return self._used

    @property
    def available(self) -> int:
        with self._cond:
            return self._capacity - self._used


class Provisioner:
    """模拟环境创建/销毁的底层执行器（可注入失败）。"""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()
        # 接下来 N 次 provision 调用强制失败（模拟瞬时故障，用于重试测试）
        self.fail_next_n = 0
        # 始终失败的环境 id 集合（模拟确定性故障）
        self.always_fail_ids: set[str] = set()
        # 每次 provision 持锁耗时，用于在测试中制造并发窗口
        self.provision_delay: float = 0.0

    def provision(self, environment_id: str) -> None:
        """创建底层资源，失败时抛 RuntimeError，且失败不留任何资源。"""
        import time

        with self._lock:
            if self.fail_next_n > 0:
                self.fail_next_n -= 1
                raise RuntimeError(f"模拟创建失败（瞬时）: {environment_id}")
            if environment_id in self.always_fail_ids:
                raise RuntimeError(f"模拟创建失败（确定性）: {environment_id}")
            # 已存在视为幂等成功，不重复创建
            self._active.add(environment_id)
            if self.provision_delay:
                time.sleep(self.provision_delay)

    def destroy(self, environment_id: str) -> None:
        """销毁底层资源，重复销毁必须安全（幂等）。"""
        with self._lock:
            self._active.discard(environment_id)

    @property
    def active_ids(self) -> set[str]:
        """当前仍然存活的底层资源 id（用于泄漏检测）。"""
        with self._lock:
            return set(self._active)
