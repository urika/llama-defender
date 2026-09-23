#!/usr/bin/env bash
# splash_par_download.sh - parallel resumable downloader (worker script version).
# Avoids export -f/herestring issues: the worker reads one TSV line via env vars.
set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"
STG=/tmp/splash-trial
DEST="${SPLASH_DIR:-$HOME/models/splash/Qwen3.8-27B-Splash}"
LIST="$STG/filelist.tsv"
LOG="$STG/par-download.log"
WORKER="$STG/worker.sh"
mkdir -p "$STG" "$DEST" "$(dirname "$LOG")"

# --- worker: reads LINE (one TSV row) from environment ---
cat > "$WORKER" <<'WOF'
#!/usr/bin/env bash
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"
LINE="$1"
IFS=$'\t' read -r path url size sha <<< "$LINE"
out="$SPLASH_DEST/$path"
mkdir -p "$(dirname "$out")"
if [ -f "$out" ]; then
  cur=$(stat -f%z "$out" 2>/dev/null || echo 0)
  [ "$cur" = "$size" ] && { echo "[skip] $path"; exit 0; }
fi
curl -sL --max-time 3600 --speed-limit 51200 --speed-time 30 \
     --retry 5 --retry-delay 5 -C - -o "$out" "$url"
rc=$?
cur=$(stat -f%z "$out" 2>/dev/null || echo 0)
if [ "$rc" != "0" ] || [ "$cur" != "$size" ]; then
  echo "[FAIL rc=$rc] $path ($cur/$size)"
  exit 1
fi
if [ -n "$sha" ]; then
  got=$(shasum -a 256 "$out" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then
    echo "[SHA-MISMATCH] $path"; exit 1
  fi
fi
echo "[ok] $path ($(du -h "$out" | cut -f1))"
WOF
chmod +x "$WORKER"

export SPLASH_DEST="$DEST"
rm -f "$STG/.results"
tail -n +1 "$LIST" | while IFS= read -r line; do
  [ -z "$line" ] && continue
  printf '%s\n' "$line"
done | xargs -I LINE -P 4 "$WORKER" "LINE" >> "$LOG" 2>&1

echo "==== PARALLEL DONE ===="
ok=$(grep -c '^\[ok\]\|^\[skip\]' "$LOG" 2>/dev/null || echo 0)
fails=$(grep -c '^\[FAIL\|^\[SHA' "$LOG" 2>/dev/null || echo 0)
echo "ok/skip=$ok fails=$fails"
[ "$fails" = "0" ] && echo "ALL COMPLETE"
