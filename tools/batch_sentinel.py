#!/usr/bin/env python3
"""batch_sentinel.py — b2 批期只读哨兵（2026-10-05 用户指令执行）。

纯观测、零动作（无 kill/无重启）：每 INTERVAL 秒采样代理/引擎/内存/swap，
追加 JSONL 到哨兵日志；越阈打 WARN/CRIT 行。判据出处=AGENTS.md 队列健康
（积压棘轮 pos>5 或 oldest>15min）+ L-54 后 splash 栈守望口径（无 rapid_mlx
计数 ⇒ 用 splash_memory_pressure/metal_healthy 代位）+ 探针误报消解
（/api/status 快路径实测 ~0.6s）。

用法: nohup python3 tools/batch_sentinel.py --interval 1800 --log <path> >> <log>.nohup 2>&1 &
停止: kill $(cat <pidfile>)
"""
import argparse, json, subprocess, sys, time, urllib.request

def get(url, timeout=5):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read()), round((time.time() - t0) * 1000)
    except Exception as e:
        return {"_error": str(e)}, round((time.time() - t0) * 1000)

def engine_metrics():
    out = {}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/metrics", timeout=5) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("splash_memory_pressure"):
                    if 'state="warning"' in line: out["pressure_warning"] = int(line.split()[-1])
                    elif 'state="critical"' in line: out["pressure_critical"] = int(line.split()[-1])
                elif line.startswith("splash_metal_healthy"): out["metal_healthy"] = int(line.split()[-1])
                elif line.startswith("splash_requests_cancelled_total"): out["cancelled_total"] = int(line.split()[-1])
                elif line.startswith("splash_requests_failed_total"): out["failed_total"] = int(line.split()[-1])
                elif line.startswith("splash_scheduler_decoding"): out["decoding"] = int(line.split()[-1])
    except Exception as e:
        out["_error"] = str(e)
    return out

def swap_mb():
    try:
        s = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        for part in s.replace(",", " ").split():
            if part.endswith("M") and part[0].isdigit():
                pass
        # "total = 5120.00M used = 4077.81M free = 1042.19M"
        seg = dict(zip([x for x in s.replace(",", " ").split() if not x.isdigit() and x not in ("=",)][::1], []))
        used = s.split("used =")[1].split("M")[0].strip()
        total = s.split("total =")[1].split("M")[0].strip()
        return round(float(used)), round(float(total))
    except Exception:
        return None, None

def sample():
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    alerts = []
    st, lat = get("http://127.0.0.1:4000/api/status")
    rec["api_status_ms"] = lat
    rec["state"] = st.get("state")
    rec["ready"] = st.get("ready")
    if st.get("_error") or rec["state"] != "healthy" or not rec["ready"]:
        alerts.append(("CRIT", f"api_status state={rec.get('state')} ready={rec.get('ready')} err={st.get('_error')}"))
    elif lat > 2000:
        alerts.append(("WARN", f"api_status latency {lat}ms > 2000"))
    q, _ = get("http://127.0.0.1:4000/api/queue")
    rec["queue_waiting"] = q.get("waiting"); rec["queue_oldest_ms"] = q.get("oldest_wait_ms")
    if (q.get("waiting") or 0) > 5: alerts.append(("WARN", f"queue waiting={q.get('waiting')} >5 (积压棘轮)"))
    if (q.get("oldest_wait_ms") or 0) > 900000: alerts.append(("WARN", f"queue oldest {q.get('oldest_wait_ms')}ms >15min"))
    m = engine_metrics(); rec.update(m)
    if m.get("pressure_critical"): alerts.append(("CRIT", "splash memory pressure=CRITICAL → 按批次处置链"))
    elif m.get("pressure_warning"): alerts.append(("WARN", "splash memory pressure=warning（盯升级）"))
    if m.get("metal_healthy") == 0: alerts.append(("CRIT", "metal_healthy=0"))
    if "_error" in m: alerts.append(("WARN", f"engine metrics: {m['_error']}"))
    u, t = swap_mb(); rec["swap_used_mb"] = u; rec["swap_total_mb"] = t
    if u and t and u / t > 0.9: alerts.append(("WARN", f"swap {u}/{t}MB >90%"))
    return rec, alerts

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1800)
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    prev_cancelled = None
    while True:
        rec, alerts = sample()
        if prev_cancelled is not None and "cancelled_total" in rec:
            d = rec["cancelled_total"] - prev_cancelled
            rec["cancelled_delta"] = d
            if d > 50: alerts.append(("WARN", f"cancelled +{d}/interval（空闲看门狗高频裁剪，盯 decode 速率）"))
        prev_cancelled = rec.get("cancelled_total")
        line = json.dumps(rec, ensure_ascii=False)
        with open(a.log, "a") as f:
            f.write(line + "\n")
            for lvl, msg in alerts:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{lvl}] {msg}\n")
                print(f"[{lvl}] {msg}", file=sys.stderr)
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
