# 多云端模型目录（Model Catalog）设计与架构 Review

> 状态：设计稿 v2（2026-08-15：并入需求稿 v2 的「定位澄清」——直连 vs 走代理分流、R8 响应头命名契约、双源真相边界）
> **Phase A 已实施（2026-08-15）**：`model_registry.py`（503 行）+ `configs/models.json`（deepseek/zhipu/kimi/local 四提供商，9 模型）+ proxy_state/reload_config 集成 + 39 个单测；949 unit / 10 integration 全绿，删除目录文件行为与 Phase A 前完全一致（兼容合成）。SIGHUP 重建 preferences，陈旧捕获缺陷已修复。
> **Phase B 已实施（2026-08-15）**：多提供商分发（按模型解析凭证/分商信号量）+ fallback_chain 跨商降级 + 分商熔断互不影响 + 按模型目录价格计费（分商预算双上限）+ R8 契约头四件套直接切换（`X-Proxy-Route-*`，旧名已清理）+ flash quirks 迁入目录（保留未知模型旧启发式兜底）；deepseek provider 改 `base_url_env`/`concurrent_env` 引用以保留 `PROXY_CLOUD_BASE_URL` 覆盖语义（中转场景）。965 unit / 10 integration 全绿。
> **端点官方校准（2026-08-15）**：zhipu OpenAI 路由=`https://open.bigmodel.cn/api/paas/v4`（docs.bigmodel.cn 确认，key 有效，429/1113=余额不足需充值；temperature 区间 (0,1) 不支持 0）。**zhipu 直连=`https://api.z.ai/api/anthropic`（Z.ai Coding Plan 订阅，实测 200；订阅额度仅覆盖 Anthropic 端点——`api.z.ai/api/paas/v4` 与 bigmodel.cn 的 OpenAI 端点均为独立按量计费（429 实测），代理的 OpenAI 分发路径暂无法消耗订阅额度，如需代理路由走订阅须扩展 Anthropic 协议分发（Phase D 候选）；glm-5.2 请求实际由 glm-5.3 服务）。**kimi=**`https://api.kimi.com/coding/v1`**（Kimi Code 会员订阅体系，kimi.com/code/docs 确认，真实调用 200；`api.kimi.com` 与 `api.moonshot.cn` 是两套独立体系/计费；模型名与 Anthropic 端点同名：k3/kimi-for-coding(-highspeed)，能力实测自 `/coding/v1/models`：k3=1048576 ctx、thinking only、vision）。moonshot 开放平台（`https://api.moonshot.cn/v1`，模型名 kimi-k3/kimi-k2.7-code(-highspeed)/kimi-k2.6，platform.kimi.com 确认）曾短暂加入目录，**同日经决策移除**（与 kimi 会员体系重复、key 未配且本机直连超时）；需要时在目录加 provider 条目即可恢复。
> **Phase C 已实施（2026-08-15）**：R9 `GET /api/route/policies`（admin_server `_build_route_policies_json`，脱敏 + catalog_hash）、R10 `/v1/models` 能力元数据（catalog 驱动）、R11 `/api/status` `route_config` 段、R12 `POST /admin/reload`（空 body 合法、_RELOAD_LOCK 串行化、幂等）、`manage.sh models`/`models-validate`；registry 无 getter 时 `$env` 回退读环境（CLI 场景）。973 unit + 10 integration 全绿。
> **Phase D 已实施（2026-08-15）：Anthropic 协议云分发**。provider 增 `protocol(openai|anthropic)` + `anthropic_key_env`（双端点双 key 体系）；anthropic 候选经 `_do_dispatch_anthropic` 发 `{anthropic_base_url}/v1/messages`——请求体复用管线 openai_body 经 `convert_openai_request_to_anthropic` 回转（与 /v1/chat/completions 入口同一条已测转换链），响应 SSE 原样透传 / 非流式 JSON 直返 + `proxy_route` 注入；OpenAI 协议客户端自动跳过 anthropic 候选（响应格式不兼容，沿 fallback 链降级）。zhipu provider 标记 protocol=anthropic → **Z.ai Coding Plan 订阅额度可被代理路由消耗**（真实调用验证：非流式 200 + usage 归因、流式 69 SSE 事件透传）；glm 价格改订阅边际 0（按量参考价移入 note，避免虚拟计费误触日预算闸门）。979 unit 全绿。
> 背景：未来需新增云端模型选择（glm5.2 / glm5.3 / K3 / deepseek-v4-pro 等），当前架构围绕单一云提供商（DeepSeek）硬编码，扩展需要改动多处代码。
> 关联：[llama-defender-integration-requirements.md](../llama-defender-integration-requirements.md)（v2，R8-R12）、[intelligent-model-routing-design.md](intelligent-model-routing-design.md)、agent_go 侧模型实体三层设计（① 模型固有 / ② 角色绑定 / ③ 部署拓扑）
> 目标：**新增一个云端模型 = 改一个声明式配置文件 + 配一个 API Key，零代码改动，SIGHUP 热生效。**

