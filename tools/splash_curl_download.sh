#!/usr/bin/env bash
# splash_curl_download.sh - curl-based resumable downloader for the Splash package.
# Works around huggingface_hub 0.30.2 mirror incompatibility (LocalEntryNotFoundError).
# Verifies SHA-256 per file against the manifest. Safe to rerun (resume).
set -uo pipefail
STG=/tmp/splash-trial
DEST="${SPLASH_DIR:-$HOME/models/splash/Qwen3.8-27B-Splash}"
LIST="$STG/filelist.tsv"
mkdir -p "$STG" "$DEST"

ok=0; skip=0; fail=0; failnames=""
total_files=$(wc -l < "$LIST" | tr -d ' ')
i=0
while IFS=$'\t' read -r path url size sha; do
  i=$((i+1))
  out="$DEST/$path"
  mkdir -p "$(dirname "$out")"
  # resume support: -C - continues partial
  if [ -f "$out" ]; then
    cur=$(stat -f%z "$out" 2>/dev/null || echo 0)
    if [ "$cur" = "$size" ]; then
      skip=$((skip+1))
      continue
    fi
  fi
  printf '[%d/%d] %s ... ' "$i" "$total_files" "$path"
  curl -sL --max-time 1800 -C - -o "$out" "$url"
  rc=$?
  cur=$(stat -f%z "$out" 2>/dev/null || echo 0)
  if [ "$rc" != "0" ] || [ "$cur" != "$size" ]; then
    echo "FAIL (rc=$rc size=$cur/$size)"
    fail=$((fail+1)); failnames="$failnames $path"
  else
    # verify sha256 if present
    if [ -n "$sha" ]; then
      got=$(shasum -a 256 "$out" | awk '{print $1}')
      if [ "$got" != "$sha" ]; then
        echo "SHA-MISMATCH"
        fail=$((fail+1)); failnames="$failnames $path"
        continue
      fi
    fi
    echo "ok ($(du -h "$out" | cut -f1))"
    ok=$((ok+1))
  fi
done < "$LIST"

echo "----------------------------------------"
echo "done: ok=$ok skip=$skip fail=$fail"
[ -n "$failnames" ] && echo "failed files:$failnames"
[ "$fail" = "0" ] && echo "ALL COMPLETE" && exit 0 || exit 1
