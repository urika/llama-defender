#!/usr/bin/env bash
# ============================================================
# A/B 对比实验执行脚本
# 支持: prepare / collect / report 三个阶段
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
EXPERIMENT_DIR="$PROJECT_DIR/logs/experiments"
mkdir -p "$EXPERIMENT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

META_FILE="$EXPERIMENT_DIR/current_meta.json"

cmd_prepare() {
    local group=""
    local task=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --group|-g) group="$2"; shift 2 ;;
            --task|-t)  task="$2";  shift 2 ;;
            *) error "未知参数: $1"; exit 1 ;;
        esac
    done
    if [[ -z "$group" || -z "$task" ]]; then
        error "用法: $0 prepare --group A|B --task '任务描述'"
        exit 1
    fi
    local ts; ts=$(date +%Y%m%d-%H%M%S)
    local exp_id="${group}-${ts}"

    # A/B 组 clearing 配置（两组都用 Cloud 模式，唯一变量是 clearing）
    local clear_enabled=""
    local clear_threshold=""
    local tool_keep=""
    local ctx_limit=""
    local ctx_limit_val=""
    if [[ "$group" == "A" ]]; then
        # A 组: 强制开启 clearing（模拟本地模式的约束）
        clear_enabled="true"
        clear_threshold="15000"
        tool_keep="2"
        ctx_limit="true"
        ctx_limit_val="180000"
        info "A 组配置: Cloud + clearing 开启 (threshold=$clear_threshold, keep=$tool_keep)"
    else
        # B 组: 使用云模式默认值（clearing 关闭）
        clear_enabled="false"
        clear_threshold="30000"
        tool_keep="10"
        ctx_limit="false"
        ctx_limit_val="500000"
        info "B 组配置: Cloud + clearing 关闭 (1M token 上下文)"
    fi

    info "准备实验环境: 组=$group, 任务='$task'"
    cat > "$META_FILE" << EOF
{"group":"$group","task":"$task","exp_id":"$exp_id","prepare_time":"$ts","status":"prepared","clearing_enabled":"$clear_enabled","clearing_threshold":"$clear_threshold","tool_keep":"$tool_keep","ctx_limit_enabled":"$ctx_limit","ctx_limit":"$ctx_limit_val"}
EOF
    > /tmp/anthropic_proxy.log 2>/dev/null || true
    > "$PROJECT_DIR/logs/anthropic_proxy.log" 2>/dev/null || true
    date +%s > "$EXPERIMENT_DIR/${exp_id}.start_time"
    info "实验 ID: $exp_id"
    info ""
    info "启动代理命令:"
    info "  cd $PROJECT_DIR"
    if [[ "$group" == "A" ]]; then
        info "  PROXY_CLEAR_ENABLED=true PROXY_CLEAR_THRESHOLD=15000 PROXY_TOOL_KEEP=2 PROXY_CTX_LIMIT_ENABLED=true PROXY_CTX_CHARS_LIMIT=180000 LLAMA_BASE_URL=https://api.deepseek.com/v1 LLAMA_API_KEY=sk-... python3 anthropic_proxy.py"
    else
        info "  LLAMA_BASE_URL=https://api.deepseek.com/v1 LLAMA_API_KEY=sk-... python3 anthropic_proxy.py"
    fi
    info ""
    info "下一步: 执行 Claude Code 任务，完成后运行: $0 collect --group $group"
}

