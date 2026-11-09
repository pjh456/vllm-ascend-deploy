# vLLM Ascend 单机部署骨架（1P1D / 单实例）

基于 Docker Compose 的 vLLM Ascend 单机部署模板，支持两种模式：

- **pd（默认）**：Prefill / Decode 分离（1P1D），带 PD shim 与 Mooncake/MemCache 三级 KV 池
- **single**：单实例聚合，prefill 与 decode 在同一个 vLLM 进程内，直接暴露 OpenAI 兼容 API；适合放不进 1P1D 的模型（例如 Qwen3.8-Flash-Next）

目标不是"一套能跑所有硬件的通用部署"，而是把单机部署里最容易出错的部分固化成可复用的骨架：

- 每个进程一个容器，`PID 1` 就是 vLLM，信号直传，没有 bash supervisor
- 参数由环境变量按命名约定自动拼装，脚本里没有一长串 `--flag`
- 健康检查是事件驱动的（`start_interval` + `depends_on: service_healthy`），不靠写死等待时间
- 主机特有的配置（IP、网卡、设备节点、模型路径、池大小）集中在一个被 git 忽略的目录里

## 架构

```
                 client (OpenAI 兼容协议)
                              │
                              ▼
                 ┌────────────────────────────┐
                 │ router :8080               │  PD shim (shim/pd_proxy.py)
                 │ PD 编排 / 重试 / 熔断       │  metrics :29000
                 └────────────┬───────────────┘
                              │ 一次请求两跳
              ┌───────────────┴───────────────┐
              ▼                               ▼
   ┌────────────────────┐          ┌────────────────────┐
   │ prefill :8100      │          │ decode  :8200      │
   │ NPU 0-3, TP4       │  ──KV──▶ │ NPU 4-7, TP4       │
   └────────────────────┘          └────────────────────┘
              │                               │
              └───────────┬───────────────────┘
                          ▼
        kv-master :50088（Mooncake master）
        kv-meta   :5000/6000/8000（memcache MetaService）
        mmc-shm（P/D 共享的 /dev/shm，KV 池）
```

- **prefill** 只做 prefill，返回 `kv_transfer_params`
- **decode** 拉取 KV 后逐 token 生成
- **router** 负责把一次客户端请求拆成 P → D 两跳，并做重试与熔断
- **kv-master / kv-meta** 是 KV 传输与池的元数据基础设施，独立于引擎生命周期

`STACK_MODE=single` 时只启动一个引擎，没有 router、没有 KV 元数据服务：

```
                 client (OpenAI 兼容协议)
                              │
                              ▼
                 ┌────────────────────────────┐
                 │ single :8088               │  一个 vLLM 进程
                 │ NPU 0-7, TP8               │  prefill + decode 聚合
                 └────────────────────────────┘
```

单实例模式不启动 Mooncake master 与 PD shim 容器；启用 KV 池时会启动 MemCache MetaService（`kv-meta`）。`/dev/shm` 仍由 `mmc-shm` 提供（tmpfs 上限由 `MMC_SHM_SIZE` 控制，按实际需要写即可）。

## 前置条件

- 单机 Ascend 910B（8 卡），CANN / driver 已正确安装，`npu-smi info` 正常
- Docker Engine 与 Compose v2.20+（需要 `start_interval`、`depends_on: service_healthy`）
- vLLM Ascend 镜像（例如 `ascendai/vllm-ascend`，版本写进 `versions.env`）
- vLLM Router 镜像（本项目提供 `router/Dockerfile`，需本地构建）
- 模型权重；如果使用投机解码，还需要对应的 draft 模型

## 快速开始

