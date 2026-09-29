#!/usr/bin/env bash
# ============================================================
# state_sentinel.sh — 状态泄漏哨兵(元测试)。
#
# 套件前后各调一次, 捕获"测试框架不得污染生产 diag 状态"的违例:
#   begin: ①先清**历史残留**(上一轮泄漏/旧版清理缺陷留下的 itest*/pfshadow*)
#             并打印清单——把"历史残留"与"本轮新泄漏"分离, 使哨兵只对后者报警
#         ②记录 sessions.jsonl 行数基线
#   end:   ①确定性: 测试会话存储文件必须已被清理(trap 失效即 fail)
#                 覆盖 itest*(集成测试) + pfshadow*(promptfoo 影子代理)
#          ②宽松:   sessions.jsonl 窗口内非测试增量 → WARN
#                   (实验批跑等生产流量会合法增长, 无法与测试泄漏区分;
#                    严格模式待生产流量静默窗口再启用)
#
# L-58(2026-09-29): 旧版无 begin 清理 ⇒ **一次泄漏(如 5 个用例的 cleanup SID 与
# 请求头 SID 错配)即让套件**永久红, 把后续真实回归掩盖在常红里(与 L-46 同族);
# 且扫描面漏 `orig/`(diag_cleanup 会清它、哨兵却不看它) ⇒ 该区泄漏长期不可见。
# 两处均已修: 扫描面与 diag_cleanup 的五个会话键控区对齐 + begin 分离历史残留。
#
# 用法(run_tests.sh 各 tier 内):
#   source "$SCRIPT_DIR/lib/state_sentinel.sh"
#   state_sentinel_begin
#   ... 测试 ...
#   state_sentinel_end   # 违例时非零返回
# ============================================================

# 会话键控存储的五个区(与 test/lib/diag_cleanup.sh 的清理面对齐——L-58 前漏 orig)
_SENTINEL_DIAG_DIRS=(manifest index archive orig ledger)
_SENTINEL_TEST_PREFIXES=(itest pfshadow)

_sentinel_residue_files() {
    local root="${REPO_ROOT:-.}" dir prefix f hits=()
    for dir in "${_SENTINEL_DIAG_DIRS[@]}"; do
        for prefix in "${_SENTINEL_TEST_PREFIXES[@]}"; do
            while IFS= read -r f; do
                [[ -n "$f" ]] && hits+=("$f")
            done < <(ls "$root/logs/diag/$dir/$prefix"* 2>/dev/null || true)
        done
    done
    (( ${#hits[@]} )) && printf '%s\n' "${hits[@]}"
    return 0
}

_SENTINEL_BASELINE_FILE=""

state_sentinel_begin() {
    local root="${REPO_ROOT:-.}"
    local stale
    stale="$(_sentinel_residue_files)"
    if [[ -n "$stale" ]]; then
        echo "  [SENTINEL] 清历史残留(itest*/pfshadow*, 非本轮泄漏——旧版清理缺陷遗留):"
        echo "$stale" | sed 's/^/      /'
        while IFS= read -r f; do
            [[ -n "$f" ]] && rm -f "$f"
        done <<< "$stale"
    fi
    _SENTINEL_BASELINE_FILE="$(mktemp)"
    local sess="$root/logs/diag/sessions.jsonl"
    if [[ -f "$sess" ]]; then
        wc -l < "$sess" | tr -d ' ' > "$_SENTINEL_BASELINE_FILE"
    else
        echo 0 > "$_SENTINEL_BASELINE_FILE"
    fi
}

state_sentinel_end() {
    local repo_root="${REPO_ROOT:-.}"
    local rc=0

    # ① 确定性: 测试会话存储残留(trap 清理失效的直接证据)
    #    覆盖: itest*(集成测试) + pfshadow*(promptfoo 影子代理, 2026-09-06)
    #    区面: manifest/index/archive/orig/ledger 五区(L-58 起含 orig, 与
    #    diag_cleanup.sh 清理面一致)
    local leftovers
    leftovers="$(_sentinel_residue_files)"
    if [[ -n "$leftovers" ]]; then
        echo "  [SENTINEL][FAIL] 测试残留会话状态 itest*/pfshadow*(清理 trap 未生效):"
        echo "$leftovers" | sed 's/^/      /'
        rc=1
    fi

    # ② 宽松: sessions.jsonl 窗口增量的非测试行(生产流量同样会命中 → WARN)
    local sess="logs/diag/sessions.jsonl"
    if [[ -f "$sess" && -f "$_SENTINEL_BASELINE_FILE" ]]; then
        local before after new_lines non_itest
        before=$(cat "$_SENTINEL_BASELINE_FILE")
        after=$(wc -l < "$sess" | tr -d ' ')
        new_lines=$(( after - before ))
        if (( new_lines > 0 )); then
            non_itest=$(tail -n "$new_lines" "$sess" | grep -cvE "itest|pfshadow" || true)
            if (( non_itest > 0 )); then
                echo "  [SENTINEL][WARN] sessions.jsonl 窗口增量含 $non_itest 条非 itest 行"
                echo "      (可能为生产流量; 与测试泄漏不可区分, 不计失败)"
            fi
        fi
    fi
    [[ -n "$_SENTINEL_BASELINE_FILE" ]] && rm -f "$_SENTINEL_BASELINE_FILE"
    return $rc
}
