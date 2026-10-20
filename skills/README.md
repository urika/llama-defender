# 共享技能（Skills）

面向 Agent 的**可复用操作技能**目录。与 `docs/` 的关系是互补而非重叠：

| | `docs/` | `skills/` |
|---|---|---|
| 读者 | 人（产品/架构/运维/分析） | Agent（Claude Code / ZCode / Command Code 等） |
| 形态 | 叙述性文档 | `SKILL.md` 指令 + 按需加载的 `references/` |
| 加载 | 人主动阅读 | 元数据匹配触发 → L2 正文 → L3 二跳引用 |

> 设计依据见
> [`docs/02-architecture-design/skill-progressive-disclosure-insights-20260901.md`](../docs/02-architecture-design/skill-progressive-disclosure-insights-20260901.md)：
> L1 元数据、L2 `SKILL.md` 正文、L3 `references/` 资源的分层披露。

## 目录约定

```
skills/
└── <skill-name>/            # 小写连字符，与 SKILL.md 的 name 字段一致
    ├── SKILL.md             # 必需。YAML frontmatter + 正文（L2）
    └── references/          # 可选。按需二跳加载的细化资料（L3）
        ├── <topic-a>.md
        └── <topic-b>.md
```

### `SKILL.md` frontmatter

```yaml
---
name: weread                 # 与目录名一致
version: 1.0.4               # 语义化版本，独立演进
description: "……"            # L1 元数据：写给「何时触发」判断用，需覆盖用户可能的口语说法
---
```

`description` 是唯一的触发依据，应当写**使用场景**而不是功能罗列——尤其要把用户
可能说的中文口语（如「书架」「读书时间」「划线」）和英文别名都覆盖进去。

### references 写法

- `SKILL.md` 正文用**相对路径**引用，如 `references/domain-rules.md`，保证可移植。
- 采用「做 Y 时读 reference 的 Z 节」的**链式递进**，不要把全部细节塞进 L2。
- 单个 reference 建议 < 300 行；超了就再分层。

## 为什么放在顶层 `skills/` 而不是 `docs/` 或 `.agents/`

- **不放 `docs/`**：`docs/` 的 7 类目录结构是固定的（见 `docs/README.md` 与 AGENTS.md §6.4），
  且面向人类读者；技能是 Agent 运行时资产，混入会破坏既有分类约定。
- **不放 `.agents/`**：该目录是工具本地状态，已在 `.gitignore` 中忽略
  （其中 `.claude/settings.local.json` 含明文密钥，绝不能入库）。
- 顶层 `skills/` 与 `tools/`、`configs/`、`assets/` 同级，语义清晰、无命名冲突。

## 工具侧发现

不同 Agent 工具各自有固定的技能搜索路径（如 `.agents/skills/`、`.claude/skills/`）。
本仓的做法是**单一真相源 + 符号链接**：

```bash
# 本仓已建立（符号链接不入库，仅本地生效）
ln -s ../../skills/weread .agents/skills/weread
```

新增技能后按需在工具目录建立同名符号链接即可，**不要把内容复制成两份**（会产生版本漂移）。

## 现有技能

| 技能 | 版本 | 用途 |
|------|------|------|
| [`weread`](./weread/SKILL.md) | 1.0.4 | 微信读书个人数据：搜书、书架、阅读进度/时长、笔记划线导出、书评、阅读统计、个性化推荐、首次 API Key 配置 |

## 新增技能检查清单

- [ ] 目录名 = `SKILL.md` 的 `name` 字段（小写连字符）
- [ ] `description` 覆盖中英文触发词与典型口语场景
- [ ] `references/` 用相对路径引用，无本机绝对路径
- [ ] 无明文密钥（密钥一律走环境变量，如 `WEREAD_API_KEY`）
- [ ] 本文件「现有技能」表已登记
- [ ] 需要时在工具目录建立符号链接
