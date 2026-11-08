#!/usr/bin/env python3
"""qwen36 PD 分离 (1P1D) 代理 —— vllm-ascend 官方示例的精简版.

相比官方 load_balance_proxy_server_example.py 删除:
  - 多实例调度脚手架: min-heap 打分 / taint / drain / waiting-nodes /
    NodeListener 线程 / /instances/add|remove 路由
  - 多 worker 进程支持: SchedulerManager RPC / authkey / manager 配置文件
  - 请求打分函数与 P 侧 KV 压力记账 (仅服务于 heap 选择, 单实例下零作用)

保留 (五条正确性不变量, 修改时勿破坏):
  1. 每条路径 (正常/断开/异常/重试) 都释放 active 计数, release() 只许一次
  2. 提交 200 响应头之前预开 decode 流: decode 侧 4xx/5xx 能返回真实状态码;
     预读的首 chunk 重放回流中
  3. 监听客户端断开 (http.disconnect), 取消任务并经由 generator finally 释放
  4. stop_reason == "recomputed" (D 侧 KV 传输失败改本地重算) 时, 把已生成
     token 拼回 prompt 重新 prefill 续跑
  5. 首个 chunk 已发给客户端之后绝不重试 (first_chunk_sent 守卫)

端点: /v1/completions, /v1/chat/completions, /v1/models, /healthcheck,
/reset_prefix_cache。日志双写 /tmp/pd_proxy.log (可用 LB_PROXY_LOG_FILE
覆盖) + stdout (docker logs 可见)。

启动行为:
  - 就绪门控: 轮询 P/D 的 /health (默认 5s); 两者都 ready 之前, 新请求直接
    返回干净的 503 backends_not_ready (不喷 traceback); ready 后每 15s 复查,
    后端掉线时降级回 503 并告警, 恢复后自动重新服务
  - 启动日志 tail: 两者 ready 之前, 把 P/D 启动日志 (qwen36.sh 里重定向的
    /tmp/qwen36_pd_{prefill,decode}.log, 可用 QWEN36_PREFILL_LOG /
    QWEN36_DECODE_LOG 覆盖) 带 [Prefill]/[Decode] 前缀转发到 stdout;
    都 ready 后停止, 此后只输出本进程日志。某个后端一直不起 (崩溃循环) 时
    tail 持续进行——这正是最需要这些日志的时候
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger("pd_proxy")


# ---------------------------------------------------------------------------
# 参数 / 日志
# ---------------------------------------------------------------------------


@dataclass
class Args:
    host: str = "0.0.0.0"
    port: int = 8080
    prefiller_host: str = "127.0.0.1"
    prefiller_port: int = 8100
    decoder_host: str = "127.0.0.1"
    decoder_port: int = 8200
    max_retries: int = 3
    retry_delay: float = 0.001
    backend_check_interval: float = 15.0
    log_level: str = "INFO"


_args: Args | None = None


def parse_args() -> Args:
    p = argparse.ArgumentParser(description="qwen36 1P1D PD-disaggregated proxy")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--prefiller-host", default="127.0.0.1")
    p.add_argument("--prefiller-port", type=int, default=8100)
    p.add_argument("--decoder-host", default="127.0.0.1")
    p.add_argument("--decoder-port", type=int, default=8200)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--retry-delay", type=float, default=0.001)
    p.add_argument(
        "--backend-check-interval",
        type=float,
        default=15.0,
        help="Backend /health polling interval in seconds (15s once both are ready)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    ns = p.parse_args()
    return Args(**vars(ns))


def get_args() -> Args:
    global _args
    if _args is None:
        _args = parse_args()
    return _args


def setup_logging(log_level: str) -> None:
    """双写: 日志文件 + stdout (容器 runtime 捕获 stdout -> docker logs)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = logging.FileHandler(
        os.environ.get("LB_PROXY_LOG_FILE", "/tmp/pd_proxy.log"), encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(logging.WARNING)
    logger.setLevel(getattr(logging, log_level.upper()))


# ---------------------------------------------------------------------------
# 运行时 (单 prefiller + 单 decoder)
# ---------------------------------------------------------------------------


def build_server_url(host: str, port: int) -> str:
    url = f"http://{host}:{port}"
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address):
            url = f"http://[{host}]:{port}"
    except ValueError:
        pass
    return url