---

## 1. 现状梳理：模型管理全景

### 1.1 一个请求中"模型"的解析链路

```
Client 请求 model="claude-opus-4-7"（别名，永远不直达后端）
  │
  ├─ RequestParser._classify_tier()          pipeline.py:496   → opus/haiku/sonnet 档位（硬编码字符串匹配）
  ├─ SmartRouter._routing_decision()         pipeline.py:570
  │    └─ MODEL_ROUTE_PREFERENCES[alias]     proxy_state.py:521 → {route_bias, threshold_factor,
  │                                                              memory_bias, cloud_model, behavior}
  │    └─ ctx._route_cloud_model = pref.cloud_model 或 PROXY_CLOUD_MODEL
  ├─ FormatConverter                          pipeline.py:1797
  │    └─ openai_body["model"] = cloud_model（云端）或 MODEL_NAME（本地）
  │    └─ flash 特例：thinking 强制 disabled（硬编码 if "flash" in MODEL_NAME）
  └─ BackendDispatcher                        pipeline.py:1987
       └─ 云端：POST PROXY_CLOUD_BASE_URL/chat/completions，鉴权 PROXY_CLOUD_API_KEY
       └─ 响应头：X-Actual-Model / X-Route-Target / X-Route-Reason
```

关键性质（必须保留）：**客户端只见别名；真实模型名仅在 BackendDispatcher 替换**。

### 1.2 模型知识的代码触点（当前共 8 处硬编码）

| # | 位置 | 内容 | 新增 glm5.2 要改吗 |
|---|------|------|--------------------|
| 1 | `proxy_state.py:408-417` | `PROXY_CLOUD_BASE_URL/API_KEY/MODEL` + `PRICE_INPUT/OUTPUT` — **单提供商、单价格** | ✅ 无法表达第二家 |
| 2 | `proxy_state.py:521` `MODEL_ROUTE_PREFERENCES` | 别名→路由偏好，`cloud_model` 写死 `deepseek-v4-pro` | ✅ |
| 3 | `proxy_state.py:628` `get_model_aliases()` | `/v1/models` 别名列表写死 | ✅ |
| 4 | `pipeline.py:496` `_classify_tier()` | opus/haiku 字符串硬编码 | 视需要 |
| 5 | `pipeline.py:1818` | `"flash" in MODEL_NAME → thinking disabled` 的 DeepSeek 特有 hack | ✅（每家厂商的怪癖都会长进代码） |
| 6 | `proxy_state.py:569` `_accumulate_route_daily_cost()` | 成本用**全局单一价格对**计算，跨模型归因错误 | ✅ |
| 7 | `anthropic_proxy.py:421` `/v1/models` | 只回别名 + route，无能力元数据（R10 缺口） | ✅ |
| 8 | `admin_server.py:999` | `/api/status` 只回单一 `cloud_base_url`（R11 缺口） | ✅ |

### 1.3 现有配置面

- `configs/*.conf`：每个 profile 一组 `LLAMA_*` / `PROXY_*`；云端 profile（`deepseek-chat.conf`）只能描述**一家**提供商。
- `configs/secret.local.conf`：单把 `PROXY_CLOUD_API_KEY`。
- `_RELOAD_SPEC`（SIGHUP 热重载）：覆盖 `PROXY_CLOUD_MODEL/BASE_URL/API_KEY/PRICE` 等标量，但 **`MODEL_ROUTE_PREFERENCES` 是 Python dict，不在重载范围内**——热重载改 `PROXY_CLOUD_MODEL` 不会刷新 preferences 里捕获的旧值（import 时求值），这是一个现存缺陷。

