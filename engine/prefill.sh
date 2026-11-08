#!/usr/bin/env bash
# Prefill 实例：参数全部由环境变量按约定生成，见 build_args.sh。
set -euo pipefail
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/build_args.sh"

: "${P_PORT:?P_PORT 未设置}"
: "${P_NPUS:?P_NPUS 未设置}"

export ASCEND_RT_VISIBLE_DEVICES="$P_NPUS"

MAX_SEQS="$(effective P MAX_NUM_SEQS)"
SPEC="$(effective P SPEC_TOKENS)"
CAPTURE_MODE="$(effective P CAPTURE_MODE)"
: "${MAX_SEQS:?MAX_NUM_SEQS 未设置}" "${SPEC:?SPEC_TOKENS 未设置}" "${CAPTURE_MODE:?P_CAPTURE_MODE 未设置}"

build_args P

CAPTURE="$(capture_sizes "$MAX_SEQS" "$SPEC")"
ARGS+=(
  --compilation-config
  "{\"cudagraph_mode\":\"$CAPTURE_MODE\",\"cudagraph_capture_sizes\":[$CAPTURE]}"
)

if [[ -n "${DRAFT:-}" ]]; then
  ARGS+=(
    --speculative-config
    "{\"method\":\"${SPEC_METHOD:-dflash}\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":$SPEC}"
  )
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%q ' vllm serve "$MODEL" "${ARGS[@]}"
  echo
  exit 0
fi

exec vllm serve "$MODEL" "${ARGS[@]}"
