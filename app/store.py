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
        # request_key -> 当前生效的环境（已回收/被接管换走的键会被移除或覆盖）
        self._envs_by_key: dict[str, PreviewEnvironment] = {}
        # env_id -> 环境对象，保留全部历史（含已回收）
        self._envs_by_id: dict[str, PreviewEnvironment] = {}
        # (project, branch) -> 当前存活（未回收）的唯一环境
        self._live_by_branch: dict[tuple[str, str], PreviewEnvironment] = {}
        # 被接管换走的老 request_key -> (env_id, 接管它的 request_key)
        self._superseded: dict[str, tuple[str, str]] = {}
        # 当前持有配额槽位的 env_id
        self._held_ids: set[str] = set()
        # 正在创建/重试中的 request_key，用于把同键并发串行化
        self._inflight: set[str] = set()

    def lock(self) -> threading.RLock:
        """对外暴露锁，编排层可用它包住“检查+变更”的复合临界区。"""
        return self._lock

    def get_by_key(self, request_key: str) -> PreviewEnvironment | None:
        with self._lock:
            return self._envs_by_key.get(request_key)

    def get_by_id(self, env_id: str) -> PreviewEnvironment | None:
        with self._lock:
            return self._envs_by_id.get(env_id)

    def put(self, env: PreviewEnvironment) -> None:
        """登记/更新环境。

        同 request_key 的新环境会覆盖键索引（旧记录仍在按 id 索引中）；
        未回收环境同时登记到 (project, branch) 的单活索引。
        """
        with self._lock:
            self._envs_by_id[env.env_id] = env
            self._envs_by_key[env.request_key] = env
            # 复用时若 key 索引已指向别的 env，不会发生；这里只维护分支单活
            if env.status.value != "reclaimed":
                self._live_by_branch[(env.project, env.branch)] = env

    def live_for_branch(self, project: str, branch: str) -> PreviewEnvironment | None:
        """返回该项目+分支当前存活（未回收）的环境；索引若过期到 reclaimed 则摘除。

        必须在持有锁时调用。
        """
        env = self._live_by_branch.get((project, branch))
        if env is not None and env.status.value == "reclaimed":
            current = self._live_by_branch.get((project, branch))
            if current is env:
                del self._live_by_branch[(project, branch)]
            return None
        return env

    def clear_branch_live(self, env: PreviewEnvironment) -> None:
        """回收时摘除分支单活索引（仅当索引确实指向该 env）。持锁调用。"""
        if self._live_by_branch.get((env.project, env.branch)) is env:
            del self._live_by_branch[(env.project, env.branch)]

    def rebind_key(
        self, env: PreviewEnvironment, new_key: str
    ) -> str | None:
        """把存活环境的归属 request_key 切换为 new_key（接管/标识轮换）。

        必须在持有锁时调用。返回被替换下来的老 request_key（env 原本的键与
        new_key 不同时）；同键调用返回 None。老键记入 superseded 索引，
        此后对老键的查询/续期/回收都得到“已被接管”的确定结论。
        """
        old_key = env.request_key
        if old_key == new_key:
            return None
        env.request_key = new_key
        self._envs_by_key[new_key] = env
        self._envs_by_key.pop(old_key, None)
        # new_key 若历史上被别的接管换走过，现在重新成为属主，摘掉其墓碑
        self._superseded.pop(new_key, None)
        self._superseded[old_key] = (env.env_id, new_key)
        return old_key

    def superseded_info(self, request_key: str) -> tuple[str, str] | None:
        """返回 (env_id, 当前属主 request_key)；该键从未被接管则返回 None。"""
        with self._lock:
            return self._superseded.get(request_key)

    def is_superseded(self, request_key: str) -> bool:
        with self._lock:
            return request_key in self._superseded

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

    def wait_until_settled(
        self, env: PreviewEnvironment, timeout: float | None = None
    ) -> None:
        """等待某个环境离开 provisioning（成功/失败/回收均算 settle）。

        接管方在环境仍在创建中时使用：标识换绑不影响“创建中”标记，
        因此按 request_key 等待不可靠，需要直接观察环境状态。持锁调用。
        """
        self._cond.wait_for(lambda: env.status.value != "provisioning", timeout)

    def finish_creation(self, request_key: str) -> None:
        """解除“创建中”标记并唤醒等待者。必须在持有锁时调用。"""
        self._inflight.discard(request_key)
        self._cond.notify_all()
