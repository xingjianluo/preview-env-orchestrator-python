"""线程安全的本地状态存储：环境资源、配额槽位与索引。"""

from __future__ import annotations

import threading

from app.models import PreviewEnvironment


class StateStore:
    """以内存结构模拟环境资源池与配额，所有访问均在锁保护下进行。"""

    def __init__(self, quota: int = 3) -> None:
        if not isinstance(quota, int) or quota < 0:
            raise ValueError("quota must be a non-negative integer")
        self.quota = quota
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        # request_key -> 当前生效的环境（已回收的旧环境会被新申请覆盖）
        self._envs_by_key: dict[str, PreviewEnvironment] = {}
        # env_id -> 环境对象，保留全部历史（含已回收）
        self._envs_by_id: dict[str, PreviewEnvironment] = {}
        # 当前持有配额槽位的 env_id
        self._held_ids: set[str] = set()
        # 正在创建/重试中的 request_key，用于把同键并发串行化
        self._inflight: set[str] = set()
        # 被接管替换掉的 request_key -> 接管它的新 request_key（永久墓碑）
        self._superseded: dict[str, str] = {}
        # (project, branch) -> 当前存活（未回收）环境的 env_id
        self._live_by_branch: dict[tuple[str, str], str] = {}

    def lock(self) -> threading.RLock:
        """对外暴露锁，编排层可用它包住“检查+变更”的复合临界区。"""
        return self._lock

    def get_by_key(self, request_key: str) -> PreviewEnvironment | None:
        with self._lock:
            return self._envs_by_key.get(request_key)

    def get_by_id(self, env_id: str) -> PreviewEnvironment | None:
        with self._lock:
            return self._envs_by_id.get(env_id)

    def get_superseded(self, request_key: str) -> str | None:
        """若该 request_key 已被接管替换，返回接管它的新 request_key；否则 None。"""
        with self._lock:
            return self._superseded.get(request_key)

    def get_live(self, project: str, branch: str) -> PreviewEnvironment | None:
        """当前项目分支下存活（未回收）的环境；没有则返回 None。"""
        with self._lock:
            env_id = self._live_by_branch.get((project, branch))
            return self._envs_by_id.get(env_id) if env_id is not None else None

    def set_live(self, env: PreviewEnvironment) -> None:
        """登记环境为其项目分支下的当前存活环境（持锁调用）。"""
        self._live_by_branch[(env.project, env.branch)] = env.env_id

    def clear_live(self, env: PreviewEnvironment) -> None:
        """摘除存活登记；仅当索引仍指向该环境时生效（持锁调用）。"""
        key = (env.project, env.branch)
        if self._live_by_branch.get(key) == env.env_id:
            del self._live_by_branch[key]

    def rebind_key(self, env: PreviewEnvironment, new_request_key: str) -> str:
        """把环境从旧 request_key 原子地改绑到新 request_key（持锁调用）。

        旧键留下永久墓碑（指向新键），此后旧键的一切访问都得到
        一致且确定的“已被接管”结论；环境对象本身与槽位归属不变。
        返回被替换掉的旧 request_key。
        """
        old_key = env.request_key
        self._envs_by_key.pop(old_key, None)
        self._superseded[old_key] = new_request_key
        env.request_key = new_request_key
        self._envs_by_key[new_request_key] = env
        return old_key

    def put(self, env: PreviewEnvironment) -> None:
        """登记/更新环境；同 request_key 的新环境会覆盖键索引（旧记录仍在按 id 索引中）。"""
        with self._lock:
            self._envs_by_id[env.env_id] = env
            self._envs_by_key[env.request_key] = env

    def used_slots(self) -> int:
        with self._lock:
            return len(self._held_ids)

    def available_slots(self) -> int:
        with self._lock:
            return self.quota - len(self._held_ids)

    def acquire_slot(self, env_id: str) -> bool:
        """原子地占用一个配额槽位。

        同一 env_id 重复占用视为成功（幂等）；槽位已满时返回 False，绝不超发。
        """
        with self._lock:
            if env_id in self._held_ids:
                return True
            if len(self._held_ids) >= self.quota:
                return False
            self._held_ids.add(env_id)
            return True

    def release_slot(self, env_id: str) -> None:
        """释放槽位；对未持有槽位的 env_id 调用是安全的（幂等）。"""
        with self._lock:
            self._held_ids.discard(env_id)

    def all_envs(self) -> list[PreviewEnvironment]:
        with self._lock:
            return list(self._envs_by_id.values())

    def begin_creation(self, request_key: str) -> bool:
        """尝试把 request_key 标记为“创建中”。

        必须在持有锁时调用。返回 True 表示当前线程获得创建权；
        返回 False 表示已有同键创建在进行中。
        """
        if request_key in self._inflight:
            return False
        self._inflight.add(request_key)
        return True

    def is_inflight(self, request_key: str) -> bool:
        """该 request_key 是否仍有创建/重试在进行中（持锁调用）。"""
        return request_key in self._inflight

    def wait_for_creation(self, request_key: str, timeout: float | None = None) -> None:
        """等待同键的创建结束。必须在持有锁时调用。"""
        self._cond.wait_for(lambda: request_key not in self._inflight, timeout)

    def finish_creation(self, request_key: str) -> None:
        """解除“创建中”标记并唤醒等待者。必须在持有锁时调用。"""
        self._inflight.discard(request_key)
        self._cond.notify_all()