### 1.4 与 agent_go 三层设计的对齐现状

| 层 | 归属 | 现状 |
|----|------|------|
| ① 模型固有（价格/能力/上下文） | agent_go registry，数据源应是代理 | 代理无处存放 → agent_go 手工录入（G1/R10） |
| ② 角色绑定（temperature/thinking 开关等） | agent_go | 无代理依赖 ✅ |
| ③ 部署拓扑（哪个模型部署在哪、走哪个端点） | **代理** | 只有单提供商全局变量，不可视（G3/R9） |
| ④ 观测归因（实际路由/真实模型/费用） | 代理回传 | 头已有三个字段，缺 Cost（G2/R8） |

---

## 2. 架构 Review（整体）

### 2.1 做得好的（保持不动）

- **24-stage 管线**（`pipeline.py`）：薄封装、可独立测试、deferred import 规避循环依赖。
- **BackendStrategy 策略模式**（`backend_strategy.py`）：local/cloud 默认值收敛，新后端类型只需加策略类。
- **`proxy_state.py` 单一真相源 + SIGHUP 热重载**：dual-setattr 保证子模块与主模块都能看到更新。
- **错误分类重试（DEF-001）**、OOM 护栏（memory reject / dynamic max_tokens / dynamic concurrency）。
- **三层测试**（unit 910 / integration 9 / e2e）+ 签名/快照/需求追踪扩展层。
- **双协议端点**（`/v1/messages` + `/v1/chat/completions`）与工具调用三层 fallback。

### 2.2 问题清单（按优先级）

| 优先级 | 问题 | 影响 | 本设计是否解决 |
|--------|------|------|----------------|
| P0 | 模型知识 8 处硬编码散点（§1.2） | 加模型 = 改代码 + 发版 | ✅ 核心目标 |
| P0 | 单云提供商假设（BASE_URL/KEY 单例） | 无法接入 GLM/K3 等第二家 | ✅ |
| P0 | 成本核算全局单一价格 | 跨模型/跨商计费归因全错（与 R8 直接相关） | ✅ |
| P1 | `MODEL_ROUTE_PREFERENCES` 不可热重载（import 时捕获 `PROXY_CLOUD_MODEL`） | 热切云端默认模型不生效 | ✅ catalog 随 SIGHUP 重建 |
| P1 | R8-R12 五个缺口 | agent_go 集成受阻 | ✅ 顺带交付 |
| P2 | `pipeline.py` BackendDispatcher 过重（传输+回退+响应写回一体，2384 行文件） | 可维护性 | ⚠️ 建议后续抽 transport 层，本设计不动 |
| P2 | `admin_server.py` 2650 行（HTML+JSON 混合） | 可维护性 | ❌ 不动，非本范围 |
| P3 | `test/README.md` 用例数过期 | 文档漂移 | ❌ 顺手修 |

---

## 3. 设计：模型目录 `configs/models.json`

### 3.1 核心思想

把 §1.2 的 8 处硬编码收敛为**一个声明式 JSON 目录**（stdlib `json` 即可，符合零依赖约束），三层结构与 agent_go 的模型实体三层设计一一对应：

- `providers` 段 = **③ 部署拓扑**（代理拥有：端点在哪、key 在哪、并发多少）
- `models` 段 = **① 模型固有**（价格、能力、怪癖——agent_go registry 的数据源，R10 直接透出）
- `routes` 段 = 别名→模型的绑定策略（代理路由决策输入；② 角色绑定仍归 agent_go）

密钥**不进目录**：provider 只写 `key_env` 名字，真实值放 `secret.local.conf`（`ZHIPU_API_KEY="..."`），保持现有 gitignore 机制。

### 3.1.1 直连 vs 走代理：定位与双源真相边界（需求稿 v2 定位澄清）

