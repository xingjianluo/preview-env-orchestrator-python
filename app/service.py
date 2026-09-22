"""故障注入与资源分配的模拟支撑服务。

用确定性内存结构模拟真实的环境资源后端：
- allocate 为每个环境生成稳定命名的模拟资源；
- 可按 env_id 注入“下一次分配失败”，用于复现创建失败与重试场景；
- 跟踪全部活跃资源，便于测试断言无泄漏；
- deallocate 幂等，重复释放同一资源不会出错也不会重复计数。
"""

from __future__ import annotations

import threading


class ResourceError(RuntimeError):
    """模拟后端在分配/释放资源时发生的故障。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ResourceBackend:
    """模拟真实资源后端，可按 env_id 注入确定性失败。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 下一次 allocate(env_id) 时生效的失败原因，消费后即移除
        self._fail_once: dict[str, str] = {}
        # 每次 allocate 都失败（可选，便于构造持续失败）
        self._always_fail: dict[str, str] = {}
        # env_id -> 当前已分配资源
        self._allocated: dict[str, list[str]] = {}

    def reset(self) -> None:
        with self._lock:
            self._fail_once.clear()
            self._always_fail.clear()
            self._allocated.clear()

    def fail_once(self, env_id: str, reason: str = "injected") -> None:
        """令该 env_id 的下一次 allocate 抛出 ResourceError。"""
        with self._lock:
            self._fail_once[env_id] = reason

    def always_fail(self, env_id: str, reason: str = "persistent") -> None:
        """令该 env_id 的每次 allocate 都失败，直到调用 clear_failures。"""
        with self._lock:
            self._always_fail[env_id] = reason

    def clear_failures(self, env_id: str) -> None:
        with self._lock:
            self._fail_once.pop(env_id, None)
            self._always_fail.pop(env_id, None)

    def allocate(self, env_id: str) -> list[str]:
        """分配一组模拟资源。

        成功返回资源标识列表；失败时抛出 ResourceError 且不会留下任何资源。
        同一 env_id 在未释放前重复分配会触发 IDENTIFIER 冲突式错误，
        以帮助发现编排层的重复创建问题。
        """
        with self._lock:
            reason = self._fail_once.pop(env_id, None) or self._always_fail.get(env_id)
            if reason is not None:
                raise ResourceError(reason)
            if env_id in self._allocated:
                raise ResourceError(f"duplicate allocation for {env_id}")
            resources = [f"{env_id}/database", f"{env_id}/service", f"{env_id}/route"]
            self._allocated[env_id] = list(resources)
            return resources

    def deallocate(self, resources: list[str]) -> None:
        """释放资源；对空列表或已释放资源重复调用均安全（幂等）。"""
        with self._lock:
            for env_id, owned in list(self._allocated.items()):
                remaining = [r for r in owned if r not in resources]
                if len(remaining) != len(owned):
                    if remaining:
                        self._allocated[env_id] = remaining
                    else:
                        del self._allocated[env_id]

    def active_resources(self) -> set[str]:
        """当前仍被占用的全部资源标识（测试用于泄漏检测）。"""
        with self._lock:
            return {r for rs in self._allocated.values() for r in rs}

    def active_count(self) -> int:
        with self._lock:
            return sum(len(rs) for rs in self._allocated.values())
