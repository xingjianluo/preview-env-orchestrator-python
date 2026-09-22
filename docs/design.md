# 预览环境编排器 — 设计说明

一个**本地可运行**的预览环境编排服务：用内存结构模拟环境资源与配额池，
提供按「项目 + 分支」申请独立预览环境的能力，并保证并发申请、失败重试与
超时回收下的环境状态始终一致、可解释。

## 1. 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 状态枚举 `EnvStatus`、合法流转表、领域对象 `PreviewEnvironment` 与 API schema |
| `app/errors.py` | 业务异常 `OrchestratorError` 与可区分错误码 `ErrorCode` |
| `app/store.py` | 线程安全存储：env 索引、配额槽位集合、同键「创建中」串行化 |
| `app/service.py` | 模拟资源后端：确定性故障注入、资源分配/释放、活跃资源跟踪 |
| `app/orchestrator.py` | 编排核心：申请 / 查询 / 续期 / 回收 / 重试 / 超时回收 |
| `app/web.py` | FastAPI 适配层：HTTP 接口与错误码 → HTTP 状态码映射 |
| `app/logging_setup.py` | 统一日志（请求键、状态流转、拒绝依据） |
| `main.py` | ASGI 入口（`uvicorn main:app`） |
| `tests/` | 存储 / 生命周期 / 配额 / 并发 / HTTP 共 42 个用例 |

## 2. 状态机

```
                 ┌──────────────────────────────────────┐
                 │                                      │
                 ▼                                      │
  (新建) ─► PROVISIONING ──分配成功──► READY ──显式回收/TTL到期──► RECLAIMED(终态)
                 │  ▲                     │
        分配失败 │  │ retry               │ 续期(renew)只更新过期时间
                 ▼  │                     │
               FAILED ───────────────────┘ 显式回收
                 │
                 └── retry：FAILED -> PROVISIONING -> READY/FAILED
```

合法流转（`ALLOWED_TRANSITIONS`）：

| From | To |
| --- | --- |
| `provisioning` | `ready`、`failed`、`reclaimed` |
| `failed` | `provisioning`（retry）、`reclaimed` |
| `ready` | `reclaimed` |
| `reclaimed` | （终态，无任何流转） |

- 每次流转都追加一条带时间戳的历史到 `env.history`，并打印
  `key=... env=... transition: A -> B` 日志。
- **创建进行中允许回收**（`provisioning -> reclaimed`）：若资源分配在回收
  之后才返回，编排器在提交结果时二次核验状态，立即释放刚拿到的资源与槽位，
  不会出现「已回收但资源还挂着」的矛盾。
- `reclaimed` 为终态：不可查询（返回 `ENV_RECLAIMED` / HTTP 410）、
  不可续期、不可重试。
- 同一 request_key 在回收后允许重新申请：生成**新的确定性递增标识**
  （`p-branch`、`p-branch-2`…），旧记录保留在按 id 的历史索引中。

## 3. 配额语义

- 配额是一个大小固定的**槽位集合**，环境创建（或重试）时占用一个槽位，
  进入 `failed` / `reclaimed` 时立即释放。
- 槽位占用/释放在同一把锁内原子完成，且都是**幂等**的：
  同一 env_id 重复占用不重复计数，重复释放安全。
- 因此以下不变式恒成立：
  `used_slots == 处于 provisioning/ready 的环境数`，
  `active_resources == 3 × 所有持有资源的环境数`（每个环境 3 个模拟资源），
  任何时刻 `used_slots ≤ quota`，绝不超发。
- 配额耗尽时请求被拒绝，错误码 `QUOTA_EXHAUSTED`（HTTP 429），**不留下
  任何半成品**：环境不登记、槽位不占用、资源不创建。
- 失败的创建会归还槽位，因此「失败 + 重试」不产生配额泄漏；若在重试前
  槽位被其他请求占满，重试得到 `QUOTA_EXHAUSTED`，环境仍保持 `failed`。

## 4. 存活时间（TTL / 租约）语义

- 申请时可带 `ttl_seconds`（默认 3600，上限 86400，必须 > 0）。
- 租约在环境**就绪时**起算：`expires_at = ready_at + ttl`；`provisioning`
  阶段不计时。
- `renew`（续期）只对 `ready` 环境生效，从**当前时刻**重新起算
  `expires_at = now + ttl`，不需要重新分配任何资源，因此不产生重复计数。
- 对已过期的环境调用 `renew`：它会被确定性地立即回收，调用收到
  `ENV_RECLAIMED`（可解释：不是「续期失败」，而是「续期时发现已死」）。
- `POST /reap` / `reap_expired()` 扫描并回收所有
  `ready 且 now ≥ expires_at` 的环境，返回被回收的 env_id 列表；
  重复扫描无副作用（幂等）。

## 5. 幂等规则

- **request_key 即幂等键**。对同一 request_key 重复提交申请：
  - 当前环境处于 `provisioning/ready/failed` → 直接复用同一对象，
    响应中 `reused=true`；绝不重复分配资源、不重复占槽。
  - 即使 request_key 相同，若 project/branch 与首次不同 → 拒绝为
    `IDENTIFIER_CONFLICT`（防止键复用串台）。
  - 当前环境已 `reclaimed` → 视为新申请，分配新标识。