需求稿 v2 明确：**非所有流量必须走代理**。代理的三重价值 = 托管（进程/GPU/生命周期）+ 智能路由（本地↔云端分流、超长转云）+ 报文压缩（压缩到本地可用范围）。agent_go 的分流决策：

| 流量 | 路径 | 判定条件（agent_go 侧） |
|------|------|------------------------|
| 云端模型 + Anthropic 兼容 + 上下文充足 | **直连** provider 的 Anthropic 端点 | 无需代理 |
| 本地模型 / 非 Anthropic 协议 / 需压缩或智能分流 | **走代理** :4000 | 代理托管/路由/压缩 |

对本设计的影响：

1. **目录 schema 增加直连资格元数据**：provider 级 `anthropic_base_url` + `anthropic_compatible`（见 §3.2）。这是 agent_go 判断"可否直连"的数据源，也解释了为什么 glm5.3 走直连而本地 Qwen 必须走代理。
2. **双源真相边界（明确声明，不强行单一来源）**：直连流量 agent_go 必须自持端点与 key（key 不允许过 HTTP），因此 agent_go `models.json` 与本目录**必然各有一份 provider 配置**。约定：
   - **本目录 = 走代理路径的唯一权威**（路由决策、按模型计费、quirks、能力元数据）
   - **agent_go models.json = 直连路径的唯一权威**（角色绑定、quality_tags、直连 pricing）
   - **漂移检测代替复制**：`GET /api/route/policies`（R9）回传脱敏目录 + `catalog_hash`（目录内容稳定哈希）；agent_go 定期比对哈希，不一致时告警提示人工同步两侧价格/端点。
3. **走代理的云流量成为少数路径**：云路由的主要场景收敛为「超长转云 + 本地故障回退 + 需压缩」，`fallback_chain` 与按商熔断的价值上升（本地不可用时跨商兜底），而"常态云端优先"不再是默认假设——routes 段的 bias 配置完全表达这两种策略，无需代码区分。
4. **R8 归因头仅走代理路径需要**：直连流量 agent_go 天然知道目标。这也意味着归因头命名必须与契约严格一致（见 §3.4.1），否则唯一能拿到归因的路径也拿不到。

### 3.2 Schema

```json
{
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/v1",
      "anthropic_base_url": "https://api.deepseek.com/anthropic",
      "anthropic_compatible": true,
      "key_env": "PROXY_CLOUD_API_KEY",
      "concurrent": 4
    },
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "anthropic_base_url": "https://open.bigmodel.cn/api/anthropic",
      "anthropic_compatible": true,
      "key_env": "ZHIPU_API_KEY",
      "concurrent": 2
    },
    "moonshot": {
      "base_url": "https://api.moonshot.cn/v1",
      "anthropic_base_url": "https://api.moonshot.cn/anthropic",
      "anthropic_compatible": true,
      "key_env": "MOONSHOT_API_KEY",
      "concurrent": 2
    },
    "local": {
      "base_url_env": "LLAMA_BASE_URL",
      "key_env": "LLAMA_API_KEY",
      "concurrent_env": "PROXY_MAX_CONCURRENT",
      "anthropic_compatible": false
    }
  },

  "models": {
    "deepseek-v4-pro": {
      "provider": "deepseek",
      "tier": "flagship",
      "price": {"input": 2.0, "output": 8.0, "currency": "CNY"},
      "capabilities": {"thinking": "required", "json": "strict", "context_tokens": 1000000},
      "request_quirks": {}
    },
    "deepseek-v4-flash": {
      "provider": "deepseek",
      "tier": "fast",
      "price": {"input": 0.5, "output": 1.5, "currency": "CNY"},
      "capabilities": {"thinking": "unsupported", "json": "loose", "context_tokens": 128000},
      "request_quirks": {"force_thinking_disabled": true}
    },
    "glm5.2": {
      "provider": "zhipu",
      "tier": "flagship",
      "price": {"input": 2.0, "output": 8.0, "currency": "CNY"},
      "capabilities": {"thinking": "supported", "json": "loose", "context_tokens": 200000},
      "request_quirks": {}
    },
    "glm5.3":  { "provider": "zhipu", "tier": "flagship", "price": { "...": "..." } },
    "k3":      { "provider": "moonshot", "tier": "flagship", "price": { "...": "..." } },
    "local-default": {
      "provider": "local",
      "tier": "standard",
      "price": {"input": 0, "output": 0},
      "capabilities": {"thinking": "supported", "json": "loose", "context_tokens": 131072}
    }
  },

  "routes": {
    "claude-opus-4-7": {
      "bias": "prefer_cloud",
      "cloud_model": "deepseek-v4-pro",
      "fallback_chain": ["glm5.2"],
      "behavior": "force_fallback",
      "threshold_factor": 0.8,
      "memory_bias": -5
    },
    "claude-sonnet-4-6": {
      "bias": "auto",
      "cloud_model": "deepseek-v4-flash",
      "behavior": "prefer",
      "threshold_factor": 1.0
    },
    "claude-haiku-4-5": {
      "bias": "prefer_local",
      "cloud_model": "deepseek-v4-flash",
      "behavior": "prefer",
      "threshold_factor": 1.33
    }
  },

  "defaults": {
    "cloud_model": "deepseek-v4-flash",
    "daily_budget": 5.0,
    "per_provider_budget": {"deepseek": 4.0, "zhipu": 1.0}
  }
}
```

