# 预览环境编排器 — 设计说明

一个**本地可运行**的预览环境编排服务：用内存结构模拟环境资源与配额池，
提供按「项目 + 分支」申请独立预览环境的能力，并保证并发申请、失败重试与
超时回收下的环境状态始终一致、可解释。

## 1. 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 状态枚举 `EnvStatus`、合法流转表、领域对象 `PreviewEnvironment` 与 API schema |
| `app/errors.py` | 业务异常 `OrchestratorError` 与可区分错误码 `ErrorCode` |
| `app/store.py` | 线程安全存储：env 索引、分支单活索引、标识轮换墓碑、配额槽位集合、同键「创建中」串行化 |
| `app/service.py` | 模拟资源后端：确定性故障注入、资源分配/释放、活跃资源跟踪 |
| `app/orchestrator.py` | 编排核心：申请 / 查询 / 续期 / 回收 / 重试 / 超时回收 / 标识轮换接管 |
| `app/web.py` | FastAPI 适配层：HTTP 接口与错误码 → HTTP 状态码映射 |
| `app/logging_setup.py` | 统一日志（请求键、状态流转、拒绝依据） |
| `main.py` | ASGI 入口（`uvicorn main:app`） |
| `tests/` | 存储 / 生命周期 / 配额 / 并发 / 标识轮换接管 / HTTP 用例 |

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

## 4. 存活时间（TTL / 租约）语义与到期实时生效

- 申请时可带 `ttl_seconds`（默认 3600，上限 86400，必须 > 0）。
- 租约在环境**就绪时**起算：`expires_at = ready_at + ttl`；`provisioning`
  阶段不计时。
- `renew`（续期）只对 `ready` 环境生效，从**当前时刻**重新起算
  `expires_at = now + ttl`，不需要重新分配任何资源，因此不产生重复计数。
- **到期实时生效，不依赖人工/定时触发清理**：编排器把“过期清扫”内联进每一个
  会读取或裁决状态的临界区——`provision` / `get` / `renew` / `reclaim` /
  `retry_failed` / `quota_snapshot` 进入锁内后，第一件事都是
  `_sweep_expired_locked()`。因此只要 `now >= expires_at`：
  - 查询：立刻得到 `ENV_RECLAIMED`（HTTP 410），环境当场转为 `reclaimed`；
  - 续期：不允许给已过期环境“续命”，当场回收并返回 `ENV_RECLAIMED`；
  - 接管判定：过期环境先被回收、退出分支单活索引，新申请只能**新建**，
    **已过期的环境不可能被接管“救活”**；
  - 容量：后端资源与配额槽位在同一临界区内立即释放，后面的申请/接管马上可用，
    不会出现“环境已死但容量还被占着”的空洞。
- `POST /reap` / `reap_expired()` 仍保留，作为显式批量扫描（如后台巡检）；
  正常运行时即使从不调用它，过期也会在任意一次访问时自行收敛。重复扫描幂等。
- 注意 `provisioning` 阶段不持有 TTL 计时，`failed` 环境不持有租约与槽位，
  因此它们不受 TTL 到期清扫影响。

## 5. 标识轮换与接管（同项目+分支单活）

### 5.1 背景与取舍

CI 每次重跑都会带来一个新的申请标识（`request_key`），但分支其实没变。
若把每个新标识都当成独立申请，同一分支会并存多份存活环境，既浪费配额也让
“当前预览”失去唯一指向。本编排器选择的语义是：

> **同一 `(project, branch)` 下，任意时刻最多只有一份存活（未回收）环境。
> 新标识到来时，默认“接管”同一份环境，而不是并存第二份。**

这是需求允许的两种确定策略（接管复用 / 以可区分理由明确拒绝）中选取的一种：
接管对 CI 重跑更友好（新流水线号拿到的就是同一份预览），拒绝路径仍然保留
给“老标识”——它在失去归属后得到一个**专门的、与其他拒绝原因可区分**的错误码。

### 5.2 接管的确定结论

存储层在键索引之外新增两个结构：

- `_live_by_branch[(project, branch)] -> env`：分支单活索引；
- `_superseded[old_key] -> (env_id, owner_key)`：被换走老键的“墓碑”。

