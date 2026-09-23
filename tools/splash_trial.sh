#!/usr/bin/env bash
# splash_trial.sh - Splash trial and same-machine A/B (staged; run after batch).
# Plan: docs/03-experiments-testing/splash-trial-plan-20260922.md
#
# Usage:
#   ./tools/splash_trial.sh download            # download Splash pkg (no GPU; ok during batch)
#   ./tools/splash_trial.sh baseline            # arm B: rapid-mlx 27B baseline (needs idle GPU)
#   ./tools/splash_trial.sh splash              # arm A: start Splash on :8000 (needs idle GPU)
#   ./tools/splash_trial.sh bench <port> <tag>
#   ./tools/splash_trial.sh stop-splash
#   ./tools/splash_trial.sh report

set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/.."
REPO="$(pwd)"
STG=/tmp/splash-trial
LOGS="$STG/logs"
SPLASH_MODEL="incoai/Qwen3.8-27B-Splash"
SPLASH_DIR="${SPLASH_DIR:-$HOME/models/splash/Qwen3.8-27B-Splash}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_ENDPOINT

mkdir -p "$STG" "$LOGS"

info(){ printf '[INFO] %s\n' "$*"; }
warn(){ printf '[WARN] %s\n' "$*"; }
err(){  printf '[ERROR] %s\n' "$*"; }

_assert_engine_idle(){
  if pgrep -f 'run_agent' >/dev/null; then
    err "batch running -> trial forbidden (redline 1)"
    return 1
  fi
  if pgrep -f 'rapid-mlx serve' >/dev/null; then
    err "rapid-mlx engine running -> ./manage.sh stop-backend first"
    return 1
  fi
  return 0
}

cmd_download(){
  info "downloading $SPLASH_MODEL (HF_ENDPOINT=$HF_ENDPOINT)"
  info "~17.4GB / 82 files; resumable"
  python3 -m huggingface_hub.commands.huggingface_cli \
    download "$SPLASH_MODEL" --local-dir "$SPLASH_DIR" 2>&1 | tail -4
  local rc=${PIPESTATUS[0]}
  if [ -f "$SPLASH_DIR/manifest.json" ] && [ -d "$SPLASH_DIR/target" ]; then
    info "DONE. size: $(du -sh "$SPLASH_DIR" 2>/dev/null | cut -f1)"
  else
    warn "incomplete (rc=$rc); rerun to resume"
  fi
}

cmd_baseline(){
  _assert_engine_idle || return 1
  info "arm B: rapid-mlx 27B baseline"
  ./manage.sh switch qwen3.8-27b-4bit && ./manage.sh start-backend || { err "start failed"; return 1; }
  sleep 5
  "$0" bench 8081 rapidmlx-27b
}

cmd_splash(){
  _assert_engine_idle || return 1
  if [ ! -f "$SPLASH_DIR/manifest.json" ]; then
    err "Splash pkg missing -> run $0 download first"
    return 1
  fi
  info "arm A: starting Splash on :8000"
  # Splash loads by owner/repo from HF cache -> ensure cache copy exists
  python3 -m huggingface_hub.commands.huggingface_cli download \
    "$SPLASH_MODEL" >/dev/null 2>&1 || true
  nohup splash serve --model "$SPLASH_MODEL" --port 8000 \
    --default-reasoning-effort none \
    > "$LOGS/splash-serve.log" 2>&1 &
  echo $! > "$STG/.splash.pid"
  info "waiting Ready (up to 300s)..."
  local i
  for i in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 3 http://127.0.0.1:8000/status; then
      info "Splash READY (pid $(cat "$STG/.splash.pid"))"
      return 0
    fi
    sleep 5
  done
  err "Splash not ready; see $LOGS/splash-serve.log"
  tail -20 "$LOGS/splash-serve.log"
  return 1
}

cmd_stop_splash(){
  if [ -f "$STG/.splash.pid" ]; then
    kill "$(cat "$STG/.splash.pid")" 2>/dev/null && info "stopped"
    rm -f "$STG/.splash.pid"
  else
    pkill -f 'splash serve' 2>/dev/null && info "stopped (pkill)" || warn "not found"
  fi
}