要点：

- **`cloud_model` 支持字符串（兼容）或 `fallback_chain` 数组**：主模型 429/5xx/冷却时按序降级（如 pro→glm5.2），解决单点依赖。
- **`request_quirks`** 取代代码里的厂商特例（flash thinking-disabled hack 迁移至此），由 FormatConverter 统一 `apply_request_quirks()` 应用。
- **直选模型**：目录中的模型名（如 `glm5.2`）本身可作为请求 `model` 值直达——命中目录即按其 provider 路由（`routes` 无条目时用 `defaults`），未命中走现有别名逻辑。这给 agent_go 提供"精确点名"能力，别名层保持 Claude 风格稳定。
- **价格按模型**：`_accumulate_route_daily_cost(model, in, out)` 改为查目录价；预算支持全局 + 分提供商双上限。

### 3.3 新模块 `model_registry.py`（~250 行，stdlib only）

职责：

1. **加载与校验**：读 `configs/models.json`（`PROXY_MODELS_CATALOG` 可覆盖路径）；结构/引用校验 fail-fast，坏文件报错并**拒绝热替换**（保留旧目录继续服务，日志 ERROR）。
2. **访问接口**：
   - `get_model(name) / list_models()` — 含 provider、price、capabilities
   - `get_provider_for_model(name)` — `{base_url, api_key, concurrent}`
   - `get_route(alias)` — 返回与现 `MODEL_ROUTE_PREFERENCES` 同构的 dict（兼容层）
   - `get_alias_list()` — `/v1/models` 用
   - `reload()` — SIGHUP 时重建
3. **向后兼容合成**：目录文件不存在时，从现有 env/硬编码值**合成**一个等价目录（deepseek 单提供商 + 现三条 preferences）——**部署零变化，行为零变化**，现有配置/测试不需要任何改动即可跑。
4. **派生旧全局量**：`PROXY_CLOUD_MODEL` 等保留为目录 `defaults` 的视图（加载时赋值），`MODEL_ROUTE_PREFERENCES` 变为加载时从 `routes` 段构建的派生 dict——`_RELOAD_SPEC` 里已存在的热重载条目继续工作，且 reload 会**重建** preferences，顺带修复 §2.2 的 import 捕获缺陷。

### 3.4 代码改动点（收敛后）

