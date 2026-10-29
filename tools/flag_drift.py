"""配置漂移检测（纯函数层）——**conf 声明的旗标 vs 运行进程实际启动旗标**。

## 为什么需要

2026-10-29 实测两次同型事故，都是「conf 改了但进程没生效」：

1. **br4 录制面**（10-22 / 10-27）：换模重建 conf 丢了 `PROXY_REQUEST_ARCHIVE_ENABLED`，
   capability_audit 只看 conf → 报 PRESENT，但进程 env 里是 false → 录制面失明。
2. **ctx_recall 死环**（10-29）：conf `08:40:01` 已置 `PROXY_PD_MICRO_TURN_ENABLED=true`，
   代理 `08:39:39` 启动（早 22 秒）→ 进程一直是 false → 工具被广告但无人应答 →
   客户端连回 `No such tool available`，模型「从 session store 恢复」路径死掉并成环。

两者的共性是：**conf 面自洽、运行面陈旧**。capability_audit 覆盖不到这一层，故补本检测：
直接拿 `ps -wwE -p <代理 pid>` 的进程环境与 active.conf 逐键比对。

## 判据（只比 conf 里**显式声明**的键）

- conf 有、进程无 → 漂移（进程按缺省值跑）
- conf 有、进程值不同 → 漂移
- 进程有、conf 无 → **不算漂移**（可能是 manage.sh 注入的派生值，噪声源，不报）

## 修复路径分类

- 键在 `proxy_state._RELOAD_SPEC` → `./manage.sh reload` 即可（SIGHUP 热改）
- 否则 → **必须重启代理**（代码/旗标在启动时定型）
"""
from __future__ import annotations


def _norm(val):
    """归一化比较用：bool/int/float 按字符串小写比，None → ''。"""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip().strip('"').strip("'").lower()


def parse_process_env(ps_output):
    """从 `ps -wwE -p <pid>` 的单行输出解析进程环境。

    macOS 的 -E 把环境变量追加在命令行之后，形如
    `... /path/python3 anthropic_proxy.py FOO=1 BAR=2`。
    只收 `^[A-Z_][A-Z0-9_]*=` 形态的 token，避免把命令行参数误当变量。
    """
    env = {}
    for tok in (ps_output or "").split():
        if "=" not in tok:
            continue
        key, val = tok.split("=", 1)
        if not key or not key[0].isupper():
            continue
        if not all(c.isupper() or c.isdigit() or c == "_" for c in key):
            continue
        env[key] = val
    return env


def diff(conf, proc, reloadable_keys=()):
    """比对 conf 与进程环境，返回漂移列表（按严重度排序：需重启的在前）。

    参数
      conf            : dict（active.conf 解析结果，只含显式声明的键）
      proc            : dict（`parse_process_env` 结果）
      reloadable_keys : 可由 `manage.sh reload` 热修的键集合

    返回 [{"key", "conf", "proc", "fix"}]，fix ∈ {"reload", "restart"}
    """
    reloadable = set(reloadable_keys or ())
    out = []
    for key, cval in (conf or {}).items():
        if not key.startswith(("PROXY_", "LLAMA_", "BACKEND_TYPE", "MODEL_NAME")):
            continue
        pval = proc.get(key)
        if _norm(cval) == _norm(pval):
            continue
        out.append({
            "key": key,
            "conf": _norm(cval),
            "proc": _norm(pval) if pval is not None else "(未设置=缺省)",
            "fix": "reload" if key in reloadable else "restart",
        })
    out.sort(key=lambda d: (d["fix"] != "restart", d["key"]))
    return out


def summarize(drift):
    """一行摘要（供日志/告警）。"""
    if not drift:
        return ""
    need_restart = [d["key"] for d in drift if d["fix"] == "restart"]
    need_reload = [d["key"] for d in drift if d["fix"] == "reload"]
    parts = []
    if need_restart:
        parts.append(f"需重启 {len(need_restart)} 项({', '.join(need_restart[:3])}"
                     f"{'…' if len(need_restart) > 3 else ''})")
    if need_reload:
        parts.append(f"可 reload {len(need_reload)} 项({', '.join(need_reload[:3])}"
                     f"{'…' if len(need_reload) > 3 else ''})")
    return "conf 与运行进程漂移：" + "；".join(parts)
