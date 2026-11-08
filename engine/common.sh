#!/usr/bin/env bash
# 由 prefill.sh / decode.sh source；只做与模型、卡数无关的环境准备。
set -euo pipefail

: "${HOST_ID:?HOST_ID 未设置}"
: "${BIND_IP:?BIND_IP 未设置}"
: "${NIC:?NIC 未设置}"
: "${MODEL:?MODEL 未设置}"
: "${SERVED_NAME:?SERVED_NAME 未设置}"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OMP_PROC_BIND=false
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-512}"
export HCCL_OP_EXPANSION_MODE=AIV
export TASK_QUEUE_ENABLE=1
export CPU_AFFINITY_CONF=1
export ASCEND_LAUNCH_BLOCKING=0
export PYTHONHASHSEED=0
export VLLM_USE_V2_MODEL_RUNNER="${VLLM_USE_V2_MODEL_RUNNER:-1}"

export HCCL_EXEC_TIMEOUT=1200
export HCCL_CONNECT_TIMEOUT=600
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=3000
export HCCL_IF_IP="$BIND_IP"
export GLOO_SOCKET_IFNAME="$NIC"
export TP_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC"

CONF_DIR="/opt/vllm/hosts/${HOST_ID}"
export MOONCAKE_CONFIG_PATH="${CONF_DIR}/mooncake.json"
export MMC_META_CONFIG_PATH="${CONF_DIR}/mmc-meta.conf"
export MMC_LOCAL_CONFIG_PATH="${CONF_DIR}/mmc-local.conf"

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

# capture 尺寸必须为 (spec_tokens+1) 的整数倍
capture_sizes() {
  local seqs="${1:?max_num_seqs}" spec="${2:?spec_tokens}"
  python3 -c "s=$spec+1; print(','.join(str(s*i) for i in range(1,$seqs+1)))"
}