`provision` 在同一把锁内按下述顺序裁决，结论只取决于锁内当前状态，因此重复
提交与并发提交的结论都确定可复现：

1. 先做过期清扫（见 §4）；
2. 若本键已在 `_superseded` 中 → 直接拒绝 `IDENTIFIER_SUPERSEDED`，该结论
   永久一致（墓碑不会让老键“复活”）；
3. 本键已有环境：身份不符 → `IDENTIFIER_CONFLICT`；`reclaimed` → 走新建；
   `provisioning`/创建中 → 等待 settle 后重查；`ready/failed` → 幂等复用；
4. 否则查分支单活：存在存活环境 → **接管**（`rebind_key` 把环境的属主键换成
   新键，老键写入 `_superseded`），复用同一 env_id 与同一批资源，不新增槽位；
5. 分支无存活环境 → 才真正新建、占槽、分配资源。

接管时的存活时间：

- 被接管环境已是 `ready`：**租约从接管时刻重新起算**，
  `ready_at = now`、`expires_at = now + 本次申请的 ttl`（不是在旧到期时间上
  叠加，也不保留旧 TTL 余量）；
- 被接管环境仍在 `provisioning`：接管方等待创建 settle，环境就绪时按本次申请
  的 TTL 起算；
- 被接管环境是 `failed`：接管同一条 failed 记录（仍受单活约束），由新属主
  调 `retry` 重试，成功后才持有资源与租约。

### 5.3 老标识的结果：清楚且一致

接管完成后，老标识与该环境彻底解绑（不在键索引中），但保留一条墓碑。此后对
老键的**查询 / 续期 / 显式回收 / 重新申请 / 重试**一律返回
`IDENTIFIER_SUPERSEDED`（HTTP 409），错误消息带 env_id 与当前属主键，
例如：

```
[IDENTIFIER_SUPERSEDED] request_key 'ci-1' no longer owns environment 'proj-feat';
ownership was taken over by 'ci-2'
```

因此老标识“再查”不会一会儿有效、一会儿失效；也绝不会出现两个标识都声称
自己指向当前环境的情况——键索引在任意时刻只允许一个属主键指向该 env。

### 5.4 并发语义：只生效一次

- **同一新标识并发提交**：沿用同键创建串行化（`_inflight` + 条件变量），
  只有一次真正生效，其余全部等待后复用同一对象（`reused=true`），底层只分配
  一批资源。
- **多个不同新标识并发轮换**：它们在锁内串行地依次换绑，最终恰好有一个键是
  存活属主，其余键全部落入 `_superseded`。虽然过程上发生过链式换绑，但终态
  满足“一份环境、一个属主、一批资源”，且每个失败者的结论都确定一致。
- **创建途中被接管**：接管发生在别人 `provisioning` 期间时，环境先换绑给新
  键，接管方等待创建 settle；提交结果时以“当前属主”为准——若创建者的老键
  已被换走，创建者得到 `IDENTIFIER_SUPERSEDED`，READY/FAILED 环境归接管方。
- **接管与显式回收竞争**：两者在同一锁内裁决，谁先提交完全由锁顺序决定且
  可复现。环境一旦进入 `reclaimed` 即为终态，任何还在路上的接管/重试都不能
  把它重新激活或重新占回容量；需要时只能以新的递增 env_id 新建。

### 5.5 与容量、存活时间的关系

- 接管**不新增配额消耗**：复用被接管环境已占用的那个槽位与那批资源。
- 新建（含同键回收后重建、过期后重建、接管尸体不可能而走的新建）才占新槽，
  配额满时得到 `QUOTA_EXHAUSTED`。
- 不变式恒成立：`used_slots == provisioning/ready 环境数`，
  `active_resources == 3 × 持有资源的环境数`，每个 `(project, branch)` 至多
  一份存活环境，槽位集合与 `slot_held` 标记严格一致（无“占着却没有环境”的
  空洞）。`tests/test_takeover.py` 中的 `_assert_invariants` 在每一步后对账。

### 5.6 兼容范围

- request_key 的幂等复用语义不变：同键重复提交仍复用同一环境。
- 显式回收（非接管换绑）不会给键写墓碑：同键回收后重新申请仍是“新建”，
  这与历史行为一致（`IDENTIFIER_SUPERSEDED` 只在“被别的键接管”时出现）。