@dataclass
class Runtime:
    prefiller: tuple[str, int]
    decoder: tuple[str, int]
    prefill_client: httpx.AsyncClient
    decode_client: httpx.AsyncClient
    active_requests: int = 0
    total_requests: int = 0
    ready_prefill: bool = False
    ready_decode: bool = False


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialized")
    return _runtime


# ---------------------------------------------------------------------------
# 后端就绪检查 + 启动日志 tail
# ---------------------------------------------------------------------------


async def check_backend(client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get("/models", timeout=3.0)
        return resp.status_code == 200
    except (httpx.RequestError, httpx.HTTPStatusError):
        return False


async def readiness_loop(rt: Runtime) -> None:
    """轮询 P/D /health。未 ready 时 handle_pd 返回 503; ready 后慢速复查,
    后端掉线降级回 503 并告警, 恢复后自动重新服务."""
    args = get_args()
    while True:
        interval = args.backend_check_interval
        prefill_ok = await check_backend(rt.prefill_client)
        decode_ok = await check_backend(rt.decode_client)
        if prefill_ok and not rt.ready_prefill:
            rt.ready_prefill = True
            logger.info("Prefill backend ready")
        elif not prefill_ok and rt.ready_prefill:
            rt.ready_prefill = False
            logger.warning("Prefill backend unavailable; rejecting new requests until it recovers")
        if decode_ok and not rt.ready_decode:
            rt.ready_decode = True
            logger.info("Decode backend ready")
        elif not decode_ok and rt.ready_decode:
            rt.ready_decode = False
            logger.warning("Decode backend unavailable; rejecting new requests until it recovers")
        if rt.ready_prefill and rt.ready_decode:
            interval = 15.0
        await asyncio.sleep(interval)


def startup_log_paths() -> tuple[tuple[str, str], ...]:
    return (
        ("Prefill", os.environ.get("QWEN36_PREFILL_LOG", "/tmp/qwen36_pd_prefill.log")),
        ("Decode", os.environ.get("QWEN36_DECODE_LOG", "/tmp/qwen36_pd_decode.log")),
    )


async def tail_startup_logs(rt: Runtime) -> None:
    """把 P/D 启动日志带前缀转发到 stdout (docker logs 可见)。

    两者都 ready 后停止; 某个后端一直不起时 tail 持续进行 (崩溃循环时最
    需要这些日志)。从文件头开始读, 因此代理启动前已产生的日志会一次性补发。
    """
    paths = startup_log_paths()
    pos = {path: 0 for _tag, path in paths}
    buf = {path: b"" for _tag, path in paths}
    while not (rt.ready_prefill and rt.ready_decode):
        for tag, path in paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < pos[path]:  # 文件被截断/轮转
                pos[path] = 0
                buf[path] = b""
            if size == pos[path]:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(pos[path])
                    data = f.read(size - pos[path])
            except OSError as e:
                logger.warning("startup log tail %s: %s", path, e)
                continue
            pos[path] = size
            chunk = buf[path] + data
            while b"\n" in chunk:
                line, chunk = chunk.split(b"\n", 1)
                buf[path] = chunk
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                if text:
                    logger.info("[%s] %s", tag, text)
        await asyncio.sleep(1.0)
    logger.info("Both backends ready; stopping startup log tail")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _runtime
    args = get_args()
    limits = httpx.Limits(max_connections=100000, max_keepalive_connections=100000)
    _runtime = Runtime(
        prefiller=(args.prefiller_host, args.prefiller_port),
        decoder=(args.decoder_host, args.decoder_port),
        prefill_client=httpx.AsyncClient(
            timeout=None,
            base_url=build_server_url(args.prefiller_host, args.prefiller_port) + "/v1",
            limits=limits,
        ),
        decode_client=httpx.AsyncClient(
            timeout=None,
            base_url=build_server_url(args.decoder_host, args.decoder_port) + "/v1",
            limits=limits,
        ),
    )
    logger.info(
        "proxy up; prefill=%s:%d decode=%s:%d (waiting for readiness)",
        args.prefiller_host,
        args.prefiller_port,
        args.decoder_host,
        args.decoder_port,
    )
    readiness_task = asyncio.create_task(readiness_loop(_runtime))
    tail_task = asyncio.create_task(tail_startup_logs(_runtime))
    try:
        yield
    finally:
        readiness_task.cancel()
        tail_task.cancel()
        await _runtime.prefill_client.aclose()
        await _runtime.decode_client.aclose()
        _runtime = None


# ---------------------------------------------------------------------------
# 请求转换 / chunk 处理
# ---------------------------------------------------------------------------


def next_req_id() -> str:
    return str(uuid.uuid4())


def auth_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}",
        "X-Request-Id": request_id,
    }