cmd_collect() {
    local group=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --group|-g) group="$2"; shift 2 ;;
            *) error "未知参数: $1"; exit 1 ;;
        esac
    done
    if [[ -z "$group" ]]; then
        error "用法: $0 collect --group A|B"; exit 1
    fi
    if [[ ! -f "$META_FILE" ]]; then
        error "未找到实验元数据，请先执行 prepare"; exit 1
    fi
    local exp_id task meta_group
    exp_id=$(python3 -c "import json; print(json.load(open('$META_FILE'))['exp_id'])")
    task=$(python3 -c "import json; print(json.load(open('$META_FILE'))['task'])")
    meta_group=$(python3 -c "import json; print(json.load(open('$META_FILE'))['group'])")
    info "收集 $group 组实验数据: $exp_id"
    local end_ts; end_ts=$(date +%s)
    echo "$end_ts" > "$EXPERIMENT_DIR/${exp_id}.end_time"
    local log_src="/tmp/anthropic_proxy.log"
    [[ -f "$PROJECT_DIR/logs/anthropic_proxy.log" ]] && log_src="$PROJECT_DIR/logs/anthropic_proxy.log"
    local log_dest="$EXPERIMENT_DIR/${exp_id}.log"
    cp "$log_src" "$log_dest"
    info "  代理日志: $log_dest ($(wc -l < "$log_dest") 行)"
    local analysis_dest="$EXPERIMENT_DIR/${exp_id}.json"
    python3 "$SCRIPT_DIR/analyze_experiment.py" --log "$log_dest" --output "$analysis_dest" --group "$group" --task "$task" --exp-id "$exp_id"
    info "  分析报告: $analysis_dest"
    local start_ts elapsed
    start_ts=$(cat "$EXPERIMENT_DIR/${exp_id}.start_time")
    elapsed=$((end_ts - start_ts))
    info "  实验耗时: ${elapsed}s"
    python3 -c "
import json
d=json.load(open('$META_FILE'))
d['status']='collected'
d['end_time']='$end_ts'
d['elapsed_seconds']=$elapsed
d['log_file']='$log_dest'
d['analysis_file']='$analysis_dest'
json.dump(d,open('$META_FILE','w'),indent=2,ensure_ascii=False)
"
    info "✅ $group 组数据收集完成"
    info "如果这是 B 组: $0 report"
    info "如果这是 A 组: $0 prepare --group B --task '$task'"
}

cmd_report() {
    info "生成 A/B 对比报告..."
    local a_analysis b_analysis
    a_analysis=$(ls -t "$EXPERIMENT_DIR"/A-*.json 2>/dev/null | head -1)
    b_analysis=$(ls -t "$EXPERIMENT_DIR"/B-*.json 2>/dev/null | head -1)
    if [[ -z "$a_analysis" ]]; then error "未找到 A 组分析数据"; exit 1; fi
    if [[ -z "$b_analysis" ]]; then error "未找到 B 组分析数据"; exit 1; fi
    local report_file="$EXPERIMENT_DIR/ab_report_$(date +%Y%m%d-%H%M%S).md"
    python3 "$SCRIPT_DIR/analyze_experiment.py" --a "$a_analysis" --b "$b_analysis" --report "$report_file"
    info "✅ 报告已生成: $report_file"
}

cmd_exp_status() {
    if [[ ! -f "$META_FILE" ]]; then info "没有正在进行的实验"; return 0; fi
    python3 -c "
import json
d=json.load(open('$META_FILE'))
for k,v in d.items(): print(f'  {k}: {v}')
"
}

main() {
    case "${1:-help}" in
        prepare) shift; cmd_prepare "$@" ;;
        collect) shift; cmd_collect "$@" ;;
        report)  cmd_report ;;
        status)  cmd_exp_status ;;
        help|--help|-h)
            cat << 'EOF'
A/B 对比实验执行脚本

用法:
  ./tools/run_experiment.sh prepare --group A|B --task '任务描述'
  ./tools/run_experiment.sh collect --group A|B
  ./tools/run_experiment.sh report
  ./tools/run_experiment.sh status

实验流程:
  1. prepare --group A --task '...'   # 准备 A 组环境
  2. 人工执行 Claude Code 任务
  3. collect --group A                # 收集 A 组数据
  4. prepare --group B --task '...'   # 准备 B 组环境
  5. 人工执行 Claude Code 任务
  6. collect --group B                # 收集 B 组数据
  7. report                           # 生成对比报告
EOF
            ;;
        *) error "未知命令: $1"; echo "使用 '$0 help' 查看用法"; exit 1 ;;
    esac
}

main "$@"