- 错误码、状态机、HTTP 路由均向后兼容，仅新增一个错误码与一组索引。

## 6. 幂等规则

- **request_key 即幂等键**。对同一 request_key 重复提交申请：
  - 当前环境处于 `provisioning/ready/failed` → 直接复用同一对象，
    响应中 `reused=true`；绝不重复分配资源、不重复占槽。
  - 即使 request_key 相同，若 project/branch 与首次不同 → 拒绝为
    `IDENTIFIER_CONFLICT`（防止键复用串台）。
  - 当前环境已 `reclaimed` → 视为新申请，分配新标识。
- **不同 request_key、同一 project+branch**：触发接管而非新建，详见 §5。
- **并发同键**：存储层维护「创建中」标记 + 条件变量。同一时刻只有一个
  线程真正创建，其余线程在锁内等待，待创建结束后复用同一个环境对象。
  结论确定可复现：N 个并发同键申请 ⇒ 恰好 1 个创建者、1 份资源，
  其余全部 `reused`。
- `retry_failed` 同样按 request_key 串行化：对已就绪环境的重试是幂等
  返回；对回收后环境的重试返回 `ENV_RECLAIMED`。
- 回收（`reclaim`）幂等：重复回收返回 `released=false`，不报错、不重复释放。
- 环境标识（env_id）由 `project-branch` 确定性生成并带递增后缀，历史标识
  永不复用，因此重试/重建不会因标识冲突而产生双份资源。

## 7. 错误分类

所有业务错误均为 `OrchestratorError`，携带 `code`、`message`、`request_key`：

| 错误码 | HTTP | 触发场景 |
| --- | --- | --- |
| `INVALID_PARAMETER` | 400 | project/branch/request_key 为空白，ttl ≤ 0 或超过上限（空串先由 pydantic 以 422 拦截） |
| `IDENTIFIER_CONFLICT` | 409 | 同一 request_key 绑定到不同 project/branch；确定性标识冲突 |
| `IDENTIFIER_SUPERSEDED` | 409 | 该 request_key 曾持有某环境，但归属已被更新的标识**接管**；对其查询/续期/回收/重试/再申请一律返回此码 |
| `QUOTA_EXHAUSTED` | 429 | 无空闲槽位（含并发争抢中槽位被占）。接管复用不占新槽，不会触发此码 |
| `PROVISION_FAILED` | 200 + `status=failed` | 模拟资源后端分配失败；环境以 failed 返回，客户端可读取后调 retry |
| `ENV_NOT_FOUND` | 404 | request_key 从未申请过 |
| `ENV_RECLAIMED` | 410 | 访问/续期/重试已回收（含访问时发现已过期被即时回收）的环境 |
| `NOT_READY` | 409 | 对非 ready 环境续期 |
| `INVALID_STATE_TRANSITION` | 409 | 违反状态机的非法流转（防御性） |

`IDENTIFIER_SUPERSEDED`（我被别人接管了）、`ENV_RECLAIMED`（环境已死）、
`IDENTIFIER_CONFLICT`（同键串台）、`QUOTA_EXHAUSTED`（没容量）四类拒绝彼此
可区分；日志统一打印 `rejected code=... reason=...`，接管动作打印
`action=takeover` / `action=takeover-lease-restart`。

## 8. 并发一致性设计要点

1. **单一锁模型**：所有「检查 + 改状态 + 改槽位 + 改属主/单活索引」的复合
   临界区都在 `StateStore` 的 `RLock + Condition` 内完成，避免多锁死锁。
2. **慢操作移出锁外**：资源后端的 allocate/deallocate 是模拟 I/O，
   在锁外执行；提交结果时在锁内二次核验状态（可能已被回收、可能已被接管）。
3. **同键创建串行化**：`_inflight` 标记 + 条件变量广播，消除「同键两个
   创建者」的重复资源竞态。等待方直接观察环境状态（`wait_until_settled`），
   而不是只看 inflight 键——因为创建途中环境可能已被新标识换绑；以此抵御
   条件变量的**虚假唤醒**，绝不把 `provisioning` 中间态当作结果返回。