```bash
git clone REPO_URL vllm-ascend-deploy
cd vllm-ascend-deploy

# 1. 生成主机配置（会被 .gitignore 忽略）
make init HOST=node1              # PD 模式
make init HOST=node1 MODE=single  # 单实例模式

# 2. 填写 hosts/<host>/ 下的配置
#    .env / runtime.env / host.yaml / mooncake.json
#    kv-p.json / kv-d.json / p-additional.json / d-additional.json
#    单实例还要看 s-additional.json / engram.json
#    mmc-local.conf / mmc-meta.conf

# 3. router 使用 shim（复用 vllm 镜像，无需额外构建）

# 4. 校验配置
make check HOST=node1

# 5. 预览最终启动参数（不启动推理）
#    pd 模式默认看 prefill，single 模式默认看 single
make args HOST=node1
make args HOST=node1 SERVICE=decode

# 6. 启动
make up HOST=node1

# 7. 查看状态
make ps HOST=node1
make logs HOST=node1
```

`make up` 会按 `STACK_MODE` 启动对应 profile：PD 模式等 prefill / decode 的 `/health` 都通过后才启动 router，最多等待 30 分钟；单实例模式只等 `single` 的 `/health`。

## 目录结构

```
.
├── compose.yaml                 # 服务编排，全部使用 ${VAR}
├── Makefile                     # init / check / args / up / down / logs
├── versions.env.example         # 镜像与组件版本（复制为 versions.env）
├── env/
│   └── common.env               # 与加速器无关的通用环境变量（提交）
├── engine/
│   ├── common.sh                # 派生值、库路径、capture 计算
│   ├── build_args.sh            # 环境变量 -> vLLM CLI 参数
│   ├── prefill.sh               # PD 模式：exec vllm serve
│   ├── decode.sh                # PD 模式：exec vllm serve
│   └── single.sh                # 单实例模式：exec vllm serve
├── shim/
│   └── pd_proxy.py              # PD 编排 shim（转发 prefill 响应里的 KV 参数）
├── router/
│   └── Dockerfile               # 可选：vllm-router 镜像（当前未使用）
├── scripts/
│   ├── init.sh                  # 生成 hosts/<host>/ 模板
│   └── check.py                 # 配置自检
├── systemd/
│   └── vllm-stack@.service      # 开机启动（配合 restart: "no"）
└── hosts/
    ├── .env.example             # 主机与引擎参数模板
    ├── runtime.env.example      # 加速器/镜像相关的环境变量模板
    ├── host.yaml.example        # 设备节点与挂载模板
    ├── kv-p.json.example        # KV 连接器配置模板
    ├── kv-d.json.example
    ├── p-additional.json.example
    ├── d-additional.json.example
    ├── s-additional.json.example # 单实例 additional-config 模板
    ├── engram.json.example      # 单实例 Engram/PLE 模板
    ├── kv-single.json.example   # 单实例 MemCache KV 池（kv_both）模板
    ├── mmc-local.conf.example
    └── mmc-meta.conf.example
```

**仓库里只有 `*.example`**。真实的 `hosts/<host>/` 和 `versions.env` 由 `make init` 生成并被忽略，不会进入 git。

## 配置体系

### 三层环境变量

`compose.yaml` 对引擎服务按顺序注入三个文件，后者覆盖前者：

| 文件 | 内容 | 是否提交 |
|---|---|---|
| `env/common.env` | 与加速器无关的通用变量（`OMP_*`、`PYTHONHASHSEED`、`VLLM_USE_V2_MODEL_RUNNER`） | 是 |
| `hosts/<host>/runtime.env` | 加速器/镜像相关（`HCCL_*`、`ASCEND_*`、`PYTORCH_NPU_ALLOC_CONF`） | 否 |
| `hosts/<host>/.env` | IP、网卡、端口、模型路径、引擎参数、KV 连接器 | 否 |

`HCCL_IF_IP`、`GLOO_SOCKET_IFNAME`、`TP_SOCKET_IFNAME`、`HCCL_SOCKET_IFNAME` 由 `BIND_IP` / `NIC` 在 `engine/common.sh` 里派生，不需要重复配置。

如果镜像内 `/vllm-workspace/vllm-ascend` 的 commit 与你要用的 fork 分支不一致，在 `runtime.env` 里设置：

```bash
VLLM_ASCEND_REPO=https://github.com/YOUR_ACCOUNT/vllm-ascend.git
VLLM_ASCEND_BRANCH=deploy/YOUR_BRANCH
VLLM_ASCEND_FETCH_PROXY=http://YOUR_PROXY:PORT
```

