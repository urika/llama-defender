# 多云模型目录（Model Catalog）全量落地 — Phase A-D + llama-defender R8-R12

> 日期：2026-08-15
> 关联设计：[multi-cloud-model-catalog-design-20260815.md](../02-architecture-design/multi-cloud-model-catalog-design-20260815.md)
> 关联需求：[llama-defender-integration-requirements.md](../llama-defender-integration-requirements.md)（R1-R12 全部交付）
> 提交范围：`e350e33 → df6aeb7` 共 11 个 commit（另含用户前期工作 `b9b3074`）
> 一句话：**新增一个云端模型从"改 5-6 处代码+发版"变为"models.json 加条目 + secret 配 key + reload，零代码"；三家云提供商（deepseek/kimi/zhipu）全部真实调用验证通过。**

---

## 1. 交付总览

| Phase | Commit | 内容 | 行为影响 |
|---|---|---|---|
| **A** 模型目录 registry | `e350e33` | `model_registry.py`(563行) + `configs/models.json` + proxy_state/reload_config 集成 + 兼容合成 | **零变化**（无目录文件时合成等价目录；SIGHUP 重建 preferences 顺带修复 import 时捕获 PROXY_CLOUD_MODEL 的陈旧值缺陷） |
| **B** 多提供商分发 | `b35b608` | 按模型解析 provider 凭证/分商信号量/分商熔断；`fallback_chain` 跨商降级（含云端 URLError 修复——原先直接上抛 503）；按模型目录价格计费 + 全局/分商预算双上限；R8 契约头 `X-Proxy-Route-*` 四件套直接切换（旧 `X-Route-*` 零别名清理） | 单提供商场景行为等价 |
| 端点校准 | `8f81934`/`3677913` | zhipu/kimi 官方文档核实+真实冒烟；**发现 Kimi 双体系**（api.kimi.com Coding 订阅 vs api.moonshot.cn 开放平台，互不相通）；kimi 模型名/能力实测自 `/coding/v1/models`（k3=1048576 ctx, thinking only, vision）；moonshot provider 冗余移除 | 目录事实层修正 |
| **C** R9-R12 端点 | `c125961` | R9 `GET /api/route/policies`（脱敏+`catalog_hash` 漂移检测）、R10 `/v1/models` 能力元数据、R11 `/api/status` `route_config`、R12 `POST /admin/reload`（空 body 合法/幂等）、`manage.sh models`/`models-validate` | 纯增量端点 |
| 价格补录 | `943816d` | 官方口径：deepseek-v4-pro 修正 ¥3/6（原 2/8 为过时近似）；kimi 订阅边际 0；glm 按量参考 ¥10/31 | 成本核算准确化 |
| **D** Anthropic 协议分发 | `b32bbb4` | provider `protocol(openai\|anthropic)` + 双端点双 key（`anthropic_key_env`）；`_do_dispatch_anthropic` 复用已测转换链回转请求体；SSE 原样透传/非流式直返+`proxy_route`；OpenAI 协议客户端自动跳过 anthropic 候选 | **Z.ai Coding Plan 订阅额度可被代理路由消耗** |
| 路由决策 | `df6aeb7` | `claude-opus-4-7 → ["glm-5.3", "deepseek-v4-pro"]`：订阅主路径（边际 0）+ 按量备援 | opus 档常态云费用归零 |

## 2. 实测发现并当场修复的缺陷（均有单测锚定）

