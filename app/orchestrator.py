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
        env.expires_at = None
        self.store.clear_branch_live(env)
        self._transition_locked(env, EnvStatus.RECLAIMED, reason)
        action_log(self.log, "reclaim", env.request_key, env.env_id, reason=reason)
        return released

    def _sweep_expired_locked(self) -> list[str]:
        """调用方必须持有 store 锁。立即回收所有已到期的 READY 环境。

        过期即时生效：任何查询/续期/接管/申请进入临界区后都先调用本方法，
        过期环境立刻变为 reclaimed、释放后端资源与配额槽位、退出分支单活索引，
        无需等待显式的 reap 扫描。返回本次被回收的 env_id。
        """
        current = self.now()
        reclaimed: list[str] = []
        for env in self.store.all_envs():
            if (
                env.status == EnvStatus.READY
                and env.expires_at is not None
                and current >= env.expires_at
            ):
                if self._reclaim_locked(env, f"ttl expired at {env.expires_at:g}"):
                    reclaimed.append(env.env_id)
        return reclaimed

    def _superseded_error_locked(self, request_key: str) -> OrchestratorError:
        """老标识在被接管换走后得到的确定性结论（区别于其他拒绝原因）。"""
        info = self.store.superseded_info(request_key)
        if info is not None:
            env_id, owner = info
            message = (
                f"request_key {request_key!r} no longer owns environment {env_id}; "
                f"ownership was taken over by {owner!r}"
            )
        else:  # 防御性兜底，正常路径下调用时必有墓碑
            message = f"request_key {request_key!r} has been superseded by a newer request"
        return OrchestratorError(
            ErrorCode.IDENTIFIER_SUPERSEDED, message, request_key=request_key
        )

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
            # 同键并发 / 标识轮换共用一个决策循环：任何一次醒来都重新做全量检查，
            # 结论只取决于锁内的当前状态，因此重复/并发提交结论确定可复现。
            while True:
                # 过期即时生效：先把已到期环境回收、让出容量与分支单活索引
                self._sweep_expired_locked()

                # 该标识曾被接管换走 => 永久不再是属主，结论始终一致
                if self.store.is_superseded(request_key):
                    raise self._superseded_error_locked(request_key)

                existing = self.store.get_by_key(request_key)
                if existing is not None:
                    if (existing.project, existing.branch) != (project, branch):
                        raise OrchestratorError(
                            ErrorCode.IDENTIFIER_CONFLICT,
                            f"request_key {request_key!r} already bound to "
                            f"{existing.project}/{existing.branch}",
                            request_key=request_key,
                        )
                    if existing.status == EnvStatus.RECLAIMED:
                        pass  # 同键回收后重建：落到下方创建流程
                    elif existing.status == EnvStatus.PROVISIONING or self.store.is_inflight(
                        request_key
                    ):
                        # 同键创建仍在进行（含虚假唤醒）：等环境 settle 后重新检查
                        action_log(self.log, "provision-await", request_key)
                        self.store.wait_until_settled(existing)
                        continue
                    else:
                        # ready / failed：幂等复用同一对象
                        existing.reused = True
                        action_log(
                            self.log, "provision-reuse", request_key, existing.env_id,
                            status=existing.status.value,
                        )
                        return existing

                # 分支单活：同 project+branch 已有存活环境 => 新标识接管同一份环境，
                # 绝不并存第二份。过期环境已在上面清扫掉，因此不会“救活”过期环境。
                live = self.store.live_for_branch(project, branch)
                if live is not None:
                    self.store.rebind_key(live, request_key)
                    action_log(
                        self.log, "takeover", request_key, live.env_id,
                        status=live.status.value,
                    )
                    live.history.append(
                        f"{self.now():.3f} ownership taken over by key={request_key}"
                    )
                    # 无论被接管环境处于哪种状态，本次申请的 TTL 都取代旧时长：
                    # provisioning/failed 在（重试）就绪提交时按 ready_at + 新 TTL 起算，
                    # ready 则立即从接管时刻重起租约。三种状态结论一致。
                    live.ttl_seconds = ttl
                    live.history.append(
                        f"{self.now():.3f} ttl superseded by takeover ttl={ttl:g}"
                    )
                    if live.status == EnvStatus.PROVISIONING:
                        # 接管发生在别人的创建过程中：等就绪/失败结论，再以属主身份复用
                        self.store.wait_until_settled(live)
                        continue
                    if live.status == EnvStatus.READY:
                        # 租约从接管时刻重新起算（使用本次申请的 TTL）
                        live.ready_at = self.now()
                        live.expires_at = live.ready_at + ttl
                        live.history.append(
                            f"{self.now():.3f} lease restarted on takeover ttl={ttl:g}"
                        )
                        action_log(
                            self.log, "takeover-lease-restart", request_key, live.env_id,
                            expires_at=live.expires_at,
                        )
                    live.reused = True
                    return live

                # 本线程获得创建权（全新申请或同键回收后重建）
                if not self.store.begin_creation(request_key):
                    self.store.wait_for_creation(request_key)
                    continue
                try:
                    if self.store.available_slots() <= 0:
                        used, total = self.store.used_slots(), self.store.quota
                        reject_log(
                            self.log, request_key, ErrorCode.QUOTA_EXHAUSTED,
                            f"quota={total} used={used}",
                        )
                        raise OrchestratorError(
                            ErrorCode.QUOTA_EXHAUSTED,
                            f"quota exhausted: used {used} of {total}",
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
                    # 先登记再调用后端：等待者/接管方能观察到 provisioning 同一对象
                    self.store.put(env)
                except BaseException:
                    self.store.finish_creation(request_key)
                    raise
                action_log(self.log, "provision-create", request_key, env_id, ttl=ttl)
                break

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
                    # 回收前若归属已被别的标识接管：老标识得到确定性的“已被接管”
                    if env.request_key != request_key:
                        raise self._superseded_error_locked(request_key)
                    return env
                env.failure_reason = exc.reason
                env.failure_count += 1
                self._transition_locked(env, EnvStatus.FAILED, f"allocate failed: {exc.reason}")
                self.store.finish_creation(request_key)
                owner_changed = env.request_key != request_key
            reject_log(
                self.log, env.request_key, ErrorCode.PROVISION_FAILED,
                exc.reason, env_id=env_id,
            )
            if owner_changed:
                # 创建期间环境被新标识接管：失败结论归接管方所有，
                # 创建者的老标识此后只能看到“已被接管”
                raise self._superseded_error_locked(request_key)
            return env

        with self.store.lock():
            if env.status == EnvStatus.RECLAIMED:
                # 分配期间被显式回收：立即归还资源与槽位
                self.backend.deallocate(resources)
                self._release_slot_locked(env)
                self.store.finish_creation(request_key)
                action_log(self.log, "provision-race-reclaimed", request_key, env_id)
                if env.request_key != request_key:
                    raise self._superseded_error_locked(request_key)
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
            owner_changed = env.request_key != request_key
            if owner_changed:
                # 创建成功提交时归属已在创建期间被接管：READY 环境归接管方，
                # 老创建者得到“已被接管”，绝不返回一个自己已不拥有的环境
                raise self._superseded_error_locked(request_key)
            return env

    # ------------------------------------------------------------------ retry

    def retry_failed(self, request_key: str) -> PreviewEnvironment:
        request_key = self._validate_identity(request_key, "request_key")

        with self.store.lock():
            # 同键串行化；在循环内统一复查状态，晚到线程复用已完成的重试结果
            while True:
                # 重试也先让过期即时生效（READY 过期会被回收，落到下方 reclaimed）
                self._sweep_expired_locked()
                if self.store.is_superseded(request_key):
                    raise self._superseded_error_locked(request_key)
                env = self.store.get_by_key(request_key)
                if env is None:
                    raise OrchestratorError(
                        ErrorCode.ENV_NOT_FOUND,
                        f"no environment for request_key {request_key!r}",
                        request_key=request_key,
                    )
                if env.status == EnvStatus.RECLAIMED:
                    raise OrchestratorError(
                        ErrorCode.ENV_RECLAIMED,
                        f"environment {env.env_id} has been reclaimed",
                        request_key=request_key,
                    )
                if env.status == EnvStatus.READY:
                    # 其他线程已重试成功：幂等复用
                    env.reused = True
                    action_log(self.log, "retry-reuse", request_key, env.env_id)
                    return env
                if env.status == EnvStatus.PROVISIONING or not self.store.begin_creation(
                    request_key
                ):
                    # 创建者正在分配中（或令牌被抢）：按环境状态等待后重新检查
                    # （重试期间可能有新标识接管，因此观察 env 而非只看 inflight 键）
                    self.store.wait_until_settled(env)
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
                    if env.request_key != request_key:
                        raise self._superseded_error_locked(request_key)
                    return env
                env.failure_reason = exc.reason
                env.failure_count += 1
                self._transition_locked(env, EnvStatus.FAILED, f"retry failed: {exc.reason}")
                self.store.finish_creation(request_key)
                owner_changed = env.request_key != request_key
            reject_log(
                self.log, env.request_key, ErrorCode.PROVISION_FAILED,
                exc.reason, env_id=env.env_id,
            )
            if owner_changed:
                raise self._superseded_error_locked(request_key)
            return env

        with self.store.lock():
            if env.status == EnvStatus.RECLAIMED:
                self.backend.deallocate(resources)
                self._release_slot_locked(env)
                self.store.finish_creation(request_key)
                if env.request_key != request_key:
                    raise self._superseded_error_locked(request_key)
                return env
            env.resource_identifiers = resources
            env.ready_at = self.now()
            env.expires_at = env.ready_at + env.ttl_seconds
            self._transition_locked(env, EnvStatus.READY, "retry allocated")
            self.store.finish_creation(request_key)
            if env.request_key != request_key:
                raise self._superseded_error_locked(request_key)
            return env

    # -------------------------------------------------------------------- get

    def get(self, request_key: str) -> PreviewEnvironment:
        request_key = self._validate_identity(request_key, "request_key")
        with self.store.lock():
            self._sweep_expired_locked()
            if self.store.is_superseded(request_key):
                raise self._superseded_error_locked(request_key)
            env = self.store.get_by_key(request_key)
            if env is None:
                raise OrchestratorError(
                    ErrorCode.ENV_NOT_FOUND,
                    f"no environment for request_key {request_key!r}",
                    request_key=request_key,
                )
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
            # 过期即时生效：续期前先清扫，已到期者当场回收，绝不“续命”
            self._sweep_expired_locked()
            if self.store.is_superseded(request_key):
                raise self._superseded_error_locked(request_key)
            env = self.store.get_by_key(request_key)
            if env is None:
                raise OrchestratorError(
                    ErrorCode.ENV_NOT_FOUND,
                    f"no environment for request_key {request_key!r}",
                    request_key=request_key,
                )
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
            # 兜底：sweep 与这里之间无 await（同一临界区），expires 必为未来；
            # 保留显式检查以在语义上表达“过期不可续期”。
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
            self._sweep_expired_locked()
            if self.store.is_superseded(request_key):
                raise self._superseded_error_locked(request_key)
            env = self.store.get_by_key(request_key)
            if env is None:
                raise OrchestratorError(
                    ErrorCode.ENV_NOT_FOUND,
                    f"no environment for request_key {request_key!r}",
                    request_key=request_key,
                )
            released = self._reclaim_locked(env, "explicit reclaim")
            return env, released

    # ------------------------------------------------------------ reaper (ttl)

    def reap_expired(self, now: float | None = None) -> list[str]:
        """回收所有已超过存活时间的 READY 环境，返回被回收的 env_id（幂等）。

        正常路径下过期环境在任意一次 get/renew/reclaim/provision 进入临界区时
        就已被惰性即时回收；本接口保留用于显式批量扫描（如后台巡检）。
        ``now`` 参数仅用于测试注入，不传时使用编排器时钟。
        """
        reclaimed: list[str] = []
        with self.store.lock():
            if now is None:
                # 走统一的惰性清扫（使用编排器时钟）
                return self._sweep_expired_locked()
            candidates = [
                e for e in self.store.all_envs()
                if e.status == EnvStatus.READY
                and e.expires_at is not None
                and now >= e.expires_at
            ]
            for env in candidates:
                if self._reclaim_locked(env, f"ttl expired at {env.expires_at:g}"):
                    reclaimed.append(env.env_id)
        return reclaimed

    # ------------------------------------------------------------------ stats

    def quota_snapshot(self) -> dict[str, int]:
        with self.store.lock():
            # 快照同样建立在“过期已即时回收”之上，保证计数不留空洞
            self._sweep_expired_locked()
            used = self.store.used_slots()
            by_status = Counter(e.status.value for e in self.store.all_envs())
        return {
            "quota": self.store.quota,
            "used": used,
            "available": self.store.quota - used,
            **{f"status_{k}": v for k, v in by_status.items()},
        }
