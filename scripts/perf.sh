#!/usr/bin/env bash
# 性能诊断入口：取数（bench）、按需抓 trace（profile）、现场取证（collect）。
#
#   scripts/perf.sh <host> bench   [bench.py 参数...]
#   scripts/perf.sh <host> profile [--seconds N] [bench.py 参数...]
#   scripts/perf.sh <host> collect [--out DIR]
#   scripts/perf.sh <host> logstats
#
# 依赖：curl、docker、python3 + requests（部署机默认都有）。
# profile 需要服务端带 --profiler-config 启动（见 hosts/profiler.json.example）。
set -euo pipefail

STACK_DIR=$(cd "$(dirname "$0")/.." && pwd)

HOST=${1:?用法: perf.sh <host> <bench|profile|collect|logstats> [参数...]}
CMD=${2:?用法: perf.sh <host> <bench|profile|collect|logstats> [参数...]}
shift 2

ENV_FILE="$STACK_DIR/hosts/$HOST/.env"
[[ -f "$ENV_FILE" ]] || { echo "缺少 $ENV_FILE" >&2; exit 1; }

getenv() { sed -n "s/^$1=\([^#]*\).*/\1/p" "$ENV_FILE" | tail -1 | sed 's/[[:space:]]*$//'; }

MODE=$(getenv STACK_MODE)
MODE=${MODE:-pd}
PROJECT=$(getenv COMPOSE_PROJECT_NAME)
[[ -n "$PROJECT" ]] || PROJECT="vllm-$(echo "$HOST" | tr . -)"

case "$MODE" in
  single) SERVICE=single;          PORT=$(getenv S_PORT) ;;
  pd)     SERVICE=${SERVICE:-decode}; PORT=$(getenv D_PORT) ;;
  *)      echo "未知 STACK_MODE=$MODE" >&2; exit 1 ;;
esac
PORT=${PORT:-8080}
MODEL_NAME=$(getenv COMMON_SERVED_NAME)
[[ -n "$MODEL_NAME" ]] || MODEL_NAME=$(getenv SERVED_NAME)
[[ -n "$MODEL_NAME" ]] || { echo "缺少 COMMON_SERVED_NAME" >&2; exit 1; }

CONTAINER="$PROJECT-$SERVICE-1"
BIND_IP=$(getenv BIND_IP)
BIND_IP=${BIND_IP:-127.0.0.1}
URL="http://$BIND_IP:$PORT"

REPORT_DIR="$STACK_DIR/perf-reports/$(date +%Y%m%d-%H%M%S)"

container_running() {
  docker ps --filter "name=^${CONTAINER}$" --format '{{.Names}}' | grep -q .
}

need_container() {
  container_running || { echo "容器 $CONTAINER 未运行" >&2; exit 1; }
}