| 缺陷 | 发现方式 | 修复 |
|---|---|---|
| kimi thinking-only 模型仅接受 `temperature:1`（默认 0.7 → 云端 400） | R8 实测审计真实调用 | `force_temperature` quirk（`a444573`） |
| 目录 deepseek 字面 base_url 压过 conf 的 `PROXY_CLOUD_BASE_URL` 覆盖（中转场景回归） | Phase B 单测 | provider 改 `base_url_env`/`concurrent_env` 引用 |
| manage.sh source secret 未导出，分商 key 启动态不可见 | 生产重启验证 | `set -a` 包裹（`a34bbea`） |
| R8 响应头与契约命名不符（`X-Route-*` vs 契约 `X-Proxy-Route-*`，agent_go fail-open 静默落空） | 需求稿 v2 对照 | 直接切换+全量清理旧名 |
| registry 无 getter 时 `$env` 解析为空（CLI 场景） | `manage.sh models` 联调 | 默认回退读环境 |

## 3. 当前路由/成本拓扑（生产实测口径）

```
claude-opus-4-7   ──prefer_cloud──▶ glm-5.3      [Z.ai Coding Plan 订阅, anthropic 协议, 边际¥0]
                                    └─429 备援─▶ deepseek-v4-pro [按量 ¥3/6]
claude-sonnet-4-6 ──auto(90K阈值)─▶ deepseek-v4-flash [按量 ¥0.5/1.5] / 超限转云
claude-haiku-4-5  ──prefer_local──▶ 本地 Qwen3.6-35B（超限转 deepseek-v4-flash）
k3/glm 直选       ──目录模型名────▶ kimi / zhipu 订阅通道
全链失败 ──▶ emergency truncation ──▶ 本地重试
```

- 归因头（每个响应）：`X-Proxy-Route-Target(cloud|local|local_forced)/Actual-Model/Reason/Cost`
- 非流式响应体（OpenAI 协议 + anthropic 协议通道）另带 `proxy_route{target,actual_model,reason,cost}`（usage 实际计费）

## 4. 运维操作速查

```bash
./manage.sh models             # 目录总览（providers/协议标记/key 就绪/models/routes/hash）
./manage.sh models-validate    # 目录校验（坏文件非零退出；热替换被拒绝时保留旧目录服务）
./manage.sh reload             # SIGHUP：conf + models.json + 分商 key + 分商信号量全热载
curl :4000/api/route/policies  # 脱敏目录 + catalog_hash（agent_go 漂移检测）
curl -X POST :4000/admin/reload -H "Content-Length: 0"   # HTTP 热重载（远程场景）
```

**新增云模型 SOP**：`configs/models.json` 加 provider/model(+route) 条目 → `secret.local.conf` 加 key（provider `key_env` 引用名）→ `./manage.sh models-validate && ./manage.sh reload`。零代码、不断服。

## 5. 遗留事项

1. **zhipu bigmodel 充值**（429/1113）：如需 openai 按量端点作为 glm 备援路径时再充值并切 `protocol: openai`
2. **token 轮换**：三枚 key（zhipu bigmodel / kimi / z.ai）均出现在聊天记录，建议轮换后更新 `secret.local.conf`
3. RouteNotification 文案：header_override 触发的切云通知沿用"超限"话术 + 估价用全局价（纯文案，不影响契约字段）
4. Kimi K3 开放平台 `reasoning_effort` 参数（low/high/max）未接入——kimi provider 走订阅通道暂无需要
5. DeepSeek 2026-08-17 分时调价生效后，如需精确核算更新目录两个数字即可

## 6. 验证记录

- 单测 979 全绿（当日 949 → 979，+30：registry 合成等价性/引用解析/校验规则/热替换拒绝/分商凭证/链降级/熔断隔离/URLError 回退/R8 头契约/R9 脱敏/R10 元数据/R11 段/R12 幂等/quirks）
- 集成 10 套件全过；签名快照重生成（+2 函数）；行为快照 57 例全过；promptfoo 5/5
- **真实云调用 e2e**：deepseek（200，真实计费 Cost 0.0005）、kimi k3（200，订阅）、Z.ai glm-5.2/5.3（200 非流式+usage 归因 / 69 SSE 事件透传）
- R1-R12 逐条实测合规审计：17 项检查 + 3 条真实推理请求全部通过
