"""监控滚动窗口速率外推（最小二乘）——纯函数层，供单测覆盖。

背景（2026-10-27）：原实现用「相邻两次快照差分」外推 headroom 耗尽时间，
5 分钟采样下噪声过大 → 两次误报（10-27 前：「0.3h 耗尽」「0.4h 耗尽」均
未发生；成因＝Splash 周期性回收使 headroom 呈锯齿，单窗差分斜率非单调）。

改法：30 分钟滚动窗口最小二乘 + 最小跨度闸（样本跨度不足**不外推**——
宁可不报，不误报）。本模块只做数学，不碰采集与判级（判级在
`tools/monitor_service.sh` 的分析段）。

样本列约定：(ts_epoch, headroom_gb, memory_denied)
"""
from __future__ import annotations

# 告警等级序（OK < INFO < WARN < CRIT）——**不可用字符串 max() 比较**：
# 原实现 `max(level, "WARN")` 走字典序，"CRIT" > "WARN" 为假 → CRIT 被
# WARN 覆盖 → 告警行级别错标（2026-10-27 实测「CRIT | 内存压力 warning」）。
SEV = {"OK": 0, "INFO": 1, "WARN": 2, "CRIT": 3}


def sev_max(a, b):
    """等级取严重度更高者。"""
    return a if SEV.get(a, 0) >= SEV.get(b, 0) else b


def slope(samples, idx):
    """最小二乘斜率（每小时变化量）+ 样本跨度（分钟）。

    samples: [(ts_epoch, v1, v2), ...]（按 ts 升序）
    idx:     列索引（1=headroom_gb, 2=memory_denied）
    返回 (slope_per_hour | None, span_min)；样本不足/退化 → (None, span)
    """
    if not samples or len(samples) < 2:
        return None, 0.0
    t0 = float(samples[0][0])
    xs = [(float(h[0]) - t0) / 3600.0 for h in samples]
    ys = [float(h[idx]) for h in samples]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    span_min = (float(samples[-1][0]) - float(samples[0][0])) / 60.0
    if den <= 1e-12:
        return None, span_min
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / den, span_min


def update_history(prev_hist, now, headroom, denied,
                   window_min=30.0, max_samples=48):
    """合并历史样本 + 本轮样本 → 按窗口裁剪后的样本列表。

    窗口保留 2 倍 window_min 的原始样本再按 max_samples 截尾——外推只用
    窗口内样本，多留一段是为了跨越窗口边界时仍有足够跨度。

    **计数器复位处理**：denied 是引擎侧单调计数器，引擎重启即归零；旧样本
    与复位后的新样本混算会得出荒谬斜率（实测 -26465/h）→ 检测到复位即清窗。
    """
    hist = []
    for h in (prev_hist or []):
        try:
            hist.append((float(h[0]), float(h[1]), float(h[2])))
        except (TypeError, ValueError, IndexError):
            continue
    hist.sort(key=lambda h: h[0])
    if hist and float(denied) < hist[-1][2]:
        hist = []          # 计数器复位（引擎重启）→ 丢弃跨复位样本
    hist.append((float(now), float(headroom), float(denied)))
    hist.sort(key=lambda h: h[0])
    cut = float(now) - float(window_min) * 120.0
    return [h for h in hist if h[0] >= cut][-int(max_samples):]


def headroom_eta(history, headroom, min_span_min=15.0, min_drain=0.2):
    """headroom 耗尽外推。

    返回 {drain_per_h, eta_h, span_min, sufficient}
    - span < min_span_min → sufficient=False, eta_h=None（不外推）
    - drain <= min_drain   → sufficient=True, eta_h=None（基本没在掉）
    """
    s, span = slope(history, 1)
    out = {"drain_per_h": None, "eta_h": None,
           "span_min": round(span, 1), "sufficient": False}
    if s is None or span < min_span_min:
        return out
    drain = -s
    out["sufficient"] = True
    if drain <= min_drain:
        return out
    out["drain_per_h"] = round(drain, 2)
    out["eta_h"] = round(float(headroom) / drain, 1)
    return out


