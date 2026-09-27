# Inco AI 跟进邮件草稿（2026-09-27 预备，S-11）

> **用途**: 9/23 打包请求（Ornith-1.5-35B-A3B Splash 格式，见 `inco-contact-email-draft-20260923.md`）
> 至 **10-09（10 工作日）** 仍未回信时按本稿跟进。用户亦可提前发送。
> **发送前待办**: ①补落款（9/23 原稿两处未闭环之一：姓名/角色/联系方式）②确认是否随附 benchmark JSON（原稿未闭环之二）。

---

**Subject**: RE: Splash packaging request — Ornith-1.5-35B-A3B (follow-up)

Hi Inco team,

Following up on our request from September 23rd regarding Splash packaging
support for **Ornith-1.5-35B-A3B** (a Qwen3.6-35B-A3B MoE+GDN hybrid).

We remain very interested in evaluating Splash's speculative decoding stack on
this model — our internal benchmarks on Qwen3.6-35B-A3B-Splash showed strong
results (prose 117 tok/s, code 264.8 tok/s, warm TTFT at 32K ≈ 0.10s), and we'd
like to quantify the same for Ornith-1.5-35B-A3B with its native tokenizer and
chat template.

Two questions in particular:
1. Is there an estimated timeline for onboarding this checkpoint?
2. Would a benchmark JSON from our side (SWE-bench-style agentic traces +
   throughput measurements) help prioritize the work? Happy to share.

Thanks again — looking forward to hearing from you.

Best regards,
[姓名 / 角色 / 联系方式——发送前补]
