#!/usr/bin/env bash
# 由 prefill.sh / decode.sh source。
# 这里只放逻辑与派生值；可调参数全部来自
#   env/common.env（通用）
#   hosts/<host>/runtime.env（加速器/镜像相关）
#   hosts/<host>/.env（主机与引擎参数）
set -euo pipefail

: "${HOST_ID:?HOST_ID 未设置}"
: "${BIND_IP:?BIND_IP 未设置}"
: "${NIC:?NIC 未设置}"
: "${MODEL:?MODEL 未设置}"

# 由 BIND_IP / NIC 派生，避免在多处维护同一事实
export HCCL_IF_IP="$BIND_IP"
export GLOO_SOCKET_IFNAME="$NIC"
export TP_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC"

CONF_DIR="${CONF_DIR:-/opt/vllm/hosts/${HOST_ID}}"
export MOONCAKE_CONFIG_PATH="${CONF_DIR}/mooncake.json"
export MMC_META_CONFIG_PATH="${CONF_DIR}/mmc-meta.conf"
export MMC_LOCAL_CONFIG_PATH="${CONF_DIR}/mmc-local.conf"

# 把镜像内的 git 源码目录固定到指定 fork 分支（vllm / vllm-ascend 共用）。
# 用法：pin_git_source <label> <dir> <repo> <branch> <proxy> [编译产物路径...]
# 镜像内是 shallow clone 时按 --depth=1 取，避免把整段历史拉进容器。
pin_git_source() {
  local label="$1" dir="$2" repo="$3" branch="$4" proxy="$5"
  shift 5
  [[ -d "$dir/.git" ]] || return 0

  local base_sha
  base_sha="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || true)"

  local -a depth=()
  if [[ "$(git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
    depth=(--depth=1)
  fi

  local ok=0 attempt
  for attempt in 1 2 3 4 5; do
    if GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
       https_proxy="$proxy" http_proxy="$proxy" \
       git -C "$dir" fetch --no-tags "${depth[@]}" "$repo" "$branch"; then
      ok=1
      break
    fi
    echo "[$label] fetch attempt ${attempt}/5 failed, retry in 3s"
    sleep 3
  done
  if [[ "$ok" != "1" ]]; then
    echo "[$label] WARN: fetch failed, keep $(git -C "$dir" rev-parse --short HEAD)"
    return 0
  fi

  git -C "$dir" checkout -f -B "$branch" FETCH_HEAD
  echo "[$label] pinned to $branch @ $(git -C "$dir" rev-parse --short HEAD) (image: ${base_sha:-unknown})"

  # 编译产物（csrc/flash-attn 等）是镜像按 base_sha 预编译的：只替换 Python 源码没问题，
  # 分支若改了编译源码，运行时会 import 失败或 kernel 不匹配，需要重建镜像。
  if [[ -n "$base_sha" && "$#" -gt 0 ]] \
     && ! git -C "$dir" diff --quiet "$base_sha" HEAD -- "$@" 2>/dev/null; then
    echo "[$label] WARN: branch differs from the image build ($base_sha..HEAD) under: $*;" \
         "Python-only switch — rebuild the image if import or a kernel fails" >&2
  fi
}

# 可选：把镜像内的 vllm 固定到指定 fork 分支。
# 镜像是 editable install（__editable__.vllm-0.33.0+empty.pth 指向该目录），
# 换源码即生效、无需 pip 重装；不设置 VLLM_BRANCH 时使用镜像自带版本。
if [[ "${DRY_RUN:-0}" != "1" && -n "${VLLM_BRANCH:-}" ]]; then
  pin_git_source vllm "${VLLM_DIR:-/vllm-workspace/vllm}" \
    "${VLLM_REPO:?VLLM_REPO 未设置}" "$VLLM_BRANCH" "${VLLM_FETCH_PROXY:-}" \
    csrc vllm/vllm_flash_attn
fi

# 可选：把镜像内的 vllm-ascend 固定到指定 fork 分支（与旧部署脚本行为一致）
# 不设置 VLLM_ASCEND_BRANCH 时直接使用镜像自带版本
if [[ "${DRY_RUN:-0}" != "1" && -n "${VLLM_ASCEND_BRANCH:-}" ]]; then
  pin_git_source vllm-ascend "${VLLM_ASCEND_DIR:-/vllm-workspace/vllm-ascend}" \
    "${VLLM_ASCEND_REPO:?VLLM_ASCEND_REPO 未设置}" "$VLLM_ASCEND_BRANCH" "${VLLM_ASCEND_FETCH_PROXY:-}"
fi

# DRY_RUN 只拼参数，跳过所有重量级初始化
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  # jemalloc：按镜像实际情况挑第一个存在的，避免写死路径后静默失效
  for _so in \
    /usr/lib64/libjemalloc.so.2 \
    "$(python3 -c 'import ray,os;print(os.path.join(os.path.dirname(ray.__file__),"core","libjemalloc.so"))' 2>/dev/null || true)"; do
    if [[ -n "$_so" && -f "$_so" ]]; then
      export LD_PRELOAD="${_so}${LD_PRELOAD:+:$LD_PRELOAD}"
      break
    fi
  done

  # 动态库路径：从已安装的包里取，不写死 python 小版本
  _lib_paths="$(python3 - <<'PY'
import os
out = []
for mod, sub in (("mooncake", ""), ("memcache_hybrid", "lib")):
    try:
        m = __import__(mod)
        base = os.path.dirname(m.__file__)
        out.append(os.path.join(base, sub) if sub else base)
    except Exception:
        pass
print(":".join(p for p in out if p))
PY
)"
  export LD_LIBRARY_PATH="${_lib_paths}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# capture 尺寸必须为 (spec_tokens+1) 的整数倍
capture_sizes() {
  local seqs="${1:?max_num_seqs}" spec="${2:?spec_tokens}"
  python3 -c "s=$spec+1; print(','.join(str(s*i) for i in range(1,$seqs+1)))"
}
