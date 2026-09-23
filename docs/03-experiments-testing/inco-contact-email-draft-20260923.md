# 给 Inco AI 的联系邮件（草稿）

> 收件人：contact@inco.ai
> 主题：**Splash packaging request: Ornith-1.5-35B-A3B (MoE + GatedDeltaNet hybrid, same family as Qwen3.6-35B-A3B)**
> 状态：**草稿待用户审阅** ｜ 撰写：2026-09-23

---

**Subject:** Splash packaging request: Ornith-1.5-35B-A3B (MoE + GDN hybrid, same family as Qwen3.6-35B-A3B)

Hi Inco team,

Congratulations on the Splash release — we benchmarked it on our M5 Pro (48 GB) the
day after your launch post, and the numbers hold up impressively well.

We run a local LLM inference stack that serves coding agents (Claude Code-compatible)
on Apple silicon, and we would like to formally request Splash packaging (including a
trained DFlash 2 draft) for:

**pyros-vault/Ornith-1.5-35B-A3B** (oQ4e, MIT license)

**Why we think this is a good fit:**

1. **Same architecture family you already support.** Ornith-1.5-35B-A3B is a
   Mixture-of-Experts (256 experts, top-8) model with Gated DeltaNet hybrid attention
   (10/40 full-attention layers + 30 GDN layers) — the same Qwen3.5-family hybrid-MoE
   layout as Qwen3.6-35B-A3B, which your engine already ships as
   `splash-packed-q4-moe` (manifest schema 4). Native 262K context.

2. **We are your reference hardware.** All of our measurements are on a 48 GB M5 Pro
   (16-core GPU) — exactly your benchmark machine.

3. **Same-machine comparison vs your Qwen3.6-35B-A3B package** (append-only prose
   decode, ~1K-token outputs, measured over HTTP):

   | Metric | Splash Qwen3.6-35B-A3B | our stack (rapid-mlx 0.14.3, bf16 KV) |
   |---|---|---|
   | prose decode | **117.0 tok/s** | 84.1 tok/s (1.39×) |
   | code-style decode | **264.8 tok/s** | — (no draft available) |
   | warm TTFT, 16K / 32K | **0.11 s / 0.10 s** | 0.68 s / 0.87 s (6–8.7×) |
   | cold prefill, 32K | **3,139 tok/s** | ~703 tok/s (4.4×) |

   Our baseline on Ornith is 84.1 tok/s steady-state decode (three-arm matrix,
   warm), which is already the fastest we have measured on this hardware for this
   model — and still 1.39× behind your engine on the same model family, before any
   DFlash 2 contribution.

4. **You invited exactly this.** Your launch post says you want to hear from people
   who want "a model on it, including your own fine-tunes" — this is that request.

**What we can offer in return:** detailed benchmark data on the 48 GB M5 Pro across
4K–160K context (we have a context-ladder harness and reproducible baselines), a
long-session workload profile from production agentic use (multi-hour, 64K+
contexts, tool-heavy), and prompta/edge-case testing on macOS 26.5.

If a trained DFlash 2 draft for Ornith is feasible, even better — our code-task
measurements suggest the acceptance-rate dividend on this model family would be
substantial.

Happy to share our full benchmark JSONs, run any model you package through our
regression suite, or test pre-release builds.

Thanks for building this,
<姓名>
<角色/项目 — llama-defender 本地推理栈>
<联系方式>

---

## 背景材料（发信前由用户补齐）

| 项 | 待填 |
|---|---|
| 发件人姓名/角色 | 【用户填】 |
| 联系方式（邮箱） | 【用户填】 |
| Ornith 是否有公开 HF 页 | 需确认 `pyros-vault/Ornith-1.5-35B-A3B` 是否公开可下载（邮件中如为私有需说明） |
| 是否附 benchmark JSON | 建议附 `splash-35b-a3b-20260923.json` + `splash-vs-rapidmlx-27b-20260923.json` |

## 语气与事实核查清单

- [x] 「day-after benchmark」属实（发布 9-17，我方 9-18 装）
- [x] 「10/40 full-attention + 30 GDN」属实（模型 config 反推）
- [x] 「splash-packed-q4-moe schema 4」属实（35B manifest 实测）
- [x] 117.0 / 264.8 / 0.11s / 3,139 全部我方实测（§28.9）
- [x] 84.1 我方三臂矩阵（§17）
- [x] Inco 博客邀请原话（"a model on it, including your own fine-tunes"）
- [ ] MIT license 需最终确认（集成文档记载，发信前再核 HF 页）