容器启动时会先 `git fetch` + `checkout` 再 `exec vllm`；不设置则直接使用镜像自带版本。镜像版本与分支不一致的典型症状是 DFlash2 / aclgraph 相关的 `Forward context is not set` 断言。

### 参数命名约定

`engine/build_args.sh` 把环境变量自动转成 CLI 参数：

| 写法 | 结果 |
|---|---|
| `COMMON_MAX_MODEL_LEN=262144` | `--max-model-len 262144`（P/D/单实例共享） |
| `P_GPU_MEMORY_UTILIZATION=0.85` | `--gpu-memory-utilization 0.85`（仅 prefill） |
| `D_MAX_NUM_BATCHED_TOKENS=1536` | `--max-num-batched-tokens 1536`（仅 decode） |
| `S_PORT=8088` | `--port 8088`（仅单实例，同时也是对外端口） |
| `COMMON_ENABLE_PREFIX_CACHING=true` | `--enable-prefix-caching`（裸 flag） |
| `COMMON_ENABLE_PREFIX_CACHING=false` | 不添加 |
| `P_KV_TRANSFER_CONFIG=@/opt/vllm/hosts/<host>/kv-p.json` | 参数值从文件读取（JSON） |
| `S_ENGRAM_CONFIG=@/opt/vllm/hosts/<host>/engram.json` | 读成 `--engram-config '<json>'` |
| `S_KV_TRANSFER_CONFIG=@/opt/vllm/hosts/<host>/kv-single.json` | 读成 `--kv-transfer-config '<json>'` |

- 大写转小写、下划线转连字符
- `P_` / `D_` / `S_` 同名键会覆盖 `COMMON_` 键
- 不以上述前缀开头的变量只作配置，不会变成参数
- 只有推导不出名字的才需要映射：`TP`、`SERVED_NAME`、`PREFIX_RETENTION`、`TOOL_PARSER`、`CHAT_TEMPLATE_KWARGS`、`MODEL_LOADER_CONFIG`、`ENABLE_EP`
- `NPUS`、`CAPTURE_MODE`、`COMPILATION_CONFIG`、`SPEC_TOKENS`、`SPEC_METHOD`、`SPEC_ENFORCE_EAGER` 只给脚本用，不生成参数
- capture sizes 由 `MAX_NUM_SEQS × (SPEC_TOKENS + 1)` 自动生成，不再手写数组
- 单实例模式下 `S_*` 覆盖 `COMMON_*`；例如 `S_MAX_NUM_SEQS` 会覆盖 `COMMON_MAX_NUM_SEQS`

用 `make args` 可以在真正启动前看到拼装结果；未知变量会变成 `--flag` 并由 vLLM 报错，所以建议每次改完参数先预览一次。

### 路径约定

- `MODEL_ROOT` 会原样挂载到容器内同名路径，`MODEL` / `DRAFT` 必须位于其下
- 本仓库以只读方式挂载到容器内 `/opt/vllm`，因此 `@` 参数写成 `/opt/vllm/hosts/<host>/...`
- `check.py` 会把容器内 `/opt/vllm/...` 映射回仓库根目录做存在性校验

## 单实例（聚合）部署

`compose.yaml` 里的 `single` 服务在 `profiles: ["single"]` 下，`STACK_MODE=single` 时激活；PD 相关的 `kv-master` / `kv-meta` / `prefill` / `decode` / `router` 都在 `profiles: ["pd"]` 下，不会启动。

```bash
make init HOST=qwen38 MODE=single
# 编辑 hosts/qwen38/.env、runtime.env、host.yaml
make check HOST=qwen38
make args HOST=qwen38      # 自动选择 single 服务
make up HOST=qwen38        # 176B 模型可加 UP_TIMEOUT=3600
```

已有 PD 主机切换成单实例时，先把 `STACK_MODE` 改成 `single`，再从 `hosts/.env.example` 补上 `S_*` 段（`check.py` 会按模式检查必填键），先 `make down` 再 `make up`，避免旧的 P/D 容器继续占卡。

