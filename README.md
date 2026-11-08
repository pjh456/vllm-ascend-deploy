# vLLM Ascend PD 部署骨架

基于 Docker Compose 的 vLLM Ascend **Prefill / Decode 分离（1P1D）** 单机部署模板。

目标不是"一套能跑所有硬件的通用部署"，而是把单机 PD 部署里最容易出错的部分固化成可复用的骨架：

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
                 │ router :8080               │  vllm-router
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
make init HOST=node1

# 2. 填写 hosts/<host>/ 下的配置
#    .env / runtime.env / host.yaml / mooncake.json
#    kv-p.json / kv-d.json / p-additional.json / d-additional.json
#    mmc-local.conf / mmc-meta.conf

# 3. 构建 router 镜像（tag 与 versions.env 里的 ROUTER_IMAGE 一致）
docker build -f router/Dockerfile -t vllm-router:0.1.17 .

# 4. 校验配置
make check HOST=node1

# 5. 预览最终启动参数（不启动推理）
make args HOST=node1              # prefill
make args HOST=node1 SERVICE=decode

# 6. 启动
make up HOST=node1

# 7. 查看状态
make ps HOST=node1
make logs HOST=node1
```

`make up` 会等到 prefill / decode 的 `/health` 都通过后才启动 router，最多等待 30 分钟。

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
│   ├── prefill.sh               # exec vllm serve
│   └── decode.sh                # exec vllm serve
├── router/
│   └── Dockerfile               # vllm-router 镜像
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
| `COMMON_MAX_MODEL_LEN=262144` | `--max-model-len 262144`（P/D 共享） |
| `P_GPU_MEMORY_UTILIZATION=0.85` | `--gpu-memory-utilization 0.85`（仅 prefill） |
| `D_MAX_NUM_BATCHED_TOKENS=1536` | `--max-num-batched-tokens 1536`（仅 decode） |
| `COMMON_ENABLE_PREFIX_CACHING=true` | `--enable-prefix-caching`（裸 flag） |
| `COMMON_ENABLE_PREFIX_CACHING=false` | 不添加 |
| `P_KV_TRANSFER_CONFIG=@/opt/vllm/hosts/<host>/kv-p.json` | 参数值从文件读取（JSON） |

- 大写转小写、下划线转连字符
- `P_` / `D_` 同名键会覆盖 `COMMON_` 键
- 不以上述前缀开头的变量只作配置，不会变成参数
- 只有推导不出名字的才需要映射：`TP`、`SERVED_NAME`、`PREFIX_RETENTION`、`TOOL_PARSER`、`CHAT_TEMPLATE_KWARGS`、`MODEL_LOADER_CONFIG`、`ENABLE_EP`
- `NPUS`、`CAPTURE_MODE` 只给脚本用，不生成参数
- capture sizes 由 `MAX_NUM_SEQS × (SPEC_TOKENS + 1)` 自动生成，不再手写数组

用 `make args` 可以在真正启动前看到拼装结果；未知变量会变成 `--flag` 并由 vLLM 报错，所以建议每次改完参数先预览一次。

### 路径约定

- `MODEL_ROOT` 会原样挂载到容器内同名路径，`MODEL` / `DRAFT` 必须位于其下
- 本仓库以只读方式挂载到容器内 `/opt/vllm`，因此 `@` 参数写成 `/opt/vllm/hosts/<host>/...`
- `check.py` 会把容器内 `/opt/vllm/...` 映射回仓库根目录做存在性校验

## 常用命令

| 命令 | 作用 |
|---|---|
| `make init HOST=node1` | 从 `*.example` 生成 `hosts/<host>/` 和 `versions.env` |
| `make check HOST=node1` | 路径、端口、JSON、TP/world_size 一致性、`compose config` |
| `make args HOST=node1 [SERVICE=decode]` | 以 `DRY_RUN=1` 打印最终参数（不加载模型、不占 NPU） |
| `make up HOST=node1` | `check` 后 `up -d --wait`，等引擎健康再起 router |
| `make down HOST=node1` | 停止并移除容器 |
| `make logs HOST=node1` | 跟踪日志 |
| `make ps HOST=node1` | 查看状态 |
| `make install-unit` | 渲染并安装 systemd 单元（需要 sudo） |

`HOST` 可以包含点号（例如 `1.2`），Makefile 会自动把项目名转成合法形式（`vllm-1-2`）。

## 启动与关闭语义

- 引擎服务 `restart: "no"`：崩溃就停住，不会无限重启；需要开机自启时用 `systemd/vllm-stack@.service`
- 基础设施与 router `restart: unless-stopped`：秒级启动，可独立重启
- 引擎脚本以 `exec vllm serve ...` 结尾，容器 `PID 1` 就是 vLLM，`docker stop` 直接发给它
- `stop_grace_period` 建议不小于 vLLM 的退出时间（默认 120s）
- 健康检查：
  - `prefill` / `decode`：`curl 127.0.0.1:<port>/health`
  - `start_interval: 2s`：端口一就绪就翻绿，`start_period` 只是告警上限
  - `router` 通过 `depends_on: condition: service_healthy` 等待两个引擎

## 关键约束

1. **KV 池依赖共享的 `/dev/shm`**
   `compose.yaml` 声明了一个 tmpfs 卷 `mmc-shm`，同时挂到 prefill 和 decode 的 `/dev/shm`。如果 P/D 各自使用独立的 `/dev/shm`，decode 将无法命中 prefill 写入池中的 segment。
   池大小 = rank 数 × `mmc-local.conf` 里的 `dram.size`，请让 `MMC_SHM_SIZE` 留出余量。

2. **设备节点按服务划分**
   `hosts/<host>/host.yaml` 只放列表型硬件差异（`/dev/davinci*` 等），prefill 与 decode 分别映射自己的卡，二者都保留 `davinci_manager`、`devmm_svm`、`hisi_hdc`。
   映射子集时，`P_NPUS` / `D_NPUS` 必须写**容器内逻辑编号**（通常都是 `0,1,2,3`），物理卡由 `devices:` 决定；写成物理编号会导致 `ASCEND_RT_VISIBLE_DEVICES ... invalid / aclInit 107001`。

3. **单机 `network_mode: host`**
   容器直接使用宿主网络，端口不能重复；`BIND_IP` 决定监听地址。

4. **router 与 KV 连接器**
   `--kv-connector mooncake` 时，`--prefill <url> <port>` 的第二个参数是 bootstrap 端口。某些传输后端（例如 memfabric）握手语义不同，可以把它设成 `none`，让 router 直接使用 prefill 响应里的 `kv_transfer_params`。

5. **不要把真实配置提交进 git**
   `hosts/*`、`versions.env`、`*.secret.env` 已在 `.gitignore` 中。`git add -f` 会绕过它，注意不要手滑。

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
| `make check` 报端口重复 | `P_PORT` / `D_PORT` / `ROUTER_PORT` / `METRICS_PORT` 必须互不相同 |
| 容器起来但 `/health` 一直不通 | `make logs`；检查模型路径、显存利用率、capture 配置 |
| router 没启动 | 它依赖两个引擎 healthy；先看 `make ps` 与引擎日志 |
| decode 的池命中率始终为 0 | 确认 `mmc-shm` 同时挂载到 P/D，且 `kv-meta` 只有一份 |
| `docker compose config` 报项目名非法 | 主机名含点号时用 Makefile 的封装，不要手敲 `-p` |
| 长 prompt 并发上不去 | 看 decode 的 `vllm:kv_cache_usage_perc` 与 `request_queue_time_seconds` |

## 已知限制

- 只覆盖单机 1P1D，不涉及多机调度与 Kubernetes
- 面向 Ascend（`--quantization ascend`、`davinci*`、HCCL、Mooncake/AscendStore），不是跨加速器的通用部署
- `vllm-router` 仍处于 0.1.x，上线前请用真实流量验证流式输出、`usage` 统计与异常重算路径
- 通过 router 访问 Anthropic `/v1/messages` 的组合未在本骨架中验证，需要时请单独测试
- 未附带 LICENSE，使用前请自行选择

## 设计取舍

- **一个进程一个容器**：避免 bash supervisor 吞信号、退出码不可见、重启策略失控
- **参数来自环境变量**：新增一个 vLLM 参数只需改 `.env`，不用改脚本；推导不出的才进映射表
- **真实的配置不进 git**：仓库只保留模板与逻辑，避免把地址、路径、密钥带进公开历史
- **不做跨硬件抽象**：设备节点、连接器、镜像本来就是平台特有的，强行抽象只会让排错更难