4. **失败回滚顺序固定**：先释放槽位，再置 failed，最后通知等待者；
   等待者醒来看到的只会是终态（ready/failed/reclaimed）。若失败结果返回时
   环境已被并发回收，则只记录审计历史，不再执行 `reclaimed -> failed` 的
   非法流转；若期间属主被接管，则失败结论归接管方，老创建者得到 superseded。
5. **回收顺序固定**：先释放后端资源 → 释放槽位 → 摘分支单活索引 →
   置 reclaimed；重复回收短路返回。分配「成功/失败」两种结果在锁外返回后，
   提交时都会在锁内二次核验状态（可能已被回收/接管），成功的资源立即补释放。
6. **分支单活 + 接管换绑原子化**：`rebind_key` 在同一临界区内完成
   “环境改属主 + 键索引迁移 + 老键墓碑”，因此任意时刻只有一个键能指向该
   环境；接管与回收竞争时，先拿到锁者生效，终态唯一且可复现。
7. **过期清扫内联**：所有裁决入口先 `_sweep_expired_locked()`，过期回收与
   本次决策在同一临界区内完成，保证“过期即不可访问、容量即释放”，且清扫与
   决策之间不可能插入其它观察到“存活但已过期”的操作。
8. 测试通过线程池/屏障/同步事件/手动锁顺序反复制造竞争（同键 32 并发、
   40 路抢 3 槽、32 个不同新标识并发轮换、provisioning 途中确定性接管、
   接管与回收两种固定先后顺序、成功/失败分配途中被确定性回收），并通过
   `_assert_invariants` 对账资源数、槽位数、分支单活、墓碑与索引；并发用例
   连续多轮运行结论稳定可复现。

## 9. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/environments` | 申请/幂等复用/**同分支换标识接管**（body: project, branch, request_key, ttl_seconds?） |
| GET | `/environments/{request_key}` | 查询（reclaimed 返回 410；被接管的老键返回 409 IDENTIFIER_SUPERSEDED） |
| POST | `/environments/{request_key}/renew` | 续期（body: ttl_seconds?） |
| POST | `/environments/{request_key}/retry` | 重试失败环境 |
| DELETE | `/environments/{request_key}` | 显式回收（幂等，返回 released） |
| POST | `/reap` | 显式扫描并回收所有 TTL 到期环境（正常情况下访问时已惰性回收） |
| GET | `/quota` | 配额与状态计数快照（先做过期清扫） |

## 10. 本地验证方法

```bash
# 安装依赖（uv）
uv sync

# 运行全部单测（日志会打印请求键/状态流转/接管/拒绝依据）
uv run pytest -q

# 只看并发与接管测试并重复多轮验证稳定性
for i in $(seq 1 30); do \
  uv run pytest tests/test_concurrency.py tests/test_takeover.py -q || break; \
done

# 启动服务
uv run uvicorn main:app --port 8000

# 手动冒烟：申请 -> 同分支换新标识（接管）-> 老键得到 409 -> 新键续期/回收
curl -s -X POST localhost:8000/environments -H 'Content-Type: application/json' \
  -d '{"project":"demo","branch":"feature/login","request_key":"ci-1","ttl_seconds":600}'
curl -s -X POST localhost:8000/environments -H 'Content-Type: application/json' \
  -d '{"project":"demo","branch":"feature/login","request_key":"ci-2"}'   # 接管，同一 env_id
curl -s localhost:8000/environments/ci-1                                 # 409 IDENTIFIER_SUPERSEDED
curl -s localhost:8000/environments/ci-2                                 # 200，当前属主
curl -s -X POST localhost:8000/environments/ci-2/renew -H 'Content-Type: application/json' -d '{"ttl_seconds":3600}'
curl -s -X DELETE localhost:8000/environments/ci-2

# 到期实时生效：把时钟/TTL 调到过期后，直接 GET（不调用 /reap）即返回 410，
# 且 GET /quota 可见 used/资源已立即释放（该路径由 tests/test_takeover.py 覆盖）
```

故障注入由测试通过 `ResourceBackend.fail_once(env_id)` /
`always_fail(env_id)` 驱动，可确定性地复现「创建失败 → 重试成功、
且只有一份资源」的全过程；标识轮换与到期实时生效的完整断言见
`tests/test_takeover.py`。
