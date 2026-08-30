# 系统知识阅读清单（按对本项目的边际收益排序）

> **日期**：2026-08-30｜**背景**：信息面工作流（IFC/PDC/实验体系）涉及的体系要素盘点后，对 CSAPP 覆盖度的评估结论——CSAPP 覆盖约一半（虚拟内存/缓存/信号/并发恰好是最核心隐喻的出处），真正缺口是 GPU/LLM 推理系统与信息论，经典分布式（CAP/共识/分片）当前架构无对应物暂不需要。
> **关联**：[信息面度量推广与应用](information-metrics-applications-survey-20260830.md) · [本地执行栈与训练可行性](local-stack-and-training-feasibility-20260830.md) · [v0.7.0 看板 IFC-10](../07-project-board/v0.7.0-information-plane.md)

---

## ① CSAPP《深入理解计算机系统》第 3 版 —— 重读 4 章（最高优先）

| 资源 | 链接 |
|---|---|
| 官网（书 + labs） | <https://csapp.cs.cmu.edu/> |
| 配套实验（Attack/Cache/Shell 等） | <https://csapp.cs.cmu.edu/3e/labs.html> |
| 配套公开课（免费视频） | CMU 15-213: <https://www.cs.cmu.edu/~213/> |

**带项目对照的读法**：

| 章节 | ↔ 本项目要素 |
|---|---|
| ch6 存储层次结构 | prefix cache / hybrid cache 条目 / radix+快照 的命中行为 |
| **ch9 虚拟内存** | **PDC 全套隐喻的字面出处**：页表=manifest、缺页=召回、驻留集=钉、thrashing=游走、LRU=钉晋升降级——读完可直接检验 PDC 设计是否忠实 |
| ch8 异常控制流 | DEF-306 reload 事故（SIGHUP 异步上下文中的异常安全——教科书级案例） |
| ch12 并发编程 | `_diag_lock`/信号量/`MAX_CONCURRENT=1`/探针的有界等锁 |

---

## ② DDIA《设计数据密集型应用》—— 全本

| 资源 | 链接 |
|---|---|
| 官网 | <https://dataintensive.net/> |
| O'Reilly 正式页（**第 2 版 2026-02 出版**，新增云技术章节，较 1 版更贴本项目） | <https://www.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/> |
| 免费正版途径 | ScyllaDB 赞助免费电子书：<https://lp.scylladb.com/designing-data-intensive-apps-book-offer>；O'Reilly 10 天试用含[第 2 版 12 章](https://www.reddit.com/r/ExperiencedDevs/comments/1nn0yl4/designing_data_intensive_applications_2nd_edition/)先行 release |

**对位**：append-only 日志/事件溯源 ↔ 台账/档案/manifest 的 A3 模式；日志压实与分区 ↔ JSONL 轮转 + MB 上限驱逐；重试/幂等/熔断 ↔ 云 API 冷却、fallback chain、幂等闸；配置漂移 ↔ config 指纹（IFC-2）。

> 分布式知识的结论：本系统是"单机多进程 + 云 API 客户端"，CAP/共识/分片无对应物——DDIA 后半的**客户端容错模式**（重试语义/幂等/背压）即所需全部；出现多机集群/多端同步场景再补。

---

## ③ 信息论 —— 精读两章 + 免费先修课

| 资源 | 链接 | 适用度 |
|---|---|---|
| Cover & Thomas《Elements of Information Theory》第 2 版（Wiley） | <https://www.wiley.com/en-us/Elements+of+Information+Theory-p-9780471241959> | 正典。**精读 ch2（熵与互信息——H_BE 与 D_ledger 的数学根基）+ ch10（率失真理论——压缩汇率的数学出处）**，其余浏览 |
| MIT 6.050J Information and Entropy（OCW 免费，零门槛） | <https://ocw.mit.edu/courses/6-050j-information-and-entropy-spring-2008/> | 编码/压缩/熵全程课（参考教材即 Cover）；**先修这门再翻书效率最高** |

---

## ④ vLLM / MLX 源码导读 —— 配合 IFC-10 后端选型实操，读码与测数据同步

| 资源 | 链接 | 对位 |
|---|---|---|
| PagedAttention 论文 | <https://arxiv.org/abs/2309.06180> | KV cache 分页——hybrid cache 的对照系（重点读 §2-3） |
| vLLM 官方博客 | <https://blog.vllm.ai/> | 版本演进与设计决策 |
| Continuous batching 深读 | <https://www.anyscale.com/blog/continuous-batching-llm-inference> | 批处理与吞吐 |
| vLLM 源码 | <https://github.com/vllm-project/vllm> | |
| MLX 框架 | <https://github.com/ml-explore/mlx> · 文档 <https://ml-explore.github.io/mlx/> | 本机栈底座（统一内存/GPU 调度） |
| mlx-lm（含 LoRA） | <https://github.com/ml-explore/mlx-lm> | **IFC-10 的 35B remap 定档在此做**：精读 `models/qwen3_5.py`（383 行，GDN+SparseMoeBlock）与 `models/qwen3_next.py`（461 行，GDN+MoE 完整实现） |
| MLX 官方示例 | <https://github.com/ml-explore/mlx-examples> | |

**读法**：不通读——以 IFC-10 的 30 分钟定档任务为钉子，两个模型类文件 + PagedAttention §2-3，带着"KV 幽灵损失通道怎么测"的问题去读。

---

## 阅读序总结（按边际收益）

```
CSAPP ch6/9/8/12 重读(带 PDC/事故对照)  →  DDIA 2e 全本
→ MIT 6.050J(免费先修) → Cover & Thomas ch2+ch10
→ mlx-lm 两个模型类 + PagedAttention §2-3(配合 IFC-10 实操)
```

## 本系统体系要素 → 知识来源映射总表

| 要素（本会话真实用例） | CSAPP | DDIA | 信息论 | vLLM/MLX |
|---|---|---|---|---|
| 统一内存/GPU wired 上限/Metal OOM 与死锁 | — | — | — | ✅（MLX 底座） |
| 内存带宽 → decode 吞吐；prefill/decode 不对称 | — | — | — | ✅ |
| 量化数值格式（oQ4e / KV 4-bit） | — | — | 率失真视角 ✅ | ✅ |
| 缓存层次（prefix/hybrid/radix+快照） | ch6 ✅ | 缓存章 ✅ | — | ✅ |
| 虚拟内存全套（PDC 隐喻） | ch9 ✅ | — | — | — |
| 信号处理（reload 事故） | ch8 ✅ | — | — | — |
| 并发/锁/信号量 | ch12 ✅ | — | — | — |
| append-only/轮转/驱逐/event sourcing | — | ✅ | — | — |
| 倒排索引（FTS5） | — | ✅ | — | — |
| 熵/互信息/率失真（H_BE/D_ledger/压缩汇率） | — | — | ✅ | — |
| 重试/幂等/熔断/背压 | — | ✅ | — | — |
| KV cache/投机解码/continuous batching | — | — | — | ✅ |
