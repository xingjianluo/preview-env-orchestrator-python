"""预览环境编排核心：申请、查询、续期、回收、重试与超时回收。

一致性约定：
- 所有“检查 + 状态变更 + 配额变更”的复合操作都在 StateStore 的同一把锁内完成；
- request_key 是幂等键：同一键的重复申请复用当前环境；同键并发创建通过
  “创建中”标记串行化——一个线程创建，其余线程等待并复用其结果，绝不重复创建；
- 资源后端（allocate/deallocate）在锁外调用，提交结果时在锁内做二次核验，
  消除“资源已分配但环境已被回收”等竞态；
- 配额槽位在创建时占用，进入 failed/reclaimed 时释放，生命周期与资源严格对齐。
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Callable

from app.errors import ErrorCode, OrchestratorError
from app.logging_setup import action_log, configure_logging, reject_log, transition_log
from app.models import (
    ALLOWED_TRANSITIONS,
    EnvStatus,
    PreviewEnvironment,
    ProvisionRequest,
)
from app.service import ResourceBackend, ResourceError
from app.store import StateStore

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    out = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return out or "x"


class PreviewEnvOrchestrator:
    def __init__(
        self,
        quota: int = 3,
        default_ttl_seconds: float = 3600.0,
        max_ttl_seconds: float = 86400.0,
        backend: ResourceBackend | None = None,
        clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if quota < 0:
            raise ValueError("quota must be non-negative")
        if default_ttl_seconds <= 0 or max_ttl_seconds <= 0:
            raise ValueError("ttl must be positive")
        self.default_ttl_seconds = float(default_ttl_seconds)
        self.max_ttl_seconds = float(max_ttl_seconds)
        self.store = StateStore(quota=quota)
        self.backend = backend or ResourceBackend()
        self._clock = clock or time.monotonic
        self.log = logger or configure_logging()

    # ------------------------------------------------------------------ utils

    def now(self) -> float:
        return self._clock()

    def _new_env_id_locked(self, project: str, branch: str) -> str:
        """调用方持锁。同项目/分支的新环境获得确定性递增标识，历史标识永不复用。"""
        base = f"{_slug(project)}-{_slug(branch)}"
        used = {e.env_id for e in self.store.all_envs()}
        if base not in used:
            return base
        n = 2
        while f"{base}-{n}" in used:
            n += 1
        return f"{base}-{n}"

    def _transition_locked(self, env: PreviewEnvironment, to: EnvStatus, note: str = "") -> None:
        """调用方必须持有 store 锁。"""
        frm = env.status
        if to not in ALLOWED_TRANSITIONS[frm]:
            raise OrchestratorError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"illegal transition {frm.value} -> {to.value}",
                request_key=env.request_key,
            )
        env.status = to
        env.history.append(f"{self.now():.3f} {frm.value}->{to.value} {note}".rstrip())
        transition_log(self.log, env.request_key, env.env_id, frm.value, to.value, note)

    def _release_slot_locked(self, env: PreviewEnvironment) -> None:
        if env.slot_held:
            env.slot_held = False
            self.store.release_slot(env.env_id)

    def _validate_ttl(self, ttl: float | None) -> float:
        value = self.default_ttl_seconds if ttl is None else ttl
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise OrchestratorError(ErrorCode.INVALID_PARAMETER, "ttl_seconds must be a number")
        if value <= 0:
            raise OrchestratorError(ErrorCode.INVALID_PARAMETER, "ttl_seconds must be > 0")
        if value > self.max_ttl_seconds:
            raise OrchestratorError(
                ErrorCode.INVALID_PARAMETER,
                f"ttl_seconds {value:g} exceeds max {self.max_ttl_seconds:g}",
            )
        return value

    @staticmethod
    def _validate_identity(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise OrchestratorError(ErrorCode.INVALID_PARAMETER, f"{field} must be non-empty")
        return value.strip()

    def _get_by_key_locked(self, request_key: str) -> PreviewEnvironment:
        """调用方必须持有 store 锁。按 request_key 查环境，查无时报错。

        被接管替换的旧键命中永久墓碑，报 REQUEST_KEY_SUPERSEDED（而非
        ENV_NOT_FOUND），使老标识的查询结论清楚且一致。
        """
        env = self.store.get_by_key(request_key)
        if env is None:
            superseded_by = self.store.get_superseded(request_key)
            if superseded_by is not None:
                raise OrchestratorError(
                    ErrorCode.REQUEST_KEY_SUPERSEDED,
                    f"request_key {request_key!r} was superseded by {superseded_by!r}; "
                    "use the new identifier",
                    request_key=request_key,
                )
            raise OrchestratorError(
                ErrorCode.ENV_NOT_FOUND,
                f"no environment for request_key {request_key!r}",
                request_key=request_key,
            )
        return env

    def _reclaim_locked(self, env: PreviewEnvironment, reason: str) -> bool:
        """调用方必须持有 store 锁。返回是否真正释放了资源（重复回收返回 False）。"""
        if env.status == EnvStatus.RECLAIMED:
            return False
        released = False
        if env.resource_identifiers:
            self.backend.deallocate(list(env.resource_identifiers))
            env.resource_identifiers.clear()
            released = True
        self._release_slot_locked(env)
        self.store.clear_live(env)
        env.expires_at = None
        self._transition_locked(env, EnvStatus.RECLAIMED, reason)
        action_log(self.log, "reclaim", env.request_key, env.env_id, reason=reason)
        return released

    def _reap_expired_locked(self, current: float) -> list[str]:
        """调用方必须持有 store 锁。回收所有已到期环境，返回被回收的 env_id。

        这是“到期即时生效”的核心：每个公开操作进入临界区时都会先调用它，
        因此任何已过期的环境在被查询/续期/接管判断之前就被确定性回收，
        其槽位与资源立即让出，不依赖人工触发清理。
        """
        reclaimed: list[str] = []
        candidates = [
            e for e in self.store.all_envs()
            if e.status == EnvStatus.READY
            and e.expires_at is not None
            and current >= e.expires_at
        ]
        for env in candidates:
            if self._reclaim_locked(env, f"ttl expired at {env.expires_at:g}"):
                reclaimed.append(env.env_id)
        return reclaimed

    # -------------------------------------------------------------- provision

    def provision(self, req: ProvisionRequest) -> PreviewEnvironment:
        # 先解析请求键，使任何参数拒绝都能在错误体中带回 request_key
        request_key = self._validate_identity(req.request_key, "request_key")
        try:
            project = self._validate_identity(req.project, "project")
            branch = self._validate_identity(req.branch, "branch")
            ttl = self._validate_ttl(req.ttl_seconds)
        except OrchestratorError as exc:
            if exc.request_key is None:
                exc.request_key = request_key
            raise

        with self.store.lock():
            # 到期即时生效：先做惰性回收，过期环境立即释放容量、不可再被复用/接管
            self._reap_expired_locked(self.now())
            # 同键并发：要么复用已存在环境，要么等创建者完成后复用其结果
            while True:
                existing = self.store.get_by_key(request_key)
                # 仅当环境存在、未回收、且没有同键创建仍在进行时才可复用；
                # 否则可能读到 provisioning 中间态（含条件变量虚假唤醒）。
                if (
                    existing is not None
                    and existing.status != EnvStatus.RECLAIMED
                    and not self.store.is_inflight(request_key)
                ):
                    if (existing.project, existing.branch) != (project, branch):
                        raise OrchestratorError(
                            ErrorCode.IDENTIFIER_CONFLICT,
                            f"request_key {request_key!r} already bound to "
                            f"{existing.project}/{existing.branch}",
                            request_key=request_key,
                        )
                    existing.reused = True
                    existing.taken_over = False
                    action_log(
                        self.log, "provision-reuse", request_key, existing.env_id,
                        status=existing.status.value,
                    )
                    return existing
                if existing is None:
                    # 该键曾被接管替换：永久墓碑，拒绝复用以防止新旧标识来回翻转
                    superseded_by = self.store.get_superseded(request_key)
                    if superseded_by is not None:
                        reject_log(
                            self.log, request_key, ErrorCode.REQUEST_KEY_SUPERSEDED,
                            f"superseded by {superseded_by}",
                        )
                        raise OrchestratorError(
                            ErrorCode.REQUEST_KEY_SUPERSEDED,
                            f"request_key {request_key!r} was superseded by "
                            f"{superseded_by!r} and is permanently retired",
                            request_key=request_key,
                        )
                if self.store.begin_creation(request_key):
                    break
                action_log(self.log, "provision-await", request_key)
                self.store.wait_for_creation(request_key)
                # 被唤醒（含虚假唤醒）后重新检查，直到创建结束并出现终态

            # 本线程获得创建权
            try:
                # 同项目分支已有存活环境：新标识接管同一份环境，绝不并存第二份。
                # 接管不新增槽位（环境已持有；failed 环境本就不占槽）；
                # 过期环境已在进入临界区时被惰性回收，不会在这里被“救活”。
                live = self.store.get_live(project, branch)
                if live is not None:
                    old_key = self.store.rebind_key(live, request_key)
                    live.ttl_seconds = ttl
                    if live.status == EnvStatus.READY:
                        # 存活时间从接管（新的就绪）时刻重新起算
                        live.expires_at = self.now() + ttl
                    live.taken_over = True
                    live.reused = False
                    live.history.append(
                        f"{self.now():.3f} takeover: {old_key} -> {request_key}"
                    )
                    self.store.finish_creation(request_key)
                    action_log(
                        self.log, "takeover", request_key, live.env_id,
                        old_key=old_key, status=live.status.value,
                    )
                    return live
                if self.store.available_slots() <= 0:
                    reject_log(
                        self.log, request_key, ErrorCode.QUOTA_EXHAUSTED,
                        f"quota={self.store.quota} used={self.store.used_slots()}",
                    )
                    raise OrchestratorError(
                        ErrorCode.QUOTA_EXHAUSTED,
                        f"quota exhausted: used {self.store.used_slots()} of {self.store.quota}",
                        request_key=request_key,
                    )
                env_id = self._new_env_id_locked(project, branch)
                env = PreviewEnvironment(
                    env_id=env_id,
                    project=project,
                    branch=branch,
                    request_key=request_key,
                    status=EnvStatus.PROVISIONING,
                    created_at=self.now(),
                    ttl_seconds=ttl,
                    slot_held=True,
                )
                env.history.append(f"{env.created_at:.3f} created")
                if not self.store.acquire_slot(env_id):
                    env.slot_held = False
                    reject_log(
                        self.log, request_key, ErrorCode.QUOTA_EXHAUSTED,
                        "slot lost to concurrent request", env_id=env_id,
                    )
                    raise OrchestratorError(
                        ErrorCode.QUOTA_EXHAUSTED,
                        "quota exhausted under concurrent contention",
                        request_key=request_key,
                    )
                # 先登记再调用后端：等待者能观察到 provisioning 并最终复用同一对象
                self.store.put(env)
                self.store.set_live(env)
            except BaseException:
                self.store.finish_creation(request_key)
                raise
            action_log(self.log, "provision-create", request_key, env_id, ttl=ttl)

        # ---- 锁外：模拟慢资源分配 ----
        try:
            resources = self.backend.allocate(env_id)
        except ResourceError as exc:
            with self.store.lock():
                self._release_slot_locked(env)
                if env.status == EnvStatus.RECLAIMED:
                    # 失败返回前环境已被并发回收：回收者已负责释放，
                    # 这里不得再执行 reclaimed -> failed 的非法流转
                    env.history.append(
                        f"{self.now():.3f} allocate failed after reclaim: {exc.reason}"
                    )
                    self.store.finish_creation(request_key)
                    action_log(
                        self.log, "provision-fail-race-reclaimed", request_key, env_id
                    )
                    return env
                env.failure_reason = exc.reason
                env.failure_count += 1
                self._transition_locked(env, EnvStatus.FAILED, f"allocate failed: {exc.reason}")
                self.store.finish_creation(request_key)
            reject_log(
                self.log, request_key, ErrorCode.PROVISION_FAILED,
                exc.reason, env_id=env_id,
            )
            return env

        with self.store.lock():
            if env.status == EnvStatus.RECLAIMED:
                # 分配期间被显式回收：立即归还资源与槽位
                self.backend.deallocate(resources)
                self._release_slot_locked(env)
                self.store.finish_creation(request_key)
                action_log(self.log, "provision-race-reclaimed", request_key, env_id)
                return env
            if env.status is not EnvStatus.PROVISIONING:
                # 理论上不可达，防御性处理，避免状态自相矛盾
                self.backend.deallocate(resources)
                self.store.finish_creation(request_key)
                raise OrchestratorError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    f"unexpected status during allocation commit: {env.status.value}",
                    request_key=request_key,
                )
            env.resource_identifiers = resources
            env.ready_at = self.now()
            env.expires_at = env.ready_at + env.ttl_seconds
            self._transition_locked(env, EnvStatus.READY, "allocated")
            self.store.finish_creation(request_key)
            return env

    # ------------------------------------------------------------------ retry

    def retry_failed(self, request_key: str) -> PreviewEnvironment:
        request_key = self._validate_identity(request_key, "request_key")

        with self.store.lock():
            self._reap_expired_locked(self.now())
            # 同键串行化；在循环内统一复查状态，晚到线程复用已完成的重试结果
            while True:
                env = self._get_by_key_locked(request_key)
                if env.status == EnvStatus.RECLAIMED:
                    raise OrchestratorError(
                        ErrorCode.ENV_RECLAIMED,
                        f"environment {env.env_id} has been reclaimed",
                        request_key=request_key,
                    )
                if env.status == EnvStatus.READY:
                    # 其他线程已重试成功：幂等复用
                    env.reused = True
                    env.taken_over = False
                    action_log(self.log, "retry-reuse", request_key, env.env_id)
                    return env
                if env.status == EnvStatus.PROVISIONING or not self.store.begin_creation(
                    request_key
                ):
                    # 创建者正在分配中（或令牌被抢）：等待后重新检查
                    self.store.wait_for_creation(request_key)
                    continue
                # 本线程获得重试权且 env 处于 failed
                break

            if env.status != EnvStatus.FAILED:
                self.store.finish_creation(request_key)
                raise OrchestratorError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    f"only failed environments can be retried, current={env.status.value}",
                    request_key=request_key,
                )
            if not self.store.acquire_slot(env.env_id):
                self.store.finish_creation(request_key)
                reject_log(
                    self.log, request_key, ErrorCode.QUOTA_EXHAUSTED,
                    "cannot retry: quota exhausted", env_id=env.env_id,
                )
                raise OrchestratorError(
                    ErrorCode.QUOTA_EXHAUSTED,
                    f"quota exhausted: used {self.store.used_slots()} of {self.store.quota}",
                    request_key=request_key,
                )
            env.slot_held = True
            env.failure_reason = None
            self._transition_locked(env, EnvStatus.PROVISIONING, "retry")

        try:
            resources = self.backend.allocate(env.env_id)
        except ResourceError as exc:
            with self.store.lock():
                self._release_slot_locked(env)
                if env.status == EnvStatus.RECLAIMED:
                    env.history.append(
                        f"{self.now():.3f} retry failed after reclaim: {exc.reason}"
                    )
                    self.store.finish_creation(request_key)
                    return env
                env.failure_reason = exc.reason
                env.failure_count += 1
                self._transition_locked(env, EnvStatus.FAILED, f"retry failed: {exc.reason}")
                self.store.finish_creation(request_key)
            reject_log(
                self.log, request_key, ErrorCode.PROVISION_FAILED,
                exc.reason, env_id=env.env_id,
            )
            return env

        with self.store.lock():
            if env.status == EnvStatus.RECLAIMED:
                self.backend.deallocate(resources)
                self._release_slot_locked(env)
                self.store.finish_creation(request_key)
                return env
            env.resource_identifiers = resources
            env.ready_at = self.now()
            env.expires_at = env.ready_at + env.ttl_seconds
            self._transition_locked(env, EnvStatus.READY, "retry allocated")
            self.store.finish_creation(request_key)
            return env

    # -------------------------------------------------------------------- get

    def get(self, request_key: str) -> PreviewEnvironment:
        request_key = self._validate_identity(request_key, "request_key")
        with self.store.lock():
            self._reap_expired_locked(self.now())
            env = self._get_by_key_locked(request_key)
            if env.status == EnvStatus.RECLAIMED:
                raise OrchestratorError(
                    ErrorCode.ENV_RECLAIMED,
                    f"environment {env.env_id} has been reclaimed and is no longer accessible",
                    request_key=request_key,
                )
            return env

    # ------------------------------------------------------------------ renew

    def renew(self, request_key: str, ttl_seconds: float | None = None) -> PreviewEnvironment:
        request_key = self._validate_identity(request_key, "request_key")
        ttl = self._validate_ttl(ttl_seconds)
        with self.store.lock():
            self._reap_expired_locked(self.now())
            env = self._get_by_key_locked(request_key)
            if env.status == EnvStatus.RECLAIMED:
                raise OrchestratorError(
                    ErrorCode.ENV_RECLAIMED,
                    f"environment {env.env_id} has been reclaimed",
                    request_key=request_key,
                )
            if env.status != EnvStatus.READY:
                raise OrchestratorError(
                    ErrorCode.NOT_READY,
                    f"only ready environments can renew lease, current={env.status.value}",
                    request_key=request_key,
                )
            if env.expires_at is not None and self.now() >= env.expires_at:
                self._reclaim_locked(env, "expired before renew")
                raise OrchestratorError(
                    ErrorCode.ENV_RECLAIMED,
                    f"environment {env.env_id} already expired and was reclaimed",
                    request_key=request_key,
                )
            env.ttl_seconds = ttl
            env.expires_at = self.now() + ttl
            env.history.append(f"{self.now():.3f} lease renewed ttl={ttl:g}")
            action_log(self.log, "renew", request_key, env.env_id, expires_at=env.expires_at)
            return env

    # ---------------------------------------------------------------- reclaim

    def reclaim(self, request_key: str) -> tuple[PreviewEnvironment, bool]:
        request_key = self._validate_identity(request_key, "request_key")
        with self.store.lock():
            self._reap_expired_locked(self.now())
            env = self._get_by_key_locked(request_key)
            released = self._reclaim_locked(env, "explicit reclaim")
            return env, released

    # ------------------------------------------------------------ reaper (ttl)

    def reap_expired(self, now: float | None = None) -> list[str]:
        """回收所有已超过存活时间的 READY 环境，返回被回收的 env_id（幂等）。"""
        current = self.now() if now is None else now
        with self.store.lock():
            return self._reap_expired_locked(current)

    # ------------------------------------------------------------------ stats

    def quota_snapshot(self) -> dict[str, int]:
        with self.store.lock():
            used = self.store.used_slots()
            by_status = Counter(e.status.value for e in self.store.all_envs())
        return {
            "quota": self.store.quota,
            "used": used,
            "available": self.store.quota - used,
            **{f"status_{k}": v for k, v in by_status.items()},
        }
