# 预览环境编排器（本地可运行）

用本地内存状态模拟环境资源与配额的预览环境编排服务，保证并发申请、失败重试
与超时回收下状态一致且可解释。完整设计见 [`docs/design.md`](docs/design.md)。

## 快速开始

```bash
uv sync
uv run pytest                       # 42 个单测（存储/生命周期/配额/并发/HTTP）
uv run uvicorn main:app --port 8000 # 启动服务
```

## 核心能力

- **按项目+分支申请独立环境**：`request_key` 为幂等键，重复提交复用已有环境，
  不重复创建、不重复计数；配额耗尽（429）、标识冲突（409）、参数非法（400）
  分别返回可区分的错误码。
- **完整生命周期**：`provisioning → ready / failed → reclaimed`；
  创建失败可重试且无重复资源；TTL 到期或显式回收释放资源；重复回收安全；
  已回收环境不可访问（410）。
- **并发一致性**：单锁 + 同键创建串行化 + 配额槽位原子占用，绝不超发、
  无资源泄漏；冲突结论确定可复现（含同键 32 并发、40 路抢 3 槽等测试）。
- **可解释日志**：每个请求打印请求键、状态流转与拒绝依据。

## 主要接口

`POST /environments`、`GET /environments/{key}`、
`POST /environments/{key}/renew`、`POST /environments/{key}/retry`、
`DELETE /environments/{key}`、`POST /reap`、`GET /quota`。