单实例与 PD 的差异：

| 项目 | pd | single |
|---|---|---|
| 入口 | `router:8080` | `single:8088`（由 `S_PORT` 决定） |
| 推理进程 | prefill + decode | 一个 vLLM（prefill/decode 聚合） |
| KV 基础设施 | mooncake + memcache | 可选：MemCache MetaService（`kv_both`） |
| NPU | P/D 各一组 | `S_NPUS` 指定，通常 8 卡 |
| 参数前缀 | `P_` / `D_` | `S_`（覆盖 `COMMON_`） |

### Qwen3.8-Flash-Next 示例

Qwen3.8-Flash-Next 是 125B 骨干 + 51B Engram（PLE）表的 MoE 模型，单实例 TP8 才放得下，因此不适合 1P1D。Ascend 侧建议使用与硬件匹配的镜像（A2 用通用 `v0.30.0-a2`，A3 可用 `qwen3.8-next-a3` / `v0.30.0-a3`）；镜像可以在单个主机的 `.env` 里覆盖：

### 为什么 32G×8 不适合 PD

以 `Eco-Tech/Qwen3.8-Flash-Next-w4a8` 为例：

- 整个 checkpoint 约 **180.9 GiB**；
- Engram/PLE 在这个 checkpoint 里是 **BF16**（128 个 shard × `[2,500,012, 160]`），约 **95.4 GiB**；
- 扣掉 Engram 后的骨干 + vision + MTP 约 **85.5 GiB**。

PD 分离意味着 P 和 D 各自加载一份完整骨干，并且各自把一份 Engram 放在宿主内存。4+4 分卡时每边只有 4×32=128 GB，扣掉约 91.8 GB 权重后只剩约 36 GB 给 KV/激活/图；宿主内存也要准备两份 ~95.4 GiB。Ascend 官方 Qwen3.8-Flash-Next PD 示例用的是 **A2 两台 8 卡机（每角色 8 卡）**，A3 用 16 卡，没有 4+4 的验证配置。

所以 32G×8 的建议是：**单实例 TP8 跑 `STACK_MODE=single`；要 PD 至少准备两台 8 卡机器。** 单实例下非 Engram 权重 TP8 后每卡约 10.7 GiB，剩余显存足够放 KV/激活；Engram 不占 NPU，但宿主内存按 ~96 GiB + 余量准备（建议 128 GB 以上，最好 256 GB）。

```bash
# hosts/<host>/.env
STACK_MODE=single
# 按实际硬件替换镜像
VLLM_IMAGE=quay.io/ascend/vllm-ascend:v0.30.0-a2

MODEL=/data/modelscope/models/Eco-Tech/Qwen3.8-Flash-Next-w4a8
# DFlash2 外部 draft；用模型自带 MTP 时改成 DRAFT= / SPEC_METHOD=qwen3_5_mtp / SPEC_TOKENS=3
DRAFT=/data/modelscope/models/incoai/Qwen3.8-Flash-Next-DFlash2
SPEC_TOKENS=6
SPEC_METHOD=dflash
SPEC_ENFORCE_EAGER=false

COMMON_SERVED_NAME=qwen3.8-flash-next
COMMON_TP=8
COMMON_ENABLE_EP=true
COMMON_MAX_MODEL_LEN=262144
COMMON_ENABLE_PREFIX_CACHING=true
COMMON_ASYNC_SCHEDULING=true
COMMON_ENABLE_AUTO_TOOL_CHOICE=true
COMMON_TOOL_PARSER=qwen3_xml
COMMON_REASONING_PARSER=qwen3
COMMON_QUANTIZATION=ascend
# 留空表示不传 --prefix-cache-retention-interval
COMMON_PREFIX_RETENTION=

S_PORT=8088
S_NPUS=0,1,2,3,4,5,6,7
S_GPU_MEMORY_UTILIZATION=0.93
S_MAX_NUM_SEQS=32
S_MAX_NUM_BATCHED_TOKENS=4096
S_CAPTURE_MODE=FULL_DECODE_ONLY
S_MAMBA_CACHE_MODE=align
S_TRUST_REMOTE_CODE=true
S_ADDITIONAL_CONFIG=@/opt/vllm/hosts/<host>/s-additional.json
S_ENGRAM_CONFIG=@/opt/vllm/hosts/<host>/engram.json
# MemCache KV 池（AscendStoreConnector kv_both）；不用池时置空
S_KV_TRANSFER_CONFIG=@/opt/vllm/hosts/<host>/kv-single.json
MMC_SHM_SIZE=640g
HEALTH_MAX_WAIT=60m
STOP_GRACE=180s
```