def build_prefill_request(req_data: dict) -> dict:
    """预填请求: P 只做 prefill (max_tokens=1), 返回 kv_transfer_params 供 D 取 KV."""
    payload = req_data.copy()
    payload["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    payload["stream"] = False
    payload["max_tokens"] = 1
    payload["min_tokens"] = 1
    if "max_completion_tokens" in payload:
        payload["max_completion_tokens"] = 1
    payload.pop("stream_options", None)
    return payload


def extract_cached_tokens(response_json: dict) -> int:
    """取 P 响应的 usage.prompt_tokens_details.cached_tokens, 取不到为 0."""
    usage = response_json.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    return cached if isinstance(cached, int) else 0


def merge_cached_tokens_in_chunk(chunk_json: dict, prefiller_cached_tokens: int) -> bool:
    """把 P/D 两侧 cached_tokens 合并进 chunk 的 usage, 报 max(P, D).

    旧版无条件用 P 的值覆盖 D (P 未上报时为 0), 会把 D 侧真实的前缀缓存命中清零。
    """
    usage = chunk_json.get("usage")
    if not isinstance(usage, dict):
        return False
    details = usage.get("prompt_tokens_details")
    decoder_cached = None
    if isinstance(details, dict):
        raw = details.get("cached_tokens")
        decoder_cached = raw if isinstance(raw, int) else None
    reported = max(prefiller_cached_tokens, decoder_cached or 0)
    if not isinstance(details, dict):
        details = {}
    details["cached_tokens"] = reported
    usage["prompt_tokens_details"] = details
    logger.info(
        "cached_tokens merged: prefiller=%d decoder=%s -> reported=%d",
        prefiller_cached_tokens,
        decoder_cached if decoder_cached is not None else "absent",
        reported,
    )
    return True


def encode_response_chunk(chunk_json: dict, is_sse: bool) -> bytes:
    chunk = json.dumps(chunk_json, ensure_ascii=False).encode("utf-8")
    return b"data: " + chunk + b"\n\n" if is_sse else chunk


# ---------------------------------------------------------------------------
# 上游请求
# ---------------------------------------------------------------------------


async def send_prefill_request(api: str, req_data: dict, request_id: str) -> httpx.Response:
    args = get_args()
    rt = get_runtime()
    payload = build_prefill_request(req_data)
    headers = auth_headers(request_id)
    max_attempts = max(1, args.max_retries)
    for attempt in range(1, max_attempts + 1):
        try:
            response = await rt.prefill_client.post(api, json=payload, headers=headers)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 or attempt == max_attempts:
                raise
            logger.warning("req=%s prefill attempt %d failed: %s", request_id, attempt, exc)
        except httpx.RequestError as exc:
            if attempt == max_attempts:
                raise
            logger.warning("req=%s prefill attempt %d failed: %s", request_id, attempt, exc)
        await asyncio.sleep(args.retry_delay * (2 ** (attempt - 1)))


async def stream_decode_response(
    api: str, req_data: dict, request_id: str
) -> AsyncIterator[bytes]:
    """解码流。仅允许在首个 chunk 发出之前重试; 发出后绝不重试."""
    args = get_args()
    rt = get_runtime()
    headers = auth_headers(request_id)
    max_attempts = max(1, args.max_retries)
    for attempt in range(1, max_attempts + 1):
        first_chunk_sent = False
        try:
            async with rt.decode_client.stream("POST", api, json=req_data, headers=headers) as response:
                if response.status_code >= 400:
                    # 缓冲上游错误体再抛出, 让调用方拿到真实原因与状态码
                    await response.aread()
                    response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    first_chunk_sent = True
                    yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 or attempt == max_attempts:
                raise
            logger.warning("req=%s decode attempt %d failed: %s", request_id, attempt, exc)
        except httpx.RequestError as exc:
            if first_chunk_sent:
                logger.error("req=%s decode stream interrupted after sending: %s", request_id, exc)
                return
            if attempt == max_attempts:
                raise
            logger.warning("req=%s decode attempt %d failed: %s", request_id, attempt, exc)
        except Exception as exc:
            if first_chunk_sent:
                logger.error("req=%s decode stream interrupted after sending: %s", request_id, exc)
                return
            if attempt == max_attempts:
                raise
            logger.warning("req=%s decode attempt %d failed: %s", request_id, attempt, exc)
        await asyncio.sleep(args.retry_delay * (2 ** (attempt - 1)))


async def _replay_first_chunk(first_chunk: bytes, gen: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """重放预读的首 chunk, 再续接 gen (预开流用于在提交响应头前探 decode 状态)."""
    if first_chunk:
        yield first_chunk
    async for chunk in gen:
        yield chunk


# ---------------------------------------------------------------------------
# 客户端断开
# ---------------------------------------------------------------------------


async def listen_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            break


def with_cancellation(handler_func):
    @wraps(handler_func)
    async def wrapper(*args, **kwargs):
        request = kwargs["request"]
        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        cancellation_task = asyncio.create_task(listen_for_disconnect(request))
        done, pending = await asyncio.wait(
            [handler_task, cancellation_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if handler_task in done:
            return handler_task.result()
        return None

    return wrapper


# ---------------------------------------------------------------------------
# 核心: 两段式 PD 处理
# ---------------------------------------------------------------------------


async def handle_pd(api: str, request: Request):
    rt = get_runtime()
    if not (rt.ready_prefill and rt.ready_decode):
        # 启动窗口: 干净 503, 不打 traceback (模型加载要几分钟)
        logger.warning(
            "%s rejected: backends not ready (prefill=%s decode=%s)",
            api,
            rt.ready_prefill,
            rt.ready_decode,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "Prefill/decode backends are starting; retry shortly",
                    "type": "server_error",
                    "code": "backends_not_ready",
                }
            },
        )
    released = False

    def release() -> None:
        """active 计数只许释放一次 (不变量 1)."""
        nonlocal released
        if not released:
            released = True
            rt.active_requests = max(0, rt.active_requests - 1)

    try:
        req_data = await request.json()
        req_body = await request.body()
        request_length = len(req_body)
        stream_flag = bool(req_data.get("stream", False))
        chat_flag = "messages" in req_data
        if "prompt" in req_data:
            origin_prompt = req_data["prompt"]
        elif chat_flag:
            origin_prompt = req_data["messages"][0].get("content", "")
        else:
            origin_prompt = ""
        origin_max_tokens = req_data.get("max_tokens", 16)

        request_id = next_req_id()
        rt.active_requests += 1
        rt.total_requests += 1
        request_start = time.time()
        logger.info("req=%s api=%s stream=%s bytes=%d", request_id, api, stream_flag, request_length)

        # ---- 阶段 1: P prefill, 取 kv_transfer_params ----
        response = await send_prefill_request(api, req_data, request_id)
        response_json = response.json()
        kv_transfer_params = response_json.get("kv_transfer_params", {})
        if kv_transfer_params:
            req_data["kv_transfer_params"] = kv_transfer_params
        prefiller_cached_tokens = extract_cached_tokens(response_json)
        logger.info(
            "req=%s kv_transfer=%s prefill_usage=%s",
            request_id,
            bool(kv_transfer_params),
            response_json.get("usage"),
        )

        # ---- 阶段 2: 预开 decode 流 (不变量 2): 失败可返回真实状态码 ----
        prefill_done = time.time()
        logger.info("req=%s phase_prefill %.3fs", request_id, prefill_done - request_start)
        gen = stream_decode_response(api, req_data, request_id)
        try:
            preopened_first = await gen.__anext__()
        except asyncio.CancelledError:
            release()
            raise
        except StopAsyncIteration:
            preopened_first = b""
        except httpx.HTTPStatusError as exc:
            release()
            return Response(
                content=exc.response.content,
                status_code=exc.response.status_code,
                media_type="application/json",
            )
        except httpx.RequestError as exc:
            release()
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Decode backend request to {api} failed: {exc}",
                        "type": "upstream_error",
                        "code": "decode_backend_unavailable",
                    }
                },
            )

        async def generate_stream() -> AsyncIterator[bytes]:
            generated_token = ""
            logger.info(
                "req=%s phase_decode_first %.3fs (from prefill end), total %.3fs",
                request_id,
                time.time() - prefill_done,
                time.time() - request_start,
            )
            retry_count = 0
            completion_tokens = 0
            reported_cached = prefiller_cached_tokens
            cur_gen = _replay_first_chunk(preopened_first, gen)
            try:
                while True:
                    recomputed = False
                    async for chunk in cur_gen:
                        try:
                            chunk_str = chunk.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            yield chunk
                            continue
                        if not chunk_str:
                            continue
                        is_sse = chunk_str.startswith("data: ")
                        if is_sse:
                            chunk_str = chunk_str[len("data: ") :]
                        try:
                            chunk_json = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            yield chunk
                            continue

                        choices = chunk_json.get("choices", [])
                        if not choices:
                            # 流式末块 (stream_options.include_usage) 等无 choices 的 chunk
                            if merge_cached_tokens_in_chunk(chunk_json, reported_cached):
                                chunk = encode_response_chunk(chunk_json, is_sse)
                            yield chunk
                            continue
                        if not stream_flag:
                            # 非流式: 单 JSON 响应, 回填 P 侧缓存命中数
                            if merge_cached_tokens_in_chunk(chunk_json, reported_cached):
                                chunk = encode_response_chunk(chunk_json, is_sse)

                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        message = choice.get("message") or {}
                        content = (
                            delta.get("content") or message.get("content") or choice.get("text") or ""
                        )
                        generated_token += content
                        stop_reason = choice.get("stop_reason")
                        usage = chunk_json.get("usage") or {}
                        completion_tokens = (
                            (completion_tokens + 1)
                            if stream_flag
                            else completion_tokens + usage.get("completion_tokens", 0)
                        )

                        if stop_reason == "recomputed":
                            # 不变量 4: D 侧 KV 传输失败改本地重算, 拼回已生成 token 重跑
                            recomputed = True
                            retry_count += 1
                            if chat_flag:
                                messages = req_data["messages"]
                                messages[0]["content"] = (
                                    origin_prompt
                                    + ([{"type": "text", "text": generated_token}] if generated_token else [])
                                    if isinstance(origin_prompt, list)
                                    else (origin_prompt or "") + generated_token
                                )
                            else:
                                req_data["prompt"] = origin_prompt + generated_token
                            req_data["max_tokens"] = origin_max_tokens - completion_tokens + retry_count
                            resp = await send_prefill_request(api, req_data, request_id)
                            resp_json = resp.json()
                            kvtp = resp_json.get("kv_transfer_params", {})
                            if kvtp:
                                req_data["kv_transfer_params"] = kvtp
                            reported_cached = extract_cached_tokens(resp_json)
                            logger.info(
                                "req=%s recomputed: retry %d, new prefill_usage=%s",
                                request_id,
                                retry_count,
                                resp_json.get("usage"),
                            )
                            cur_gen = stream_decode_response(api, req_data, request_id)
                            break
                        if retry_count > 0 and not stream_flag:
                            # 非流式: 客户端只应看到最终拼接全文, 覆盖中间重算的输出
                            if chat_flag:
                                choice["message"]["content"] = generated_token
                            else:
                                choice["text"] = generated_token
                            chunk = encode_response_chunk(chunk_json, is_sse)
                        yield chunk
                    if not recomputed:
                        break
            except asyncio.CancelledError:
                logger.warning("req=%s client cancelled; releasing", request_id)
                raise
            except Exception:
                logger.exception("req=%s decode stream error", request_id)
            finally:
                release()
                logger.info(
                    "req=%s api=%s done: stream=%s tokens=%d retries=%d reported_cached=%d elapsed=%.2fs",
                    request_id,
                    api,
                    stream_flag,
                    completion_tokens,
                    retry_count,
                    reported_cached,
                    time.time() - request_start,
                )

        media_type = "text/event-stream; charset=utf-8" if stream_flag else "application/json"
        return StreamingResponse(generate_stream(), media_type=media_type)
    except httpx.HTTPStatusError as e:
        release()
        try:
            return JSONResponse(e.response.json(), status_code=e.response.status_code)
        except Exception:
            return JSONResponse(
                {"error": {"message": e.response.text or "upstream error"}},
                status_code=e.response.status_code,
            )
    except Exception:
        release()
        logger.exception("Error handling %s request", api)
        raise


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)


