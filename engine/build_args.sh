#!/usr/bin/env bash
# 按命名约定从环境变量生成 vLLM CLI 参数，供 prefill/decode 复用。
#
# 约定：
#   COMMON_FOO_BAR=value  -> --foo-bar value（P/D 共享）
#   P_FOO_BAR=value       -> --foo-bar value（仅 prefill，覆盖 COMMON_）
#   D_FOO_BAR=value       -> --foo-bar value（仅 decode，覆盖 COMMON_）
#   值为 true              -> 加裸 flag
#   值为 false 或空        -> 不加
#   值以 @ 开头            -> 从文件读取内容（用于 JSON）
#   SKIP_KEYS 中的后缀     -> 只给脚本用，不生成参数
#   ALIAS                  -> 处理无法由变量名推导的 flag
#
# 未识别的变量会被转成一个 --flag，vLLM 启动时会报错；
# 配合 `make args` 预览可以避免等 10 分钟才发现。

# 变量名推导不出正确 flag 的，在这里显式映射
declare -A ALIAS=(
  [SERVED_NAME]=served-model-name
  [TP]=tensor-parallel-size
  [PREFIX_RETENTION]=prefix-cache-retention-interval
  [TOOL_PARSER]=tool-call-parser
  [CHAT_TEMPLATE_KWARGS]=default-chat-template-kwargs
  [MODEL_LOADER_CONFIG]=model-loader-extra-config
  [ENABLE_EP]=enable-expert-parallel
)

# 去掉 P_/D_/S_ 前缀后命中这些后缀的变量不生成参数
#   NPUS / CAPTURE_MODE / COMPILATION_CONFIG：由脚本自行拼装
#   SPEC_*：给 single.sh 拼装 speculative-config 用
SKIP_KEYS=" NPUS CAPTURE_MODE COMPILATION_CONFIG SPEC_TOKENS SPEC_METHOD SPEC_ENFORCE_EAGER "

ARGS=()

_flag_of() {
  local key=$1
  if [[ -n "${ALIAS[$key]:-}" ]]; then
    printf '%s' "${ALIAS[$key]}"
  else
    printf '%s' "$key" | tr '[:upper:]_' '[:lower:]-'
  fi
}

_add() {
  local key=$1 val=$2 flag
  [[ -z "$val" ]] && return 0
  flag="$(_flag_of "$key")"
  case "$val" in
    true)  ARGS+=("--$flag"); return 0 ;;
    false) return 0 ;;
    @*)    val="$(cat "${val#@}")" ;;
  esac
  ARGS+=("--$flag" "$val")
}

build_args() {
  local prefix="${1:?P 或 D}" name suffix pkey
  ARGS=()

  # 1) COMMON_ 共享参数（被 <prefix>_ 同名覆盖时跳过）
  while IFS= read -r name; do
    [[ "$name" == COMMON_* ]] || continue
    suffix="${name#COMMON_}"
    case " $SKIP_KEYS " in *" $suffix "*) continue ;; esac
    pkey="${prefix}_${suffix}"
    [[ -n "${!pkey:-}" ]] && continue
    _add "$suffix" "${!name}"
  done < <(env | cut -d= -f1 | sort)

  # 2) 本实例前缀参数
  while IFS= read -r name; do
    [[ "$name" == "${prefix}_"* ]] || continue
    suffix="${name#${prefix}_}"
    case " $SKIP_KEYS " in *" $suffix "*) continue ;; esac
    _add "$suffix" "${!name}"
  done < <(env | cut -d= -f1 | sort)
}

# 取派生值（如 capture 尺寸）：<prefix>_SUFFIX > COMMON_SUFFIX > SUFFIX
effective() {
  local prefix="${1:?P 或 D}" suffix="${2:?SUFFIX}" pkey ckey
  pkey="${prefix}_${suffix}"
  ckey="COMMON_${suffix}"
  if [[ -n "${!pkey+x}" ]]; then printf '%s' "${!pkey}"; return 0; fi
  if [[ -n "${!ckey+x}" ]]; then printf '%s' "${!ckey}"; return 0; fi
  if [[ -n "${!suffix+x}" ]]; then printf '%s' "${!suffix}"; fi
}