`runtime.env` 建议补上（模型加载慢，健康检查要有足够时间）：

```bash
VLLM_ENGINE_READY_TIMEOUT_S=4800
VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=3000
```

要点：

- **Engram/PLE**：`S_ENGRAM_CONFIG` 默认指向 `engram.json`，内容是 `{"cpu_offload": true, "dp_shared_memory": false}`。这个 w4a8 checkpoint 的 PLE 是 BF16，约 **95.4 GiB / 102.4 GB**（不是 51 GB；51 GB 是 FP8 版本的大小），`cpu_offload=true` 会把它 pin 在宿主内存，建议至少 128 GB 宿主内存并留出运行余量；不要把它设成 `cpu_offload=false`，NPU 显存放不下。P/D 各跑一个进程时会各占一份，做 PD 时宿主内存要按两份算。宿主内存不足时，Ascend 侧目前没有官方 `disk_offload` 方案，只能换 PLE 更小的权重版本。
- **投机解码**：默认用 DFlash2 外部 draft（`DRAFT` 指向 `incoai/Qwen3.8-Flash-Next-DFlash2`，`SPEC_METHOD=dflash`，`SPEC_TOKENS=6`），脚本会生成 `--speculative-config '{"method":"dflash","model":"...","num_speculative_tokens":6}'`。想改用模型自带 MTP 头时，把 `DRAFT` 置空、`SPEC_METHOD=qwen3_5_mtp`、`SPEC_TOKENS=3`。`SPEC_METHOD=dflash` 但 `DRAFT` 为空时，`make args` 会直接报错。
- **上下文**：`COMMON_MAX_MODEL_LEN=262144` 是模型原生上限，不需要 YaRN；如果 8 卡显存吃紧，先降 `S_MAX_NUM_SEQS`，再考虑降上下文。
- **前缀缓存**：Qwen3.8-Flash-Next 上仍是实验特性。启用 `--enable-prefix-caching` 时必须同时 `--mamba-cache-mode align`，并按实际上下文长度/并发重新验证 NPU 显存。
- **additional-config**：`s-additional.json.example` 是 A2/A3 单实例模板（shared expert DP、FlashComm1、CPU 绑定、关闭 fused MC2 等）；A3 可把 `enable_static_kernel` 改为 `true`。
- **KV 池**：`S_KV_TRANSFER_CONFIG` 指向 `kv-single.json`，内容是官方 KV Cache Pool 的 PD-Mixed 用法：`AscendStoreConnector` + `kv_role=kv_both` + `backend=memcache`。单实例会额外启动 `kv-meta`（MetaService），`mmc-local.conf` 的 `world_size` 不小于 TP，池大小约为 `rank 数 × dram.size`（例如 8 × 64GB = 512GB）。不需要池时把 `S_KV_TRANSFER_CONFIG` 置空。
- **/dev/shm**：单实例仍复用 `mmc-shm`。启用 KV 池时按池大小留足（例如 512GB 池配 `MMC_SHM_SIZE=640g`）；不启用池时 `128g` 左右即可。tmpfs 只按实际写入计费，上限大不会直接占内存。
- **镜像与版本**：Qwen3.8-Flash-Next 需要 vllm-ascend `v0.26.0rc` 以上，推荐 `v0.32.0rc`；不要拿跑 Qwen3.6/3.8-27B 的旧镜像直接启动。

## 常用命令

