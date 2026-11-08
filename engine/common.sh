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
