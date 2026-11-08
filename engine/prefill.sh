#!/usr/bin/env bash
# Prefill 实例：只做 prefill，把 KV 交给 decode。
set -euo pipefail
source "$(dirname "$0")/common.sh"

: "${P_PORT:?P_PORT 未设置}"
: "${P_NPUS:?P_NPUS 未设置}"
: "${P_GMU:?P_GMU 未设置}"
: "${P_CHUNK:?P_CHUNK 未设置}"
: "${P_CAPTURE_MODE:?P_CAPTURE_MODE 未设置}"

export ASCEND_RT_VISIBLE_DEVICES="$P_NPUS"

CAPTURE="$(capture_sizes "$MAX_NUM_SEQS" "$SPEC_TOKENS")"

args=(
  serve "$MODEL"
  --host 0.0.0.0
  --port "$P_PORT"
  --served-model-name "$SERVED_NAME"
  --quantization ascend
  --data-parallel-size 1
  --tensor-parallel-size "$TP"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$P_CHUNK"
  --gpu-memory-utilization "$P_GMU"
  --block-size "$BLOCK_SIZE"
  --enable-chunked-prefill
  --enable-prefix-caching
  --prefix-cache-retention-interval "$PREFIX_RETENTION"
  --enable-prompt-tokens-details
  --tool-call-parser "$TOOL_PARSER"
  --enable-auto-tool-choice
  --reasoning-parser "$REASONING_PARSER"
  --async-scheduling
  --compilation-config "{\"cudagraph_mode\":\"$P_CAPTURE_MODE\",\"cudagraph_capture_sizes\":[$CAPTURE]}"
  --model-loader-extra-config "$MODEL_LOADER_CONFIG"
  --default-chat-template-kwargs "$CHAT_TEMPLATE_KWARGS"
  --kv-transfer-config "$(cat "$CONF_DIR/kv-p.json")"
  --additional-config "$(cat "$CONF_DIR/p-additional.json")"
)

if [[ "${ENABLE_EP:-0}" == "1" ]]; then
  args+=(--enable-expert-parallel)
fi
if [[ -n "${DRAFT:-}" ]]; then
  args+=(--speculative-config "{\"method\":\"${SPEC_METHOD:-dflash}\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":$SPEC_TOKENS}")
fi
if [[ "${P_API_SERVERS:-1}" -gt 1 ]]; then
  args+=(--api-server-count "$P_API_SERVERS")
fi

exec vllm "${args[@]}"
