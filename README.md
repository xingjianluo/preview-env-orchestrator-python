# 本地预览环境编排服务（Preview Environment Orchestrator）

一个完全在本地运行的预览环境编排器：用**内存状态 + 模拟资源层**代表真实云资源与配额，
提供按「项目 + 分支」申请独立预览环境的能力，并保证在**并发申请、失败重试、超时回收**下
环境状态始终一致、可解释、可复现。

- 运行时：Python 3.14 + FastAPI（`uvicorn`）
- 无外部依赖：资源池、配额、环境存储全部在进程内模拟
- 线程安全：所有状态变更在同一把可重入锁内原子完成

---

## 1. 快速开始

```bash
# 安装依赖（uv）
uv sync

# 启动 HTTP 服务
uv run uvicorn main:app --reload

# 运行全部单测（含并发/幂等/重试/TTL/配额场景，并打印状态流转日志）
uv run pytest -q -s

# 只跑某一组
uv run pytest tests/test_concurrency.py -q
```

---

## 2. 状态机（生命周期）

环境对象 `PreviewEnvironment.status` 有 4 个状态：

| 状态 | 含义 | 是否占用配额 |
| --- | --- | --- |
| `pending` | 申请中，正在创建底层资源 | 是（槽位已预留） |
| `ready` | 就绪，可访问、可续期 | 是 |
| `failed` | 创建失败，可重试 | 否（失败即释放） |
| `reclaimed` | 已回收（TTL 超时或显式回收），终态 | 否 |

合法流转（非法流转抛 `invalid_state_transition`）：

```
            ┌──────────────────────────────┐
            ▼                               │
(none) ─► pending ──创建成功──► ready ──回收─┴─► reclaimed(终态)
            │  ▲                  ▲
         失败│  │重试成功          │续期只刷新 expires_at，不改状态
            ▼  │
          failed ───重试──────────► pending
            │
          回收└──────────────────────────────► reclaimed
```

- `pending → ready`：底层资源创建成功。
- `pending → failed`：创建抛错；**同一事务内释放配额**，不留底层资源。
- `failed → pending`：显式重试，复用**同一个 environment_id**，重新预留配额再创建。
- `* → reclaimed`：显式回收或 TTL 超时；销毁底层资源、释放配额、让出
  `(project, branch)` 标识。`reclaimed` 是终态，不可再访问。
- 回收对任意存活状态都合法（包括 `pending`、`failed`）。

### 可解释性：日志

所有关键动作都通过 logger `preview_orchestrator` 打印，单测中可直接看到
**请求键（key）、状态流转、拒绝依据**，例如：

```
key=k1 id=env-000001 状态流转 (none) -> pending，已占配额（ttl=60s, used=1/2）
key=k1 id=env-000001 状态流转 pending -> ready，配额占用 1/2
key=k1 id=env-000001 重复提交，复用已有环境（状态=ready）
key=k4 拒绝依据: 配额耗尽 used=2/2
key=k1 id=env-000001 状态流转 ready -> reclaimed（回收依据: ttl_expired）
```

---

## 3. 配额语义（Quota）

- 构造参数 `quota` 决定资源池容量（`ResourcePool`，默认 3）。
- 配额在**锁内**预留：`检查标识冲突 → acquire 槽位 → 落 PENDING 记录 → 创建资源`
  是一个原子序列，因此并发突发下：
  - 成功者数量**恰好**等于配额，其余全部得到 `quota_exhausted`；
  - `pool.used` 永远等于底层存活资源数（`provisioner.active_ids`），
    不超卖、不重复计数、不泄漏；
  - 被配额拒绝的请求**不落任何记录**，之后可用同一 request_key 重试成功。
- 创建失败立即释放槽位；重试成功才重新占用。
- 回收（显式/TTL）释放槽位。

---

## 4. 存活时间语义（TTL）

- 每个环境带 `created_at / expires_at = created_at + ttl_seconds`，TTL 取值
  `1..86400` 秒，默认 300 秒。
- 过期回收有两条路径，结果一致：
  1. **惰性回收**：`get / retry / renew / request(同键)` 访问时发现过期，立即回收；
  2. **后台回收**：`start_reaper(interval_seconds)` 启动守护线程周期性 `reap_expired()`。
- **续期（renew）**：仅 `ready` 环境可续期，`expires_at = 当前时间 + 新 ttl`；
  在过期时刻之前续期即可阻止回收。
- 回收后 `(project, branch)` 标识被释放，可用**新的 request_key** 重新申请；
  旧 request_key 永远返回 `environment_not_found`，不会复活或产生第二份资源。

---

## 5. 幂等规则（Idempotency）

- 请求键 `request_key` 是幂等主键；缺省为 `project:branch`。
- **同一 request_key 的重复提交**：只要环境未回收，无论它处于
  `pending / ready / failed`，都返回同一对象（同一 `environment_id`），
  不会第二次创建资源、不会重新计数，也不会改写原 TTL。
- **回收幂等**：对同一键重复 `reclaim` 安全，第二次是空操作，仍返回该
  `reclaimed` 记录，不报错、不重复释放。
- **重试不产生重复资源**：`retry_failed` 只能作用于 `failed` 环境，复用原
  `environment_id`；并发重试时只有第一个线程执行实际创建，其余线程直接
  看到 `ready`。
- 注意区分：同一 `(project, branch)` 被**不同 request_key** 申请属于
  `identifier_conflict`，不是幂等复用。
- 已回收的 request_key 不可复用（返回 `environment_not_found`），
  这保证「一个请求键的历史结论」永远不会被悄悄改写。

---

## 6. 错误分类（拒绝原因可区分）

