#!/usr/bin/env python3
"""exp9_watch.py — EXP-9 批期值守（纯只读 + 证据归档拷贝，零批期干预）。

每轮动作（联合协调批期红线兼容）：
  1. 驱动存活（swe_test / run_agent 进程）+ 最新 turn 新鲜度（>30min 判可疑）
  2. L-30 信号量审计（ipcs -s，>100 告警）
  3. 日志水位（后端/代理/sessions.jsonl）
  4. **sessions.jsonl 轮转防护**：增长 >2MB 或检测到轮转（体积回落）即归档拷贝
     到 logs/diag/evidence-exp9/（轮转只留 1 备份，不归档会永久丢 EXP-9 早期段）
  5. 驱动消失 = 收官信号 → 全量终扫归档并报告

状态存 logs/diag/evidence-exp9/.watch_state.json。退出码：0 正常，1 有告警，
2 收官信号。定时消费方按输出行前缀（ALERT/收官/OK）解读。
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EV_DIR = os.path.join(ROOT, "logs", "diag", "evidence-exp9")
STATE = os.path.join(EV_DIR, ".watch_state.json")
SESSIONS = os.path.join(ROOT, "logs", "diag", "sessions.jsonl")
WATCHED = {
    "backend": os.path.join(ROOT, "logs", "llama-server.log"),
    "proxy": os.path.join(ROOT, "logs", "anthropic_proxy.log"),
    "sessions": SESSIONS,
}
ARCHIVE_GROW_MB = 2
TURN_STALE_MIN = 30
SEM_ALERT = 100


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(EV_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, ensure_ascii=False)


def driver_alive():
    for pat in ("swe_test", "run_agent"):
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            if r.stdout.strip():
                return pat, r.stdout.split()[0]
        except OSError:
            pass
    return None, None


def latest_turn_ts():
    try:
        with open(SESSIONS, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            m = re.search(r'"ts": "([^"]+)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def ts_age_min(ts):
    try:
        t = datetime.fromisoformat(ts)
        return (datetime.now() - t).total_seconds() / 60
    except ValueError:
        return None


def sem_count():
    try:
        r = subprocess.run(["ipcs", "-s"], capture_output=True, text=True)
        return max(0, len(r.stdout.strip().splitlines()) - 3)
    except OSError:
        return -1


def archive(tag, path):
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = os.path.join(EV_DIR, "%s-%s-%s" % (
        os.path.basename(path).replace(".jsonl", ""), tag, stamp + ".jsonl"))
    os.makedirs(EV_DIR, exist_ok=True)
    shutil.copy2(path, dst)
    return dst


def main():
    lines, alerts = [], []
    st = load_state()

    # 1. 驱动与 turn 新鲜度
    pat, pid = driver_alive()
    turn_ts = latest_turn_ts()
    age = ts_age_min(turn_ts) if turn_ts else None
    if pat:
        lines.append("驱动在途: %s (pid %s)" % (pat, pid))
        if age is not None and age > TURN_STALE_MIN:
            alerts.append("最新 turn 距今 %.0f 分钟（>%d，疑似卡住）" % (age, TURN_STALE_MIN))
        lines.append("最新 turn: %s (%.0f min 前)" % (turn_ts, age or -1))
    else:
        lines.append("驱动进程未发现（swe_test/run_agent 均无）")
        if st.get("driver_seen"):
            # 首次发现消失 → 终扫
            for k, p in WATCHED.items():
                dst = archive("final", p)
                if dst:
                    lines.append("终扫归档[%s]: %s" % (k, os.path.basename(dst)))
            lines.append("收官信号: 之前在途、现已退出——EXP-9 疑似收官，"
                         "请按 runbook §0 复核并通知 swe-eval 侧（值守任务可删）")
            save_state({"driver_seen": False, "t": time.time()})
            print("\n".join(lines))
            return 2

    # 2. 信号量
    n_sem = sem_count()
    lines.append("POSIX sem: %d" % n_sem)
    if n_sem > SEM_ALERT:
        alerts.append("信号量 %d 条（>%d，L-30 风险）" % (n_sem, SEM_ALERT))

    # 3. 水位
    sizes = {}
    for k, p in WATCHED.items():
        try:
            sizes[k] = os.path.getsize(p)
        except OSError:
            sizes[k] = -1
    lines.append("水位: backend=%.0fM proxy=%.0fM sessions=%.1fM" % (
        sizes["backend"] / 1e6, sizes["proxy"] / 1e6, sizes["sessions"] / 1e6))

    # 4. sessions.jsonl 轮转防护
    cur = sizes["sessions"]
    prev = st.get("sessions_size")
    archived = False
    if prev is not None and cur < prev - 1024 * 1024:
        # 体积回落 → 已轮转：立即抢救 .1（即将被下次轮转覆盖）+ 当前段
        d1 = archive("rot", SESSIONS + ".1")
        d0 = archive("post-rot", SESSIONS)
        lines.append("轮转检出! 已抢救: %s + %s" % (
            os.path.basename(d1 or "?"), os.path.basename(d0 or "?")))
        archived = True
    elif cur - st.get("sessions_archived_at_size", 0) > ARCHIVE_GROW_MB * 1024 * 1024:
        d = archive("incr", SESSIONS)
        if d:
            lines.append("增量归档: %s" % os.path.basename(d))
            st["sessions_archived_at_size"] = cur
            archived = True

    st.update({"driver_seen": bool(pat), "sessions_size": cur, "t": time.time()})
    save_state(st)
    if alerts:
        for a in alerts:
            lines.append("ALERT: " + a)
        print("\n".join(lines))
        return 1
    lines.insert(0, "OK")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys_code = main()
    raise SystemExit(sys_code)