@app.post("/v1/completions")
@with_cancellation
async def handle_completions(request: Request):
    return await handle_pd("/completions", request)


@app.post("/v1/chat/completions")
@with_cancellation
async def handle_chat_completions(request: Request):
    return await handle_pd("/chat/completions", request)


@app.get("/v1/models")
async def handle_models():
    rt = get_runtime()
    request_id = next_req_id()
    for name, client in (("prefill", rt.prefill_client), ("decode", rt.decode_client)):
        try:
            resp = await client.get("/models", headers=auth_headers(request_id), timeout=3.0)
            resp.raise_for_status()
            return Response(content=resp.content, media_type="application/json")
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning("GET /models from %s failed: %s", name, exc)
    return JSONResponse(
        status_code=503, content={"error": "All backend instances failed to serve /v1/models"}
    )


@app.get("/healthcheck")
async def healthcheck():
    rt = get_runtime()
    return {
        "status": "ok",
        "prefill_instances": 1,
        "decode_instances": 1,
        "active_requests": rt.active_requests,
        "total_requests": rt.total_requests,
    }


@app.post("/reset_prefix_cache")
async def reset_prefix_cache(request: Request):
    rt = get_runtime()
    params = dict(request.query_params)
    failures = []
    for name, client, (host, _port) in (
        ("prefill", rt.prefill_client, rt.prefiller),
        ("decode", rt.decode_client, rt.decoder),
    ):
        try:
            url = build_server_url(host, _port) + "/reset_prefix_cache"
            resp = await client.post(url, params=params, timeout=10.0)
            resp.raise_for_status()
        except Exception as e:
            logger.error("reset_prefix_cache failed for %s: %s", host, e)
            failures.append(host)
    if failures:
        return JSONResponse(status_code=500, content={"failed": failures})
    return Response(status_code=200)


def main() -> None:
    args = get_args()
    setup_logging(args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