trace_dir_container() {
  local json="$STACK_DIR/hosts/$HOST/profiler.json"
  if [[ -f "$json" ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("torch_profiler_dir") or "/root/.cache/vllm-profiles")' "$json"
  else
    printf '%s' /root/.cache/vllm-profiles
  fi
}

trace_dir_host() {
  local inner cache
  inner=$(trace_dir_container)
  cache=$(getenv CACHE_ROOT)
  cache=${cache:-/root/.cache}
  printf '%s' "${inner/#\/root\/.cache/$cache}"
}

api_get()  { curl -fsS -m 30 "$URL$1"; }
api_post() { curl -fsS -m 600 -X POST "$URL$1"; }

run_bench() {
  local default_args=(--input-len 1024 --output-len 128 --num-prompts 8 --concurrency 1)
  python3 "$STACK_DIR/scripts/bench.py" --url "$URL" --model "$MODEL_NAME" \
    ${BENCH_ARGS+"${BENCH_ARGS[@]}"}
}

cmd_bench() {
  BENCH_ARGS=("$@")
  [[ ${#BENCH_ARGS[@]} -gt 0 ]] || BENCH_ARGS=(--input-len 1024 --output-len 128 --num-prompts 8 --concurrency 1)
  api_get /health >/dev/null || { echo "服务未就绪: $URL/health" >&2; exit 1; }
  run_bench
}

cmd_profile() {
  local seconds=0 extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --seconds) seconds=$2; shift 2 ;;
      *) extra+=("$1"); shift ;;
    esac
  done
  api_get /health >/dev/null || { echo "服务未就绪: $URL/health" >&2; exit 1; }

  echo "[perf] start_profile"
  api_post /start_profile || {
    echo "start_profile 失败：服务端需要用 --profiler-config 启动（S_PROFILER_CONFIG）" >&2
    exit 1
  }
  trap 'api_post /stop_profile >/dev/null 2>&1 || true' EXIT

  if [[ ${#extra[@]} -gt 0 ]]; then
    BENCH_ARGS=("${extra[@]}")
  else
    BENCH_ARGS=(--input-len 256 --output-len 32 --num-prompts 4 --concurrency 1)
  fi
  local bench_log="$STACK_DIR/.perf-bench.log"
  run_bench | tee "$bench_log"
  if (( seconds > 0 )); then
    sleep "$seconds"
  fi

  echo "[perf] stop_profile"
  api_post /stop_profile
  trap - EXIT

  local dir
  dir=$(trace_dir_host)
  inner=$(trace_dir_container)
  sleep 5
  echo "[perf] trace 目录: $dir（容器内 $inner）"
  if [[ -d "$dir" ]] || docker exec "$CONTAINER" test -d "$inner" 2>/dev/null; then
    du -sh "$dir" 2>/dev/null || true
    # torch_npu 的 trace 目录是 root 700，宿主机读不了：导出与统计都在容器里做
    docker exec "$CONTAINER" chmod -R a+rX "$inner" 2>/dev/null || true
    docker exec "$CONTAINER" ls -lt "$inner" 2>/dev/null | head -12 || ls -lt "$dir" | head -12
    echo "[perf] 热点算子（最新 rank，msprof 导出约 20s）"
    docker exec "$CONTAINER" python3 /opt/vllm/scripts/trace_top.py "$inner" \
      --export --top "${TOP:-20}" \
      || echo "[perf] 容器内分析失败，可稍后手动: docker exec $CONTAINER python3 /opt/vllm/scripts/trace_top.py $inner --export" >&2
  else
    echo "[perf] 未找到 trace 目录，检查 profiler.json 的 torch_profiler_dir 与 CACHE_ROOT 挂载" >&2
  fi
}

cmd_collect() {
  local out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out) out=$2; shift 2 ;;
      *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
  done
  out=${out:-$REPORT_DIR}
  mkdir -p "$out"

  {
    echo "# host=$HOST mode=$MODE container=$CONTAINER url=$URL"
    echo "## versions"
    docker inspect "$CONTAINER" --format 'image={{.Config.Image}}' 2>/dev/null || true
    docker exec "$CONTAINER" bash -lc 'git -C /vllm-workspace/vllm-ascend log --oneline -1; git -C /vllm-workspace/vllm log --oneline -1' 2>/dev/null || true
  } > "$out/summary.txt"

  docker inspect "$CONTAINER" --format '{{json .Path}} {{json .Args}}' \
    > "$out/cmd.txt" 2>/dev/null || true
  docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sort > "$out/env.txt" 2>/dev/null || true

  if api_get /health >/dev/null 2>&1; then
    api_get /metrics > "$out/metrics.txt" 2>/dev/null || true
    grep -E 'vllm:(spec_decode|num_requests|kv_cache|generation_tokens|prompt_tokens|time_to_first_token|inter_token|request_success|cudagraph)' \
      "$out/metrics.txt" > "$out/metrics-focused.txt" 2>/dev/null || true
  else
    echo "服务未就绪，跳过 /metrics" >> "$out/summary.txt"
  fi

  docker logs --tail 3000 "$CONTAINER" > "$out/vllm.log" 2>&1 || true
  # 只看引擎自己的 stats 行；ERROR/traceback 单独收，避免混进“统计”
  grep -E 'Avg (prompt|generation) throughput|Speculative metrics|Running:|Waiting:|KV cache usage|[Cc]udagraph (dispatch|mode)|iteration' \
    "$out/vllm.log" | grep -vE 'ERROR|Traceback|File "' | tail -200 > "$out/stats-tail.txt" || true
  grep -E 'ERROR|Traceback' "$out/vllm.log" | tail -100 > "$out/errors.txt" || true
  grep -iE 'warn|fallback|not support|disable|deprecat' "$out/vllm.log" \
    | grep -vE 'MMC|AccLink|mmc_local|backend_id|mooncake' | sort -u | tail -80 \
    > "$out/warnings.txt" || true

  npu-smi info > "$out/npu-smi.txt" 2>&1 || true
  for i in 0 1 2 3 4 5 6 7; do
    npu-smi info -t usages -i "$i" 2>/dev/null || true
  done > "$out/npu-usages.txt"

  echo "[perf] 采集完成: $out"
  ls -l "$out"
}

cmd_logstats() {
  need_container
  docker logs -f --tail 200 "$CONTAINER" 2>&1 \
    | grep --line-buffered -E 'Avg (prompt|generation) throughput|Speculative metrics|Running:|Waiting:|KV cache usage|cudagraph|iteration'
}

case "$CMD" in
  bench)     cmd_bench "$@" ;;
  profile)   cmd_profile "$@" ;;
  collect)   cmd_collect "$@" ;;
  logstats)  cmd_logstats "$@" ;;
  *) echo "未知子命令: $CMD" >&2; exit 1 ;;
esac
