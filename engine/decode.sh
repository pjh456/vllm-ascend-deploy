#!/usr/bin/env bash
# Decode 实例：从 prefill 拉 KV，负责逐 token 生成。
set -euo pipefail
source "$(dirname "$0")/common.sh"

: "${D_PORT:?D_PORT 未设置}"
: "${D_NPUS:?D_NPUS 未设置}"
: "${D_GMU:?D_GMU 未设置}"
: "${D_CHUNK:?D_CHUNK 未设置}"
: "${D_CAPTURE_MODE:?D_CAPTURE_MODE 未设置}"

export ASCEND_RT_VISIBLE_DEVICES="$D_NPUS"

CAPTURE="$(capture_sizes "$MAX_NUM_SEQS" "$SPEC_TOKENS")"

args=(
  serve "$MODEL"
  --host 0.0.0.0
  --port "$D_PORT"
  --served-model-name "$SERVED_NAME"
  --quantization ascend
  --data-parallel-size 1
  --tensor-parallel-size "$TP"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$D_CHUNK"
  --gpu-memory-utilization "$D_GMU"
  --block-size "$BLOCK_SIZE"
  --enable-chunked-prefill
  --enable-prefix-caching
  --prefix-cache-retention-interval "$PREFIX_RETENTION"
  --enable-prompt-tokens-details
  --tool-call-parser "$TOOL_PARSER"
  --enable-auto-tool-choice
  --reasoning-parser "$REASONING_PARSER"
  --async-scheduling
  --compilation-config "{\"cudagraph_mode\":\"$D_CAPTURE_MODE\",\"cudagraph_capture_sizes\":[$CAPTURE]}"
  --model-loader-extra-config "$MODEL_LOADER_CONFIG"
  --default-chat-template-kwargs "$CHAT_TEMPLATE_KWARGS"
  --kv-transfer-config "$(cat "$CONF_DIR/kv-d.json")"
  --additional-config "$(cat "$CONF_DIR/d-additional.json")"
)

if [[ "${ENABLE_EP:-0}" == "1" ]]; then
  args+=(--enable-expert-parallel)
fi
if [[ -n "${DRAFT:-}" ]]; then
  args+=(--speculative-config "{\"method\":\"${SPEC_METHOD:-dflash}\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":$SPEC_TOKENS}")
fi

exec vllm "${args[@]}"