| 命令 | 作用 |
|---|---|
| `make init HOST=node1 [MODE=single]` | 从 `*.example` 生成 `hosts/<host>/` 和 `versions.env` |
| `make check HOST=node1` | 路径、端口、JSON、TP/world_size 一致性、`compose config`（按 `STACK_MODE` 校验） |
| `make args HOST=node1 [SERVICE=decode]` | 以 `DRY_RUN=1` 打印最终参数；single 模式默认打印 `single` |
| `make up HOST=node1` | `check` 后按 `STACK_MODE` 启动对应 profile，并等待健康检查 |
| `make down HOST=node1` | 停止并移除容器 |
| `make logs HOST=node1` | 跟踪日志 |
| `make ps HOST=node1` | 查看状态 |
| `make install-unit` | 渲染并安装 systemd 单元（需要 sudo） |

`HOST` 可以包含点号（例如 `1.2`），Makefile 会自动把项目名转成合法形式（`vllm-1-2`）。

## 启动与关闭语义

- 引擎服务（prefill / decode / single）`restart: "no"`：崩溃就停住，不会无限重启；需要开机自启时用 `systemd/vllm-stack@.service`
- 基础设施与 router `restart: unless-stopped`：秒级启动，可独立重启；单实例模式不启动 router 与 Mooncake master，启用 KV 池时仍启动 `kv-meta`
- 引擎脚本以 `exec vllm serve ...` 结尾，容器 `PID 1` 就是 vLLM，`docker stop` 直接发给它
- `stop_grace_period` 建议不小于 vLLM 的退出时间（默认 120s）
- 健康检查：
  - `prefill` / `decode`：`curl 127.0.0.1:<port>/health`
  - `single`：`curl 127.0.0.1:${S_PORT}/health`
  - `start_interval: 2s`：端口一就绪就翻绿，`start_period` 只是告警上限
  - `router` 通过 `depends_on: condition: service_healthy` 等待两个引擎

## 关键约束

1. **KV 池依赖共享的 `/dev/shm`**
   `compose.yaml` 声明了一个 tmpfs 卷 `mmc-shm`，同时挂到 prefill 和 decode 的 `/dev/shm`。如果 P/D 各自使用独立的 `/dev/shm`，decode 将无法命中 prefill 写入池中的 segment。
   池大小 = rank 数 × `mmc-local.conf` 里的 `dram.size`，请让 `MMC_SHM_SIZE` 留出余量。单实例启用 KV 池时同理：8 卡 TP8 就是 8 × `dram.size`。

2. **设备节点按服务划分**
   `hosts/<host>/host.yaml` 只放列表型硬件差异（`/dev/davinci*` 等），prefill 与 decode 分别映射自己的卡，二者都保留 `davinci_manager`、`devmm_svm`、`hisi_hdc`；单实例模式用同一文件里的 `single:` 映射全部 8 卡。
   映射子集时，`P_NPUS` / `D_NPUS` / `S_NPUS` 必须写**容器内逻辑编号**（通常都是 `0,1,2,3` 或 `0..7`），物理卡由 `devices:` 决定；写成物理编号会导致 `ASCEND_RT_VISIBLE_DEVICES ... invalid / aclInit 107001`。

3. **单机 `network_mode: host`**
   容器直接使用宿主网络，端口不能重复；`BIND_IP` 决定监听地址。

4. **router 与 KV 连接器**
   `--kv-connector mooncake` 时，`--prefill <url> <port>` 的第二个参数是 bootstrap 端口。某些传输后端（例如 memfabric）握手语义不同，可以把它设成 `none`，让 router 直接使用 prefill 响应里的 `kv_transfer_params`。

5. **不要把真实配置提交进 git**
   `hosts/*`、`versions.env`、`*.secret.env` 已在 `.gitignore` 中。`git add -f` 会绕过它，注意不要手滑。

6. **单实例的 Engram/PLE 吃宿主内存**
   Qwen3.8-Flash-Next 的 51B n-gram 表在这个 w4a8 checkpoint 里是 BF16，约 95.4 GiB，通过 `--engram-config '{"cpu_offload":true}'` 放在宿主内存；机器至少预留 95.4 GiB + 运行余量（建议 128 GB 以上宿主内存）。`/dev/shm` 与宿主内存是两笔账，不要互相占用。单实例启动后 CPU 内存会明显上涨，属于正常现象。

