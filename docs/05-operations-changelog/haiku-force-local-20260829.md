# haiku 强制本地路由（数据保密决策，2026-08-29）

## 决策
`claude-haiku-4-5` 路由 `behavior` 从 `prefer` 改为 **`force`**（+ `route_bias: prefer_local`）→ **恒走本地 Ornith，永不云端**。

## 动机
数据保密：haiku 档请求（轻量任务）全部留在本地，避免敏感内容经云端提供商处理。confidentiality 优先于成本/延迟（此前已通过阈值 2.0=160K chars 尽量本地，但大上下文仍会切云端）。

## 实现
`configs/models.json` → routes.`claude-haiku-4-5`：`"behavior": "force"`。

SmartRouter 语义（pipeline.py:798-820）：`behavior=force` + `route_bias=prefer_local` → `_route_model_bias=prefer_local` → 直接 `return "local", "model_forced_local(...)"`，**早于阈值/内存/会话粘性判断**。唯一覆盖途径 = 请求头 `X-Proxy-Route-To: cloud`（客户端默认不发送）。

## 实测验证
- 180K chars 长上下文 haiku 请求 → `[smart_router] local (model_forced_local(claude-haiku-4-5))` → `Forwarding to http://127.0.0.1:8081` → 200 ✅
- 任意上下文均本地；大上下文由 Ornith 承受（343K chars 实测 24.7GB < cap 28.1GB；decode 随上下文衰减）

## 影响
- **sonnet/opus 不受影响**：仍按阈值路由云端（零边际链 glm-5.3-flash-cn → kimi → deepseek 兜底）。
- haiku 云端链 `[glm-5.3-flash-cn, glm-5.3-flash, kimi-for-coding-highspeed, deepseek-v4-flash]` 保留仅作 header 覆盖兜底。
- 本地后端不可用时 haiku 会失败（无云端降级）——保密性优先于可用性。

## 相关
- 路由表同步更新：`CLAUDE.md`、`docs/02-architecture-design/opencode-anthropic-routing-decision-20260829.md` §四/§七。
- 单测 `test_smart_router.py::test_haiku_force_always_local`（三种上下文均断言 `model_forced_local`）。

## 补充（同日）：优先级队列 + force-local 巨请求保护

haiku 强制本地后，opencode 长会话（256K chars）全走本地 Ornith → `MAX_CONCURRENT=1` 下小请求被长生成饿死（实测 0-0.5 tok/s）。处置：

1. **开启优先级队列** `PROXY_QUEUE_ENABLED=true`（ornith-oq4e.conf）：interactive 桶（<16K chars）优先于 standard/large——多请求排队时小请求优先拿 worker 槽。**队列 = 单 worker 优先级调度，不增加并发、不抢占在途生成**（小请求仍等长生成完成）。
2. **force-local 巨请求保护**：队列 `HUGE_ACTION=cloud` 默认会把 ≥350K chars 请求强制云端，早于 SmartRouter——`decide_huge_action` 新增 `force_local` 参数：force-local 模型（haiku）巨请求**永不云端**（≤400K 本地承载 / >400K 413 拒绝）；sonnet/opus 巨请求仍按默认 cloud。
3. **并发决策**：保持 `PROXY_MAX_CONCURRENT=1` + `--max-num-seqs 1`（不升 2 并发）——Metal 时间片半速 + 内存余量考量，保守优先。
4. 测试：`test_queue_manager.py::TestDecideHugeActionForceLocal`（4 用例）；unit 1205 全过。