- **并发同键**：存储层维护「创建中」标记 + 条件变量。同一时刻只有一个
  线程真正创建，其余线程在锁内等待，待创建结束后复用同一个环境对象。
  结论确定可复现：N 个并发同键申请 ⇒ 恰好 1 个创建者、1 份资源，
  其余全部 `reused`。
- `retry_failed` 同样按 request_key 串行化：对已就绪环境的重试是幂等
  返回；对回收后环境的重试返回 `ENV_RECLAIMED`。
- 回收（`reclaim`）幂等：重复回收返回 `released=false`，不报错、不重复释放。
- 环境标识（env_id）由 `project-branch` 确定性生成并带递增后缀，历史标识
  永不复用，因此重试/重建不会因标识冲突而产生双份资源。

## 6. 错误分类

所有业务错误均为 `OrchestratorError`，携带 `code`、`message`、`request_key`：

| 错误码 | HTTP | 触发场景 |
| --- | --- | --- |
| `INVALID_PARAMETER` | 400 | project/branch/request_key 为空白，ttl ≤ 0 或超过上限（空串先由 pydantic 以 422 拦截） |
| `IDENTIFIER_CONFLICT` | 409 | 同一 request_key 绑定到不同 project/branch；确定性标识冲突 |
| `QUOTA_EXHAUSTED` | 429 | 无空闲槽位（含并发争抢中槽位被占） |
| `PROVISION_FAILED` | 200 + `status=failed` | 模拟资源后端分配失败；环境以 failed 返回，客户端可读取后调 retry |
| `ENV_NOT_FOUND` | 404 | request_key 从未申请过 |
| `ENV_RECLAIMED` | 410 | 访问/续期/重试已回收（或续期时发现已过期被回收）的环境 |
| `NOT_READY` | 409 | 对非 ready 环境续期 |
| `INVALID_STATE_TRANSITION` | 409 | 违反状态机的非法流转（防御性） |

「配额耗尽」「标识冲突」「参数非法」三类拒绝彼此可区分，日志统一打印
`rejected code=... reason=...`。

## 7. 并发一致性设计要点

1. **单一锁模型**：所有「检查 + 改状态 + 改槽位」的复合临界区都在
   `StateStore` 的 `RLock + Condition` 内完成，避免多锁死锁。
2. **慢操作移出锁外**：资源后端的 allocate/deallocate 是模拟 I/O，
   在锁外执行；提交结果时在锁内二次核验状态（可能已被回收）。
3. **同键创建串行化**：`_inflight` 标记 + 条件变量广播，消除「同键两个
   创建者」的重复资源竞态。等待方复用前必须同时满足「环境存在 + 未回收
   + 该键已不在 inflight」，以抵御条件变量的**虚假唤醒**，绝不把
   `provisioning` 中间态当作结果返回。
4. **失败回滚顺序固定**：先释放槽位，再置 failed，最后通知等待者；
   等待者醒来看到的只会是终态（ready/failed）。若失败结果返回时环境已被
   并发回收，则只记录审计历史，不再执行 `reclaimed -> failed` 的非法流转。
5. **回收顺序固定**：先释放后端资源 → 释放槽位 → 置 reclaimed；
   重复回收短路返回。分配「成功/失败」两种结果在锁外返回后，提交时都会
   在锁内二次核验状态（可能已被回收），成功的资源立即补释放。
6. 测试通过线程池/屏障/同步事件反复制造竞争（同键 32 并发、40 路抢 3 槽、
   重试与补位同时抢槽、成功/失败分配途中被确定性回收），并断言资源数与
   槽位数严格对齐；并发用例连续 200 轮运行结论稳定可复现。

## 8. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/environments` | 申请/幂等复用环境（body: project, branch, request_key, ttl_seconds?） |
| GET | `/environments/{request_key}` | 查询（reclaimed 返回 410） |
| POST | `/environments/{request_key}/renew` | 续期（body: ttl_seconds?） |
| POST | `/environments/{request_key}/retry` | 重试失败环境 |
| DELETE | `/environments/{request_key}` | 显式回收（幂等，返回 released） |
| POST | `/reap` | 扫描并回收所有 TTL 到期环境 |
| GET | `/quota` | 配额与状态计数快照 |

## 9. 本地验证方法

```bash
# 安装依赖（uv）
uv sync

# 运行全部单测（日志会打印请求键/状态流转/拒绝依据）
uv run pytest -q

# 只看并发测试并重复多轮验证稳定性
for i in $(seq 1 30); do uv run pytest tests/test_concurrency.py -q || break; done

# 启动服务
uv run uvicorn main:app --port 8000

# 手动冒烟
curl -s -X POST localhost:8000/environments -H 'Content-Type: application/json' \
  -d '{"project":"demo","branch":"feature/login","request_key":"ci-1","ttl_seconds":600}'
curl -s localhost:8000/environments/ci-1          # 查询
curl -s -X POST localhost:8000/environments/ci-1/renew -H 'Content-Type: application/json' -d '{"ttl_seconds":3600}'
curl -s -X POST localhost:8000/reap               # 超时回收
curl -s -X DELETE localhost:8000/environments/ci-1
```

故障注入由测试通过 `ResourceBackend.fail_once(env_id)` /
`always_fail(env_id)` 驱动，可确定性地复现「创建失败 → 重试成功、
且只有一份资源」的全过程。