所有业务错误继承 `OrchestratorError`，带稳定的 `code` 字段与 `request_key`：

| code | 异常 | 触发场景 | HTTP |
| --- | --- | --- | --- |
| `invalid_parameter` | `InvalidParameterError` | project/branch 为空或含非法字符、ttl 非正或 > 86400、quota 非正 | 400 |
| `identifier_conflict` | `IdentifierConflictError` | 同一 `(project, branch)` 已被其他 request_key 占用 | 409 |
| `quota_exhausted` | `QuotaExhaustedError` | 申请或重试时无可用配额槽位 | 429 |
| `environment_not_found` | `EnvironmentNotFoundError` | 键不存在，或环境已回收/已超时仍被访问 | 404 |
| `environment_failed` | `EnvironmentFailedError` | 对 `failed` 环境执行续期等不允许的操作 | 409 |
| `invalid_state_transition` | `InvalidStateTransitionError` | 状态机不允许的流转 | 409 |

判定优先级（`request_environment`）：参数校验 → 同键复用 → 标识冲突 →
配额预留 → 创建（成功 ready / 失败 failed）。该顺序固定，因此同一输入下
结论确定、可复现。

---

## 7. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/environments` | 申请环境（幂等）。body: `project, branch, request_key?, ttl_seconds?` |
| GET | `/environments/{request_key}` | 查询（触发惰性 TTL 检查） |
| POST | `/environments/{request_key}/retry` | 重试失败环境 |
| POST | `/environments/{request_key}/renew` | 续期，body: `{"ttl_seconds": 120}` |
| DELETE | `/environments/{request_key}` | 显式回收（幂等） |
| POST | `/reap-expired` | 立即回收所有超时环境，返回键列表 |
| GET | `/quota` | 查看 `capacity / used / available / active_resources` |

快速手动验证：

```bash
curl -s -X POST localhost:8000/environments \
  -H 'content-type: application/json' \
  -d '{"project":"demo","branch":"feat/login","ttl_seconds":60}'
curl -s localhost:8000/quota
curl -s -X DELETE localhost:8000/environments/demo:feat/login
```

---

## 8. 编程式用法

```python
from preview_orchestrator import Orchestrator, EnvironmentRequest
from preview_orchestrator.resources import Provisioner

provisioner = Provisioner()
orch = Orchestrator(quota=3, provisioner=provisioner)

env = orch.request_environment(EnvironmentRequest("demo", "feat/x", ttl_seconds=60, request_key="k1"))
# 可注入故障，验证失败与重试：
# provisioner.always_fail_ids.add(env.environment_id)
orch.retry_failed("k1")
orch.renew("k1", 120)
orch.reclaim("k1")
orch.reap_expired()
orch.start_reaper(interval_seconds=1.0)  # 可选后台回收
```

`Provisioner` 测试钩子：

- `always_fail_ids: set[str]`：指定环境确定性创建失败；
- `fail_next_n: int`：接下来 N 次创建瞬时失败（用于重试链路）；
- `provision_delay: float`：创建时持锁延时（用于构造并发窗口）；
- `active_ids`：当前底层存活资源集合，用于断言**无资源泄漏**。

---

## 9. 测试覆盖与本地验证

测试位于 `tests/`，共 6 组（190+ 用例），并发场景以参数化方式重复多轮：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_lifecycle.py` | 参数校验、状态流转、标识冲突、回收不可访问、标识释放、续期 |
| `test_idempotency_and_retry.py` | 同键重复提交复用、重复回收安全、失败释放配额、重试复用同一 id、配额不足时重试 |
| `test_quota.py` | 配额耗尽拒绝、三类拒绝原因可区分、回收释放配额、失败不占配额、被拒不落记录 |
| `test_ttl.py` | 惰性过期、批量 reap、续期防过期、过期释放标识、后台 reaper、旧键拒绝 |
| `test_concurrency.py` | 突发申请不超卖、同键并发只建一次、标识冲突唯一赢家、并发重试、续期/回收竞争、混合压测无泄漏 |
| `test_api.py` | HTTP 状态码与错误 code 映射、端到端生命周期 |

运行：

```bash
uv run pytest -q -s          # -s 可查看 key/状态流转/拒绝依据日志
uv run pytest -q -k concurrent
```

并发测试中的关键不变量（每轮都断言）：

1. `pool.used == len(provisioner.active_ids)`，且 `0 <= used <= quota`；
2. 突发 N 个不同申请：成功数恰为 `quota`，其余错误全部是 `quota_exhausted`；
3. 同键并发 N 次：只创建 1 个资源、占用 1 个槽位，返回同一 id；
4. 同项目分支不同键并发：恰有 1 个赢家，其余全部 `identifier_conflict`；
5. 全部回收后 `used == 0` 且 `active_ids` 为空（无泄漏）。

---

## 10. 代码结构

```
preview_orchestrator/
├── errors.py        # 错误分类（稳定 code）
├── models.py        # EnvironmentStatus 状态机、EnvironmentRequest、PreviewEnvironment
├── resources.py     # ResourcePool 配额层 + Provisioner 模拟创建/销毁
├── store.py         # 以 request_key 为主键、(project,branch) 为唯一索引的存储
├── orchestrator.py  # 编排核心：幂等/重试/续期/回收/TTL reaper
└── api.py           # FastAPI 路由与错误 -> HTTP 映射
main.py              # uvicorn 入口
tests/               # 单测与并发测试
```

一致性设计要点：所有变更路径都在 `EnvironmentStore.lock`（可重入锁）内执行，
把「检查—预留—创建—落状态」串行化，从而以简单的临界区换取确定且可复现的结论。