cmd_bench(){ # $1=port $2=tag
  local port="${1:-8081}" tag="${2:-unknown}"
  info "bench port=$port tag=$tag"
  python3 - "$port" "$tag" <<'PY'
import json,sys,time,urllib.request,os
port,tag=sys.argv[1],sys.argv[2]
url=f"http://127.0.0.1:{port}/v1/chat/completions"
try:
    m=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",timeout=10))
    MODEL=m["data"][0]["id"]
except Exception:
    MODEL="x"
def call(content,maxtok):
    body=json.dumps({"model":MODEL,"max_tokens":maxtok,
                     "messages":[{"role":"user","content":content}]}).encode()
    req=urllib.request.Request(url,data=body,headers={"Content-Type":"application/json"})
    t0=time.perf_counter(); r=urllib.request.urlopen(req,timeout=900); d=json.loads(r.read())
    dt=time.perf_counter()-t0
    u=d.get("usage",{})
    return dt,u.get("completion_tokens",0),u.get("prompt_tokens",0)
out={"tag":tag,"port":int(port),"model":MODEL,"decode":[],"ladder":[]}
call("Warm up with one short paragraph about testing.",32)
for i in range(3):
    dt,c,p=call("Write a detailed technical essay about CPU memory caching in modern processors.",512)
    out["decode"].append({"wall_s":round(dt,2),"comp":c,"tok_s":round(c/dt,1) if dt else 0})
filler=" ".join(f"line{i} token{i} alpha beta gamma delta epsilon zeta" for i in range(1300))
for name,txt in (("16K",filler),("32K"," ".join([filler]*2))):
    dt,c,p=call(txt+" Summarize in one word.",32)
    dt2,c2,p2=call(txt+" Summarize in one word.",32)
    out["ladder"].append({"bucket":name,"cold_ttft_s":round(dt,2),"prompt":p,
                          "warm_ttft_s":round(dt2,2),
                          "prefill_tok_s":round(p/dt,1) if dt else 0})
os.makedirs("/tmp/splash-trial/logs",exist_ok=True)
open(f"/tmp/splash-trial/logs/bench-{tag}.json","w").write(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out,ensure_ascii=False,indent=2))
PY
}

cmd_report(){
  info "comparison report"
  python3 - <<'PY'
import json,glob
def load(pat):
    g=glob.glob(pat)
    try: return json.load(open(g[0]))
    except Exception: return None
b=load("/tmp/splash-trial/logs/bench-rapidmlx-27b.json")
s=load("/tmp/splash-trial/logs/bench-splash-27b.json")
print("="*72)
print(f"{'metric':<30}{'rapid-mlx':>18}{'Splash':>18}")
print("-"*72)
def row(label,fn):
    bv=fn(b) if b else None; sv=fn(s) if s else None
    print(f"{label:<30}{str(bv):>18}{str(sv):>18}")
row("decode short tok/s(avg)", lambda d:round(sum(x['tok_s'] for x in d['decode'])/len(d['decode']),1) if d else None)
for i,name in ((0,"16K"),(1,"32K")):
    row(f"{name} cold TTFT s", lambda d,i=i:d['ladder'][i]['cold_ttft_s'] if d else None)
    row(f"{name} warm TTFT s", lambda d,i=i:d['ladder'][i]['warm_ttft_s'] if d else None)
    row(f"{name} prefill tok/s", lambda d,i=i:d['ladder'][i]['prefill_tok_s'] if d else None)
print("="*72)
PY
}

case "${1:-help}" in
  download)    cmd_download ;;
  baseline)    cmd_baseline ;;
  splash)      cmd_splash ;;
  bench)       shift; cmd_bench "$@" ;;
  stop-splash) cmd_stop_splash ;;
  report)      cmd_report ;;
  *) sed -n '2,20p' "$0" ;;
esac
