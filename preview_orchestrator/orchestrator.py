"""编排核心：幂等申请、生命周期、重试、配额与 TTL 回收。"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable

from .errors import (
    EnvironmentFailedError,
    EnvironmentNotFoundError,
    IdentifierConflictError,
    InvalidParameterError,
    InvalidStateTransitionError,
    QuotaExhaustedError,
)
from .models import EnvironmentRequest, EnvironmentStatus, PreviewEnvironment
from .resources import Provisioner, ResourcePool
from .store import EnvironmentStore

logger = logging.getLogger("preview_orchestrator")


class Orchestrator:
    """预览环境编排器（线程安全）。

    所有会改变状态的决策都在 ``store.lock`` 这一把全局可重入锁内完成，
    从而把"检查标识冲突 -> 占配额 -> 建资源 -> 落状态"做成原子序列，
    并发下结论确定：不超卖、不重复计数、不泄漏。
    """

    def __init__(
        self,
        quota: int = 3,
        default_ttl_seconds: int = 300,
        resource_pool: ResourcePool | None = None,
        provisioner: Provisioner | None = None,
        store: EnvironmentStore | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if quota <= 0:
            raise InvalidParameterError("quota 必须为正整数")
        self.quota = quota
        self.default_ttl_seconds = default_ttl_seconds
        self.pool = resource_pool or ResourcePool(quota)
        self.provisioner = provisioner or Provisioner()
        self.store = store or EnvironmentStore()
        self._clock = clock or time.time
        self._id_counter = itertools.count(1)
        # 仍占用配额槽位的环境 id（环境维度，避免重复计数）
        self._slots_held: set[str] = set()
        self._reaper_thread: threading.Thread | None = None
        self._reaper_stop = threading.Event()

    # ---- 内部辅助（调用方必须已持有 store.lock） ----

    def _now(self) -> float:
        return self._clock()

    def _new_environment_id(self) -> str:
        return f"env-{next(self._id_counter):06d}"

    def _take_slot(self, env: PreviewEnvironment) -> None:
        if not self.pool.acquire():
            raise QuotaExhaustedError(
                f"配额已耗尽：已用 {self.pool.used}/{self.pool.capacity}",
                request_key=env.request_key,
            )
        self._slots_held.add(env.environment_id)

    def _release_slot(self, env: PreviewEnvironment) -> None:
        if env.environment_id in self._slots_held:
            self._slots_held.discard(env.environment_id)
            self.pool.release()

    def _reclaim_locked(self, env: PreviewEnvironment, reason: str) -> None:
        """在锁内执行回收；对已回收环境调用是安全的空操作。"""
        if env.status is EnvironmentStatus.RECLAIMED:
            return
        old = env.status
        env.transition_to(EnvironmentStatus.RECLAIMED)
        self.provisioner.destroy(env.environment_id)
        self._release_slot(env)
        self.store.mark_reclaimed(env)
        logger.info(
            "key=%s id=%s 状态流转 %s -> %s（回收依据: %s）",
            env.request_key,
            env.environment_id,
            old.value,
            env.status.value,
            reason,
        )

    def _expire_if_needed_locked(self, env: PreviewEnvironment) -> bool:
        """惰性 TTL 回收；返回该环境是否已处于 RECLAIMED。"""
        if env.status is not EnvironmentStatus.RECLAIMED and env.is_expired(self._now()):
            self._reclaim_locked(env, reason="ttl_expired")
            return True
        return env.status is EnvironmentStatus.RECLAIMED

    def request_environment(self, request: EnvironmentRequest) -> PreviewEnvironment:
        """幂等申请环境。

        语义（按顺序判断，结论确定）：

        1. 参数非法 -> InvalidParameterError
        2. 同 request_key 已有未回收环境 -> 直接复用（无论 PENDING/READY/FAILED）
        3. 同 (project, branch) 被其他 request_key 占用 -> IdentifierConflictError
        4. 无可用配额 -> QuotaExhaustedError
        5. 底层创建失败 -> 环境置 FAILED 并立即释放配额，可稍后重试
        6. 创建成功 -> READY
        """
        req = request.validated()
        key = req.request_key
        with self.store.lock:
            existing = self.store.get_including_reclaimed(key)
            if existing is not None:
                if existing.status is EnvironmentStatus.RECLAIMED:
                    # 已回收的键不可再访问，调用方必须换新请求键
                    raise EnvironmentNotFoundError(
                        f"请求键 {key!r} 对应的环境已回收，请使用新的请求键重新申请",
                        request_key=key,
                    )
                self._expire_if_needed_locked(existing)
                if existing.status is EnvironmentStatus.RECLAIMED:
                    raise EnvironmentNotFoundError(
                        f"请求键 {key!r} 对应的环境已超过存活时间被回收",
                        request_key=key,
                    )
                logger.info(
                    "key=%s id=%s 重复提交，复用已有环境（状态=%s）",
                    key,
                    existing.environment_id,
                    existing.status.value,
                )
                return existing

            # 标识冲突检查：同一项目分支只允许一个存活环境
            conflict = self.store.find_by_project_branch(req.project, req.branch)
            if conflict is not None:
                raise IdentifierConflictError(
                    f"环境标识冲突：({req.project}, {req.branch}) 已被请求键 "
                    f"{conflict.request_key!r}（环境 {conflict.environment_id}）占用",
                    request_key=key,
                )

            env = PreviewEnvironment(
                request_key=key,
                project=req.project,
                branch=req.branch,
                environment_id=self._new_environment_id(),
                status=EnvironmentStatus.PENDING,
                ttl_seconds=req.ttl_seconds,
                created_at=self._now(),
                expires_at=self._now() + req.ttl_seconds,
            )
            # 先在锁内保留配额槽位：配额不足直接拒绝且不留任何记录，
            # 之后同键重试不会错误复用一个"从未创建"的 PENDING。
            try:
                self._take_slot(env)
            except QuotaExhaustedError as exc:
                logger.warning(
                    "key=%s 拒绝依据: 配额耗尽 used=%s/%s",
                    key,
                    self.pool.used,
                    self.pool.capacity,
                )
                raise exc
            # 槽位到手再落 PENDING 记录，保证并发重复提交一定命中复用
            self.store.put_if_absent(key, env)
            logger.info(
                "key=%s id=%s 状态流转 (none) -> %s，已占配额（ttl=%ss, used=%s/%s）",
                key,
                env.environment_id,
                EnvironmentStatus.PENDING.value,
                req.ttl_seconds,
                self.pool.used,
                self.pool.capacity,
            )
            self._provision_locked(env)
            return env

    def _provision_locked(self, env: PreviewEnvironment) -> None:
        """在锁内创建资源（配额槽位已预留）；失败置 FAILED 并释放配额，不泄漏槽位。"""
        env.attempts += 1
        try:
            self.provisioner.provision(env.environment_id)
        except Exception as exc:  # 底层创建失败：状态机置 FAILED，释放配额
            env.transition_to(EnvironmentStatus.FAILED)
            env.failure_reason = str(exc)
            self._release_slot(env)
            logger.warning(
                "key=%s id=%s 状态流转 %s -> %s，失败原因: %s（第 %s 次尝试，配额已释放）",
                env.request_key,
                env.environment_id,
                EnvironmentStatus.PENDING.value,
                EnvironmentStatus.FAILED.value,
                exc,
                env.attempts,
            )
            return

        env.transition_to(EnvironmentStatus.READY)
        env.ready_at = self._now()
        env.failure_reason = None
        logger.info(
            "key=%s id=%s 状态流转 %s -> %s，配额占用 %s/%s",
            env.request_key,
            env.environment_id,
            EnvironmentStatus.PENDING.value,
            EnvironmentStatus.READY.value,
            self.pool.used,
            self.pool.capacity,
        )

    def get_environment(self, request_key: str) -> PreviewEnvironment:
        """获取环境；已回收/不存在抛 EnvironmentNotFoundError。"""
        with self.store.lock:
            env = self.store.get_including_reclaimed(request_key)
            if env is None:
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 不存在任何环境记录",
                    request_key=request_key,
                )
            if self._expire_if_needed_locked(env):
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 对应的环境已回收，不可再访问",
                    request_key=request_key,
                )
            return env

    def retry_failed(self, request_key: str) -> PreviewEnvironment:
        """重试失败的环境，不产生重复资源。

        - 只能重试 FAILED 环境；READY 直接返回，PENDING 直接返回；
        - 重试复用同一 environment_id，配额先释放后重新申请，
          因此重试成功/失败都不会重复计数或泄漏。
        """
        with self.store.lock:
            env = self.store.get_including_reclaimed(request_key)
            if env is None:
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 不存在，无法重试",
                    request_key=request_key,
                )
            if self._expire_if_needed_locked(env):
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 对应的环境已回收，不可再访问",
                    request_key=request_key,
                )
            if env.status is EnvironmentStatus.READY:
                logger.info("key=%s 重试时已就绪，直接返回", request_key)
                return env
            if env.status is EnvironmentStatus.PENDING:
                return env
            if env.status is not EnvironmentStatus.FAILED:
                raise InvalidStateTransitionError(
                    f"环境当前为 {env.status.value}，不可重试",
                    request_key=request_key,
                )

            # FAILED 时槽位已在失败路径释放；重新申请配额
            try:
                self._take_slot(env)
            except QuotaExhaustedError as exc:
                logger.warning(
                    "key=%s id=%s 重试被拒绝: 配额耗尽 used=%s/%s",
                    request_key,
                    env.environment_id,
                    self.pool.used,
                    self.pool.capacity,
                )
                raise exc
            env.transition_to(EnvironmentStatus.PENDING)
            env.failure_reason = None
            logger.info(
                "key=%s id=%s 状态流转 %s -> %s（重试，重新占用配额）",
                request_key,
                env.environment_id,
                EnvironmentStatus.FAILED.value,
                EnvironmentStatus.PENDING.value,
            )
            self._provision_locked(env)
            return env

    def renew(self, request_key: str, ttl_seconds: int | None = None) -> PreviewEnvironment:
        """续期存活时间（只允许 READY 环境续期）。"""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        if ttl <= 0:
            raise InvalidParameterError("ttl_seconds 必须为正整数", request_key=request_key)
        if ttl > 24 * 3600:
            raise InvalidParameterError(
                "ttl_seconds 不能超过 86400（24 小时）", request_key=request_key
            )
        with self.store.lock:
            env = self.store.get_including_reclaimed(request_key)
            if env is None:
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 不存在，无法续期",
                    request_key=request_key,
                )
            if self._expire_if_needed_locked(env):
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 对应的环境已回收，不可续期",
                    request_key=request_key,
                )
            if env.status is EnvironmentStatus.FAILED:
                raise EnvironmentFailedError(
                    "环境处于失败状态，不能续期，请先重试",
                    request_key=request_key,
                )
            if env.status is not EnvironmentStatus.READY:
                raise InvalidStateTransitionError(
                    f"环境当前为 {env.status.value}，不能续期",
                    request_key=request_key,
                )
            old_expiry = env.expires_at
            env.renew(ttl, now=self._now())
            logger.info(
                "key=%s id=%s 续期 ttl=%ss，过期时刻 %.3f -> %.3f",
                request_key,
                env.environment_id,
                ttl,
                old_expiry,
                env.expires_at,
            )
            return env

    def reclaim(self, request_key: str) -> PreviewEnvironment:
        """显式回收；重复回收安全（幂等）。

        已回收环境的重复回收直接返回原记录，不报错；
        不存在的键抛 EnvironmentNotFoundError。
        """
        with self.store.lock:
            env = self.store.get_including_reclaimed(request_key)
            if env is None:
                raise EnvironmentNotFoundError(
                    f"请求键 {request_key!r} 不存在，无法回收",
                    request_key=request_key,
                )
            if env.status is EnvironmentStatus.RECLAIMED:
                logger.info("key=%s 重复回收，安全跳过", request_key)
                return env
            self._reclaim_locked(env, reason="explicit_reclaim")
            return env

    def reap_expired(self) -> list[str]:
        """回收所有已超时的环境，返回被回收的 request_key 列表。"""
        reclaimed: list[str] = []
        with self.store.lock:
            for env in self.store.all():
                if env.is_expired(self._now()):
                    self._reclaim_locked(env, reason="ttl_expired_background")
                    reclaimed.append(env.request_key)
        return reclaimed

    def start_reaper(self, interval_seconds: float = 1.0) -> None:
        """启动后台 TTL 回收线程（重复调用安全）。"""
        with self.store.lock:
            if self._reaper_thread is not None and self._reaper_thread.is_alive():
                return
            self._reaper_stop.clear()
            thread = threading.Thread(
                target=self._reaper_loop,
                args=(interval_seconds,),
                name="ttl-reaper",
                daemon=True,
            )
            self._reaper_thread = thread
        thread.start()
        logger.info("TTL 后台回收线程已启动，间隔 %.3fs", interval_seconds)

    def stop_reaper(self) -> None:
        """停止后台回收线程。"""
        thread = self._reaper_thread
        if thread is None:
            return
        self._reaper_stop.set()
        thread.join(timeout=5.0)
        self._reaper_thread = None
        logger.info("TTL 后台回收线程已停止")

    def _reaper_loop(self, interval_seconds: float) -> None:
        while not self._reaper_stop.wait(interval_seconds):
            try:
                reclaimed = self.reap_expired()
                if reclaimed:
                    logger.info("后台回收超时环境: %s", reclaimed)
            except Exception:  # 回收线程绝不能因异常退出
                logger.exception("后台回收循环发生异常，已忽略")