## 推送前自检

```bash
# 1. 确认没有主机配置被跟踪
git ls-files | grep -E '^hosts/[^/]+/' && echo "有主机配置被跟踪！"

# 2. 扫描可能的敏感信息（按需补充关键字）
git grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|/home/|/data/|secret|password' -- . ':!README.md'

# 3. 确认 versions.env 未被跟踪
git ls-files | grep -x versions.env && echo "versions.env 被跟踪！"
```

如果敏感信息已经进入过历史提交，仅删除文件不够，需要改写历史后再推送。

## 故障排查

| 现象 | 排查方向 |
|---|---|
| `make check` 报端口重复 | PD 模式看 `P_PORT` / `D_PORT` / `ROUTER_PORT` / `METRICS_PORT`；单实例看 `S_PORT` |
| 容器起来但 `/health` 一直不通 | `make logs`；检查模型路径、显存利用率、capture 配置 |
| router 没启动 | 它依赖两个引擎 healthy；先看 `make ps` 与引擎日志 |
| 单实例起来后内存暴涨 | Engram/PLE 在 CPU offload，这个 w4a8 checkpoint 的 BF16 表约 95.4 GiB，宿主内存至少 128 GB；`free -h` 确认没有触发 OOM |
| 单实例报 `SPEC_METHOD=dflash 需要 DRAFT` | 用模型自带 MTP 时改成 `SPEC_METHOD=qwen3_5_mtp` 并让 `DRAFT` 为空 |
| 单实例 KV 池没生效 | 确认 `S_KV_TRANSFER_CONFIG` 非空、`kv-meta` 健康、`mmc-local.conf` 的 `world_size` ≥ TP |
| decode 的池命中率始终为 0 | 确认 `mmc-shm` 同时挂载到 P/D，且 `kv-meta` 只有一份 |
| `docker compose config` 报项目名非法 | 主机名含点号时用 Makefile 的封装，不要手敲 `-p` |
| 长 prompt 并发上不去 | 看 decode 的 `vllm:kv_cache_usage_perc` 与 `request_queue_time_seconds` |

## 已知限制

- 覆盖单机 1P1D 与单机单实例，不涉及多机调度与 Kubernetes
- 面向 Ascend（`--quantization ascend`、`davinci*`、HCCL、Mooncake/AscendStore），不是跨加速器的通用部署
- `vllm-router` 0.1.17 的 Mooncake 模式要求 prefill 在 8998 提供 `/query` bootstrap，而 vllm-ascend 的 `MooncakeConnectorV1` 不提供，因此默认用 `shim/pd_proxy.py` 做 PD 编排；`router/Dockerfile` 保留给后续兼容版本
- 单实例模式下没有 router，健康检查/端口/超时都直接落在 `single` 上；PD 专属的 `P_*` / `D_*` 配置会被忽略，`check.py` 也不会再要求它们
- 通过 router 访问 Anthropic `/v1/messages` 的组合未在本骨架中验证，需要时请单独测试
- 未附带 LICENSE，使用前请自行选择

## 设计取舍

- **一个进程一个容器**：避免 bash supervisor 吞信号、退出码不可见、重启策略失控
- **参数来自环境变量**：新增一个 vLLM 参数只需改 `.env`，不用改脚本；推导不出的才进映射表
- **真实的配置不进 git**：仓库只保留模板与逻辑，避免把地址、路径、密钥带进公开历史
- **不做跨硬件抽象**：设备节点、连接器、镜像本来就是平台特有的，强行抽象只会让排错更难
- **PD 编排用 shim 而不是 vllm-router**：后者的 Mooncake 实现与 vllm-ascend 的连接器不兼容；shim 直接使用 prefill 响应里的 `kv_transfer_params`，行为可预测
- **单实例与 PD 共用一套编排**：用 compose profile 切换，避免维护两份 compose；参数仍然走 `COMMON_` / `S_` 约定，单实例不需要额外的启动脚本分支