| 模块 | 改动 |
|------|------|
| `model_registry.py` | 新增（解析/校验/访问/兼容合成/重载） |
| `proxy_state.py` | 删除 §1.2 触点 1/2/3 的硬编码，改为从 registry 派生；保留变量名兼容 |
| `reload_config.py` | SIGHUP 时调 `model_registry.reload()` + `invalidate_model_aliases_cache()` |
| `pipeline.py` SmartRouter | `MODEL_ROUTE_PREFERENCES.get` → `registry.get_route`；解析 fallback_chain 到 `ctx._route_fallback_models` |
| `pipeline.py` BackendDispatcher | 云端分发改为按 `get_provider_for_model()` 取 base_url/key；`_cloud_lock` 全局信号量 → 按提供商信号量池 |
| `pipeline.py` FormatConverter | flash hack → `apply_request_quirks(openai_body, model_entry)` |
| `pipeline.py` / `proxy_state.py` 成本 | 按模型价格累计；预算双上限；响应头补 `X-Proxy-Route-Cost`（R8） |
| `anthropic_proxy.py` `/v1/models` | metadata 增 `real_model / thinking_supported / thinking_required / json_compliance / context_chars / price / direct_capable`（R10；`direct_capable = provider.anthropic_compatible && 非本地模型`，供 agent_go 直连判定） |
| `admin_server.py` | 新增 `GET /api/route/policies`（R9，脱敏：key 只回 `key_set: bool`；含 providers/models/routes/defaults 全量 + `catalog_hash` 供 agent_go 漂移检测）；`/api/status` 增 `route_config` 段（R11）；`POST /admin/reload`（R12，同时重载 models.json 目录） |
| `manage.sh` | 新增 `models` 子命令（人读目录+路由表+key 就绪状态）；`models-validate` |
| 密钥 | `secret.local.conf` 增加分提供商 key（`ZHIPU_API_KEY` / `MOONSHOT_API_KEY`），key_env 间接引用 |

### 3.4.1 ⚠️ R8 响应头命名契约对齐（现状 P0 集成缺陷）

需求稿 v2 §R8 规定的契约头名与当前实现**不一致**（`pipeline.py:1979-1984`）：

| 契约（agent_go 按此读取） | 当前实现（`pipeline.py`） | 状态 |
|---|---|---|
| `X-Proxy-Route-Target: cloud\|local\|local_forced` | `X-Route-Target`（且无 `local_forced` 值） | ❌ 名字不符 |
| `X-Proxy-Route-Actual-Model` | `X-Actual-Model` | ❌ 名字不符 |
| `X-Proxy-Route-Reason` | `X-Route-Reason` | ❌ 名字不符 |
| `X-Proxy-Route-Cost: 0.0002` | （无） | ❌ 缺失 |

agent_go 侧 fail-open 降级意味着**这个不匹配不会报错，只会让 G2 归因问题静默持续**。修复方案（并入 Phase B，**决策：直接切换，不留旧名别名**——2026-08-15 确认，唯一外部消费方 agent_go 按契约名读取，旧名从未进入契约）：

1. 按**契约名**发送四个头（`X-Proxy-Route-*`），值语义：
   - `Target`：`cloud | local | local_forced`（会话被 `route-force-local` 或 header 覆盖强制本地时用 `local_forced`）
   - `Reason`：现有 reason 字符串去掉括号详情后的稳定枚举（如 `model_forced_fallback_cloud`），括号详情可保留在尾部但 agent_go 按前缀匹配（契约示例即如此）
   - `Cost`：本次请求实际云端费用（本地为 `0`），按 §3.3 目录价格 × usage 计算
2. **同 PR 内清理全部旧名内部消费方**（仓内共 4 处，已排查）：
   - `pipeline.py:1980-1983`（发送方，改名）
   - `tools/bench_route.py` 6 处 `h.get('X-Route-Target'/'X-Actual-Model')`（读取方，改名）
   - `test/unit/test_pipeline_stages.py:1132` 断言 `X-Route-Target`（测试，改名）
   - `CLAUDE.md` / `AGENTS.md` 文档中的旧头名（同步更新）
   - `anthropic_proxy.py:884/1153/1216` 只泛读 `_route_response_headers` dict，无名字耦合，不用改
3. 非流式响应体增加 `"proxy_route": {"target","actual_model","reason","cost"}`（R8 备选方式，一并实现）
4. 新增 admin 端点响应体统一带 `"api_version": "1"`（接口协议要求 §4）

### 3.5 分发与并发

- 按提供商独立信号量：`{"deepseek": Semaphore(4), "zhipu": Semaphore(2), ...}`，本地后端沿用 `_llama_lock`。单提供商故障熔断（现有 `PROXY_ROUTE_CLOUD_COOLDOWN_SECONDS` 机制）按提供商实例化，一家冷却不拖累另一家。
- 回退链失败语义：主模型尝试失败 → 链上下一家（跨提供商时最有价值）；全链失败 → 现有 emergency truncation → local 重试路径不变。

