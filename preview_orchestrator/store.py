"""环境记录的线程安全存储。"""

from __future__ import annotations

import threading

from .models import EnvironmentStatus, PreviewEnvironment


class EnvironmentStore:
    """以 request_key 为主键的环境存储，外加 (project, branch) 唯一索引。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[str, PreviewEnvironment] = {}
        # (project, branch) -> request_key，仅索引"未回收"的环境，
        # 回收后删除索引项，同一项目分支可再次申请。
        self._index: dict[tuple[str, str], str] = {}

    def get(self, request_key: str) -> PreviewEnvironment | None:
        with self._lock:
            env = self._by_key.get(request_key)
            if env is None or env.status is EnvironmentStatus.RECLAIMED:
                return None
            return env

    def get_including_reclaimed(self, request_key: str) -> PreviewEnvironment | None:
        """返回记录本身，即使已经 RECLAIMED（编排器用于区分拒绝原因）。"""
        with self._lock:
            return self._by_key.get(request_key)

    def put_if_absent(
        self, request_key: str, environment: PreviewEnvironment
    ) -> PreviewEnvironment:
        """不存在则放入并返回新对象；已存在则返回已有对象（幂等）。"""
        with self._lock:
            existing = self._by_key.get(request_key)
            if existing is not None:
                return existing
            self._by_key[request_key] = environment
            if environment.status is not EnvironmentStatus.RECLAIMED:
                self._index[(environment.project, environment.branch)] = request_key
            return environment

    def find_by_project_branch(self, project: str, branch: str) -> PreviewEnvironment | None:
        with self._lock:
            key = self._index.get((project, branch))
            if key is None:
                return None
            return self.get(key)

    def mark_reclaimed(self, environment: PreviewEnvironment) -> None:
        """环境回收后从 (project, branch) 索引中移除，释放标识占用。"""
        with self._lock:
            current_key = self._index.get((environment.project, environment.branch))
            if current_key == environment.request_key:
                del self._index[(environment.project, environment.branch)]

    def all(self) -> list[PreviewEnvironment]:
        with self._lock:
            return [
                env
                for env in self._by_key.values()
                if env.status is not EnvironmentStatus.RECLAIMED
            ]

    @property
    def lock(self) -> threading.RLock:
        """存储内部锁，编排器可用它做复合检查（必须是可重入锁）。"""
        return self._lock