def denied_rate(history, min_span_min=15.0):
    """denied 每小时增量（滚动窗口斜率）。

    跨度不足 min_span_min → rate_per_h=None（不外推）：denied 增长是**突发**
    的（单分钟可 +350），1–2 个样本的差分会算出上万/h 的荒谬速率
    （2026-10-27 实测 79710/h）。返回 {rate_per_h, span_min}。
    """
    s, span = slope(history, 2)
    if s is None or span < min_span_min:
        return {"rate_per_h": None, "span_min": round(span, 1)}
    return {"rate_per_h": round(s, 1), "span_min": round(span, 1)}


# ---------------------------------------------------------------------------
# 重启决策（2026-10-27：把「批跑收官清场」与「当前持续劣化」拆成两个判据）
#
# 背景：2026-10-26 批跑收官后服务带残留劣化（denied 765 累计、TTFT p95 9.8s、
# decode 72.5 tok/s）空转近 2 小时无人处理，swap 一路涨到 10.4GB。旧实现
# 只有一条判据（累计 denied>500 且 batch=False）且未启用（MONITOR_AUTO_RESTART
# 恒 0），既没触发、也没有冷却/退避（若触发且重启失败会每 5 分钟重试＝风暴）。
# ---------------------------------------------------------------------------

def degraded_now(snap, denied_rate_th=300.0, headroom_g=8.0, ttft_p95_s=30.0):
    """当前是否**仍在**劣化（速率口径，不用累计计数）。

    返回 (bool, 原因)。累计 denied 是「历史累积」信号（批跑期必然高），
    不能用来判「现在还坏着」——那是清场判据的事。
    """
    rate = snap.get("denied_rate_per_h")
    if rate is not None and rate > denied_rate_th:
        return True, "denied_rate"
    hr = snap.get("memory_headroom_gb")
    if hr is not None and hr < headroom_g:
        return True, "headroom"
    p95 = (snap.get("ttft_p95_ms") or 0) / 1000.0
    if p95 > ttft_p95_s:
        return True, "ttft_p95"
    return False, ""


def recycle_needed(prev_batch, now_batch, cum_denied, th=500):
    """批跑收官**一次性清场**：上轮在批跑、本轮不在，且本批累计拒绝量超阈。

    命中即重启一次（清 KV/state、denied 归零、headroom 回位）。
    """
    if prev_batch is True and now_batch is False and (cum_denied or 0) > th:
        return True
    return False


def restart_allowed(now_ts, last_restart_ts, attempts,
                    cooldown_s=900, max_attempts=3):
    """重启许可闸：冷却期 + 最大尝试次数（防重启风暴）。

    返回 (bool, 原因)。
    """
    if attempts >= max_attempts:
        return False, "已达最大尝试次数"
    if last_restart_ts and (now_ts - last_restart_ts) < cooldown_s:
        return False, "冷却期内"
    return True, ""


def swap_alert(swap_used_mb, swap_total_mb, warn_mb=6144):
    """系统级 swap 判据——**真实根因所在**（引擎重启救不了 swap）。

    返回 (level, msg)：level ∈ {None, "WARN", "CRIT"}。
    """
    if not swap_used_mb:
        return None, ""
    if swap_total_mb and swap_used_mb >= swap_total_mb * 0.9:
        return "CRIT", (f"swap 近满 {swap_used_mb}MB/{swap_total_mb}MB —— "
                        f"系统内存账本已耗尽：引擎重启救不了，需重启系统")
    if swap_used_mb > warn_mb:
        return "WARN", (f"swap 已用 {swap_used_mb}MB（阈值 {warn_mb}MB）—— "
                        f"内存准入拒绝(denied)的上游根因，建议择机重启系统")
    return None, ""