---

## 4. 实施计划

| 阶段 | 内容 | 交付对应 | 规模 |
|------|------|----------|------|
| **Phase A（P0）** | `model_registry.py` + `models.json` + 兼容合成 + SIGHUP 集成 + 单元测试（解析/校验/合成/路由决策等价性） | 无行为变化的地基 | ~1-2 天 |
| **Phase B（P0）** | 多提供商分发 + 按提供商并发/熔断 + fallback_chain + 按模型成本核算 + **R8 契约头对齐（`X-Proxy-Route-*` 四头 + `local_forced` + 非流式 `proxy_route` 体字段，直接切换不留别名，见 §3.4.1）** | 核心扩展能力 + R8 | ~2-3 天 |
| **Phase C（P1）** | R9 `/api/route/policies`（含 `catalog_hash` 漂移检测）、R10 `/v1/models` 元数据（含 `direct_capable`/price）、R11 `route_config`、R12 `/admin/reload`、`manage.sh models`、CLAUDE.md/AGENTS.md 同步 | llama-defender R8-R12 全清 | ~1-2 天 |

每阶段独立可发布：A 合入后无任何行为差异；B 合入后单提供商场景行为不变；C 纯增量端点。

## 5. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 目录文件写坏导致服务不可用 | 校验 fail-fast + 拒绝热替换（保留旧目录）+ `manage.sh models-validate` 本地预检 |
| 旧测试/代码引用 `MODEL_ROUTE_PREFERENCES` | 保留同名派生 dict（构建自 routes 段），只删硬编码来源 |
| 密钥泄漏 | key 只存 `secret.local.conf`；`/api/route/policies` 只回 `key_set` 布尔（R9 已约定） |
| 直选模型名与别名冲突 | 校验规则：目录模型名不得以 `claude-` 开头；别名命名空间与模型命名空间分离 |
| 跨提供商响应格式差异（reasoning 字段等） | 现有 message_converter 已兼容 DeepSeek/OpenAI 风格；quirks 段可按需扩 `response_quirks`，Phase B 用 mock 双后端集成测试兜底 |
| 双源配置漂移（agent_go 直连配置 vs 代理目录） | 不强行单一来源（key 不能过 HTTP，agent_go 必须自持）；R9 回传 `catalog_hash`，agent_go 定期比对告警，人工同步 |
| 旧响应头 `X-Route-*` 切换风险 | 已决策直接切换不留别名：旧名从未进入 agent_go 契约，内部消费方仅 3 处（bench_route.py / test_pipeline_stages.py / 文档），同 PR 一并清理（§3.4.1） |
| 预算双上限复杂化 | 先全局后分商的判定顺序，`/status` 展示两层余额 |

## 6. 验收标准

1. **新增 glm5.2 全流程零代码**：`models.json` 加 provider+model+route 条目 → `secret.local.conf` 加 key → `./manage.sh reload` → `/v1/models` 可见、请求 `model=claude-sonnet-4-6` 超阈值路由至 glm5.2、`X-Proxy-Route-Actual-Model`/`X-Proxy-Route-Cost` 正确。
2. **R8 契约一致性**：四个 `X-Proxy-Route-*` 头名与需求稿 §R8 逐字一致；`Target` 含 `local_forced`；非流式响应含 `proxy_route` 体字段；agent_go 按契约名可读取（不再依赖旧 `X-Route-*`）。
3. 删除 `models.json` → 服务行为与当前版本完全一致（兼容合成）。
4. `GET /api/route/policies` 输出脱敏路由表（providers + models + routes + `catalog_hash`，key 只回布尔）；`GET /api/status` 含 `route_config`；`POST /admin/reload` 等效 SIGHUP 且重载目录。
5. 单测：registry 解析/校验失败路径/兼容合成等价性/按模型成本/quirks 应用/头名契约；集成：mock 双提供商后端验证按商分发、fallback_chain、分商熔断互不影响。
6. 三个测试层级全绿（`bash test/run_tests.sh --all`）。
