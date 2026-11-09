#!/usr/bin/env bash
# 单实例（聚合）模式：prefill 与 decode 在同一个 vLLM 进程内，直接暴露 OpenAI API。
# 参数生成规则与 prefill/decode 相同，只是前缀为 S_（见 build_args.sh）。
set -euo pipefail
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/build_args.sh"

: "${S_PORT:?S_PORT 未设置}"
: "${S_NPUS:?S_NPUS 未设置}"

export ASCEND_RT_VISIBLE_DEVICES="$S_NPUS"

MAX_SEQS="$(effective S MAX_NUM_SEQS)"
SPEC="$(effective S SPEC_TOKENS)"
CAPTURE_MODE="$(effective S CAPTURE_MODE)"
SPEC_METHOD="$(effective S SPEC_METHOD)"
: "${MAX_SEQS:?MAX_NUM_SEQS 未设置}" "${SPEC:?SPEC_TOKENS 未设置}" "${CAPTURE_MODE:?S_CAPTURE_MODE 未设置}"

build_args S

CAPTURE="$(capture_sizes "$MAX_SEQS" "$SPEC")"
ARGS+=(
  --compilation-config
  "{\"cudagraph_mode\":\"$CAPTURE_MODE\",\"cudagraph_capture_sizes\":[$CAPTURE]}"
)

# 投机解码：
#   DRAFT 非空           -> 外部 draft 模型（dflash 等）
#   DRAFT 为空且 SPEC>0  -> 模型自带的 MTP 头（Qwen3.8-Flash-Next 用 qwen3_5_mtp）
if [[ -n "${DRAFT:-}" ]]; then
  ARGS+=(
    --speculative-config
    "{\"method\":\"${SPEC_METHOD:-dflash}\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":$SPEC}"
  )
elif (( SPEC > 0 )); then
  SPEC_METHOD="${SPEC_METHOD:-qwen3_5_mtp}"
  if [[ "$SPEC_METHOD" == dflash* ]]; then
    echo "[single] SPEC_METHOD=${SPEC_METHOD} 需要 DRAFT，但 DRAFT 为空" >&2
    exit 1
  fi
  ARGS+=(
    --speculative-config
    "{\"method\":\"$SPEC_METHOD\",\"num_speculative_tokens\":$SPEC,\"enforce_eager\":${SPEC_ENFORCE_EAGER:-false}}"
  )
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%q ' vllm serve "$MODEL" "${ARGS[@]}"
  echo
  exit 0
fi

exec vllm serve "$MODEL" "${ARGS[@]}"
