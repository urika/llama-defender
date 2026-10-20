#!/usr/bin/env python3
"""stakeout4: model_not_found burst 抓现行。日志落 logs/（防 /tmp 清理）。"""
import time, subprocess, os, collections
LOG = "/Users/jinsongwang/APP/llama.cpp/logs/stakeout4_8081.log"
LSERVER = "/Users/jinsongwang/APP/llama.cpp/logs/llama-server.log"
def count():
    c = 0
    try:
        with open(LSERVER, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "model_not_found" in line: c += 1
    except FileNotFoundError: pass
    return c
def run(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e: return f"<err {e}>"
base = count(); last = base
ring = collections.deque(maxlen=60)  # 2s 间隔 = 2 分钟窗口
last_snap = 0
with open(LOG, "a") as out:
    out.write(f"[stakeout4] start {time.strftime('%F %H:%M:%S')} baseline={base} pid={os.getpid()}\n"); out.flush()
    while True:
        now = time.time()
        if now - last_snap >= 2:
            ring.append((time.strftime('%H:%M:%S'), run(["ps","-ax","-o","pid,ppid,lstart,command"])))
            last_snap = now
        c = count()
        if c != last:
            out.write(f"\n### {time.strftime('%F %H:%M:%S')} BURST {last}->{c} (+{c-last})\n")
            out.write(f"--- lsof :8081 FULL ---\n{run(['lsof','-i',':8081'])}\n")
            for ts, snap in ring:
                sus = "\n".join(l[:300] for l in snap.split("\n")
                                if l.strip() and ("ython" in l or "curl" in l or "node " in l or "swe" in l.lower())
                                and all(k not in l for k in ("stakeout","mcp-server","serper","jev_mcp","searxng","cc-connect","ZCode","dsh")))
                if sus: out.write(f"--- ring {ts} ---\n{sus}\n")
            out.flush(); last = c
        time.sleep(0.15)
